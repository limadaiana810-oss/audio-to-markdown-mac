#!/usr/bin/env python3
"""Local GUI bridge: JSON events out, private logs on disk, existing ASR unchanged."""
import argparse
import codecs
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".mp4", ".mov", ".amr"}


def emit_event(kind, **fields):
    print(json.dumps({"type": kind, **fields}, ensure_ascii=False, allow_nan=False), flush=True)


def collect_inputs(paths):
    files, seen = [], set()
    for raw in paths:
        path = Path(raw).expanduser()
        if not path.exists():
            raise ValueError(f"找不到所选录音：{path.name}，请重新选择。")
        if path.is_dir():
            candidates = [p for p in sorted(path.iterdir()) if p.is_file()
                          and p.suffix.lower() in AUDIO_EXT and not p.name.startswith(".")]
        elif path.is_file() and path.suffix.lower() in AUDIO_EXT:
            candidates = [path]
        else:
            raise ValueError(f"不支持这个文件：{path.name}。请选择录音文件或文件夹。")
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved not in seen:
                files.append(resolved)
                seen.add(resolved)
    if not files:
        raise ValueError("没有找到可转写的录音，请选择录音文件或包含录音的文件夹。")
    return files


class TaskLock:
    def __init__(self, path):
        self.path, self.stream = Path(path), None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a+")
        try:
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.stream.close()
            self.stream = None
            raise ValueError("已有转写任务正在运行，请等它完成或先停止当前任务。")
        self.stream.seek(0)
        self.stream.truncate()
        self.stream.write(str(os.getpid()))
        self.stream.flush()
        return self

    def __exit__(self, *_):
        if self.stream is not None:
            fcntl.flock(self.stream, fcntl.LOCK_UN)
            self.stream.close()


class Progress:
    """Forward only the engine's explicit protocol, never inferred log text."""
    PREFIX = "TX_PROGRESS "
    STATES = {"queued", "preparing", "running", "exporting", "completed", "failed"}

    def __init__(self, emit, sources=None):
        self.emit = emit
        self.sources = {str(path) for path in sources} if sources is not None else None
        self.overall, self.files = 0.0, {}

    def feed(self, line):
        if not line.startswith(self.PREFIX):
            return
        try:
            event = json.loads(line[len(self.PREFIX):])
            if not isinstance(event, dict):
                return
            value = event.get("progress")
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 1):
                return
            if event.get("type") == "file":
                path, state = event.get("path"), event.get("state")
                if (not isinstance(path, str) or state not in self.STATES
                        or (self.sources is not None and path not in self.sources)):
                    return
                previous = self.files.get(path, {})
                if previous.get("state") == "completed" and state != "completed":
                    return
                fields = {"path": path, "state": state,
                          "progress": max(value, previous.get("progress", 0.0))}
                for key in ("completed_chunks", "total_chunks"):
                    if type(event.get(key)) is int and event[key] >= 0:
                        fields[key] = event[key]
                if state == "completed" and value != 1:
                    return
                self.files[path] = fields
                self.emit("file", **fields)
            elif event.get("type") == "status":
                self.overall = max(self.overall, min(.99, value))
                fields = {"progress": self.overall,
                          "message": "正在合并…" if event.get("message") == "正在合并…" else "正在转写…"}
                if type(event.get("completed_chunks")) is int and event["completed_chunks"] >= 0:
                    fields["completed_chunks"] = event["completed_chunks"]
                self.emit("status", **fields)
        except (ValueError, TypeError):
            return


def libproc_group_has_live_members(group_id):
    """macOS kernel query for environments that cannot launch /bin/ps.

    Constants/layout follow the public macOS libproc.h and sys/proc_info.h ABI.
    An unavailable or denied query raises; it never means the group is empty.
    """
    import ctypes
    import errno

    if sys.platform != "darwin":
        raise OSError("当前系统无法查询转写进程组")
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_listpids.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    library.proc_listpids.restype = ctypes.c_int
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                    ctypes.c_void_p, ctypes.c_int]
    library.proc_pidinfo.restype = ctypes.c_int

    class ShortInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in ("pid", "ppid", "pgid", "status")] + [
            ("comm", ctypes.c_char * 16)] + [(name, ctypes.c_uint32) for name in (
                "flags", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]

    capacity = 64
    while capacity <= 65536:
        members = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        size = library.proc_listpids(2, group_id, members, ctypes.sizeof(members))  # PROC_PGRP_ONLY
        error = ctypes.get_errno()
        if size < 0 or (size == 0 and error):
            raise OSError(error or errno.EIO, "无法确认转写进程组状态")
        if size >= ctypes.sizeof(members):
            capacity *= 2
            continue
        for pid in members[:size // ctypes.sizeof(ctypes.c_int)]:
            if pid <= 0:
                continue
            info = ShortInfo()
            ctypes.set_errno(0)
            read = library.proc_pidinfo(pid, 13, 0, ctypes.byref(info), ctypes.sizeof(info))
            error = ctypes.get_errno()
            if read != ctypes.sizeof(info):
                if read == 0 and error == errno.ESRCH:
                    continue  # The member exited between the two kernel queries.
                raise OSError(error or errno.EIO, "无法确认转写进程状态")
            if info.pgid == group_id and info.status != 5:  # SZOMB has already exited.
                return True
        return False
    raise ValueError("无法完整读取转写进程组，请稍后重试。")


def group_has_live_members(group_id):
    try:
        snapshot = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="], capture_output=True,
                                  text=True, timeout=1, check=True)
    except (OSError, subprocess.SubprocessError):
        return libproc_group_has_live_members(group_id)
    members = [line.split() for line in snapshot.stdout.splitlines()]
    return any(len(parts) >= 2 and parts[0] == str(group_id)
               and not parts[1].startswith("Z") for parts in members)


def stop_owned_process(process, grace=.2):
    """Reap the known single-process caffeinate helper, without inspecting unrelated processes."""
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)


def confirm_group_stopped(process, timeout=.5):
    """Confirm the owned group has no running members; exited zombies are harmless."""
    process.wait(timeout=timeout)
    deadline = time.monotonic() + timeout
    while True:
        live = group_has_live_members(process.pid)
        if not live:
            return
        if time.monotonic() >= deadline:
            raise ValueError("尚不能确认转写任务已停止，请稍后重试。")
        time.sleep(.05)


def stop_process_group(process, grace=5.0):
    """A worker's inherited pipe must not keep the GUI alive after cancellation."""
    for signum, timeout in ((signal.SIGINT, grace), (signal.SIGTERM, 1.0), (signal.SIGKILL, .5)):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            break
        except PermissionError:
            # macOS can report EPERM for an already terminating process group.
            # Only accept that race after the direct child is demonstrably reaped.
            confirm_group_stopped(process)
            break
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            process.poll()  # reap the direct child before checking its process group
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            except PermissionError:
                confirm_group_stopped(process)
                break
            time.sleep(.05)
        else:
            continue
        break
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=1)
    confirm_group_stopped(process)


def run_process(command, log_stream, cancelled, line_callback=None, environment=None):
    """Drain output without blocking cancellation; all descendants share a new group."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               stdin=subprocess.DEVNULL, bufsize=0, start_new_session=True,
                               env=environment)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ)
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    pending, exited_at = "", None
    try:
        while selector.get_map():
            if cancelled.is_set():
                stop_process_group(process)
                return 130
            for key, _ in selector.select(.1):
                block = os.read(key.fileobj.fileno(), 65536)
                if not block:
                    selector.unregister(key.fileobj)
                    continue
                log_stream.write(block)
                log_stream.flush()
                pending += decoder.decode(block)
                lines = pending.split("\n")
                pending = lines.pop()
                if line_callback:
                    for line in lines:
                        line_callback(line)
            if process.poll() is not None:
                exited_at = exited_at or time.monotonic()
                # An orphan holding stdout open must be cleaned up, not awaited forever.
                if selector.get_map() and time.monotonic() - exited_at > 1:
                    stop_process_group(process, grace=.2)
                    break
        pending += decoder.decode(b"", final=True)
        if pending and line_callback:
            line_callback(pending)
        while process.poll() is None:
            if cancelled.wait(.1):
                stop_process_group(process)
                return 130
        return process.returncode
    finally:
        selector.close()
        process.stdout.close()
        if process.poll() is None:
            stop_process_group(process)


def completed_document(pointer, out_root, sources, started_at):
    """Resolve only this invocation's private pointer, never the mutable latest index."""
    path = Path(pointer).read_text("utf-8").strip()
    history = (Path(out_root) / ".批次").resolve()
    index = Path(path).resolve(strict=True)
    if index.parent != history or index.suffix != ".json":
        raise ValueError("本次任务的结果索引无效。")
    batch = json.loads(index.read_bytes())
    if (not isinstance(batch, dict) or batch.get("status") != "completed"
            or Path(batch.get("index_path", "")).resolve() != index
            or not isinstance(batch.get("started_at"), (int, float))
            or isinstance(batch["started_at"], bool)
            or not math.isfinite(batch["started_at"])
            or batch["started_at"] < started_at - 1):
        raise ValueError("本次转写没有全部完成，请重新开始以继续处理。")
    entries = batch.get("files", [])
    expected = [str(Path(p).resolve()) for p in sources]
    if (not isinstance(entries, list) or len(entries) != len(expected)
            or not all(isinstance(e, dict) for e in entries)
            or [e.get("source") for e in entries] != expected
            or any(e.get("status") not in ("completed", "skipped") for e in entries)):
        raise ValueError("本次结果与所选录音不一致，请重新开始。")
    merged = batch.get("merged_markdown", {})
    if not isinstance(merged, dict) or merged.get("status") != "completed":
        raise ValueError("录音文字已保留，但完整 Markdown 尚未生成，请重试。")
    document = Path(merged.get("path", "")).resolve(strict=True)
    if document.parent != (Path(out_root) / "合并全文").resolve() or document.suffix != ".md":
        raise ValueError("本次 Markdown 的位置无效。")
    content = document.read_bytes()
    if not content or hashlib.sha256(content).hexdigest() != merged.get("sha256"):
        raise ValueError("本次 Markdown 校验未通过，请重试。")
    return document


def run_gui(inputs, *, app_dir=None, user_dir=None, program_dir=None, emit=emit_event,
            cancelled=None):
    app = Path(app_dir or os.environ.get("TX_APP_DIR") or
               Path.home() / "Library/Application Support/录音转写工具").expanduser().resolve()
    user = Path(user_dir or os.environ.get("TX_USER_DIR") or Path.home() / "录音转写").expanduser().resolve()
    program = Path(program_dir or Path(__file__).parent).resolve()
    model_dir = Path(os.environ.get("TX_MODEL_DIR") or app / "models-light").expanduser().resolve()
    out, logs = user / "转写结果", user / "日志"
    cancelled = cancelled or threading.Event()
    log_path, awake = None, None
    try:
        sources = collect_inputs(inputs)
        with TaskLock(app / ".gui-task.lock"):
            if cancelled.is_set():
                emit("cancelled", message="已停止，已完成的部分会保留。")
                return 130
            logs.mkdir(parents=True, exist_ok=True)
            out.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix="界面转写_", suffix=".log", dir=logs)
            log_path = Path(name)
            environment = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                               TQDM_DISABLE="1", TX_MODEL_DIR=str(model_dir),
                               PYTHONNOUSERSITE="1")
            for name in ("PYTHONPATH", "PYTHONHOME"):
                environment.pop(name, None)
            caffeine = shutil.which("caffeinate")
            if caffeine:
                awake = subprocess.Popen([caffeine, "-i", "-w", str(os.getpid())],
                                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL, start_new_session=True)
            with os.fdopen(fd, "wb") as log_stream, tempfile.TemporaryDirectory(prefix="gui-", dir=app) as temp:
                emit("status", message=f"已选择 {len(sources)} 份录音，正在准备…", progress=0.0,
                     completed_chunks=0)
                verify = [sys.executable, str(program / "selftest.py"), "--engine", "light",
                          "--model-dir", str(model_dir), "--manifest",
                          str(program / "models-light.manifest.json"), "--verify-models-only"]
                result = run_process(verify, log_stream, cancelled, environment=environment)
                if cancelled.is_set() or result == 130:
                    emit("cancelled", message="已停止，已完成的部分会保留。")
                    return 130
                if result:
                    raise ValueError("转写准备未完成，请重新打开应用后重试。")
                pointer = Path(temp) / "batch.txt"  # Must not exist until tx publishes this run's ID.
                command = [sys.executable, str(program / "tx.py"), "run", "-o", str(out),
                           "--voiceprints", str(user / "声纹样本"), "--hotwords", str(user / "热词.txt"),
                           "--engine", "light", "--model-dir", str(model_dir),
                           "--workers", "4", "--batch-id-file", str(pointer), "--progress-events", "--",
                           *(str(p) for p in sources)]
                started_at = time.time()
                result = run_process(command, log_stream, cancelled, Progress(emit, sources).feed, environment)
                if cancelled.is_set() or result in (130, -signal.SIGINT):
                    emit("cancelled", message="已停止。再次开始会继续处理已选录音。")
                    return 130
                if result:
                    raise ValueError("部分录音尚未完成。已完成的内容已保留，请点击开始转写以重试。")
                document = completed_document(pointer, out, sources, started_at)
                emit("complete", path=str(document), file_count=len(sources))
                return 0
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "本次处理未完成，请重试或查看日志。"
        fields = {"message": message}
        if log_path is not None:
            fields["log_path"] = str(log_path)
            try:
                with log_path.open("ab") as stream:
                    stream.write((f"\nGUI backend: {type(exc).__name__}: {exc}\n").encode("utf-8"))
            except OSError:
                pass
        emit("error", **fields)
        return 2
    finally:
        if awake is not None:
            stop_owned_process(awake)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    args = parser.parse_args()
    cancelled = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: cancelled.set())
    return run_gui(args.inputs, cancelled=cancelled)


if __name__ == "__main__":
    raise SystemExit(main())
