#!/usr/bin/env python3
"""同录音 original/light 四进程对照；只把自动可证明的项目标为通过。

示例（使用已安装 venv 的 Python）：
  python accept_light.py --audio 会议.wav --source-kind real \
    --reference 人工逐字稿.txt --reference-kind human \
    --original-model-dir /path/to/models --light-model-dir /path/to/light-models \
    --out /path/to/新验收目录

输出目录必须不存在。两引擎使用独立空缓存，按轮次交替先后顺序。
--repeats 默认 3；开发冒烟可设 1，但短样例不能代表真实长录音。
参考稿须为 UTF-8 纯正文（不含标题、时间戳、说话人标签）。退出码 0
仅代表工具执行且没有自动失败项，人工待验项目不会因此变为通过。
"""
import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import shutil
import signal
import statistics
import subprocess
import sys
import time
import unicodedata


MODEL_LIMIT_BYTES = 700_000_000  # MB 用十进制，报告同时提供 MiB。
CER_MARGIN = 0.005             # 0.5 个百分点，不是相对 0.5%。
SPEED_REDUCTION = 0.30
TARGET_AUDIO_SECONDS = 10 * 3600
TARGET_WALL_SECONDS = 15 * 60
MANUAL_ITEMS = {
    "names": "姓名、公司名和产品名逐项核对",
    "numbers": "数量、金额、比例等数字逐项核对",
    "dates": "日期和时间逐项核对",
    "negations": "不、没、不能等否定表达逐项核对",
    "speakers": "说话人切换、身份及跨段一致性核对",
    "speech_omissions": "对照原音频检查漏话、远场及重叠说话",
}


def normalize_text(text):
    """NFKC 后仅去除空白与 Unicode 标点；不转换数字、同音字或大小写。"""
    return "".join(c for c in unicodedata.normalize("NFKC", text)
                   if not c.isspace() and not unicodedata.category(c).startswith("P"))


def edit_distance(reference, hypothesis):
    try:
        from rapidfuzz.distance import Levenshtein
        return Levenshtein.distance(reference, hypothesis)
    except ImportError:
        # 开发测试无需模型依赖；长稿必须使用锁文件已有的 rapidfuzz，避免慢速二次方回退。
        if len(reference) * len(hypothesis) > 1_000_000:
            raise RuntimeError("长稿 CER 需要 rapidfuzz，请使用本工具已安装的 venv Python")
        if len(hypothesis) > len(reference):
            reference, hypothesis = hypothesis, reference
        previous = list(range(len(hypothesis) + 1))
        for i, a in enumerate(reference, 1):
            current = [i]
            for j, b in enumerate(hypothesis, 1):
                current.append(min(current[-1] + 1, previous[j] + 1,
                                   previous[j - 1] + (a != b)))
            previous = current
        return previous[-1]


def cer(reference, hypothesis):
    ref, hyp = normalize_text(reference), normalize_text(hypothesis)
    if not ref:
        raise ValueError("人工逐字稿去除空白、标点后为空，不能计算 CER")
    edits = edit_distance(ref, hyp)
    return {"reference_characters": len(ref), "hypothesis_characters": len(hyp),
            "edit_distance": edits, "cer": edits / len(ref)}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_inventory(root):
    """统计模型目录全部文件；跟随符号链接，按实际文件去重，防止漏算共享模型。"""
    seen_dirs, seen_files, files = set(), set(), []

    def walk(directory):
        resolved = directory.resolve(strict=True)
        if resolved in seen_dirs:
            return
        seen_dirs.add(resolved)
        for path in sorted(directory.iterdir()):
            if path.is_dir():
                walk(path)
            elif path.is_file():
                st = path.stat()
                key = (st.st_dev, st.st_ino)
                if key not in seen_files:
                    seen_files.add(key)
                    files.append({"path": str(path.relative_to(root)), "bytes": st.st_size})
            else:
                raise ValueError(f"模型目录有损坏链接或非普通文件：{path}")

    walk(root)
    if not files:
        raise ValueError(f"模型目录为空：{root}")
    total = sum(f["bytes"] for f in files)
    return {"bytes": total, "MB": total / 1_000_000, "MiB": total / 1024 ** 2,
            "limit_bytes": MODEL_LIMIT_BYTES, "files": files,
            "method": "模型目录全部实际文件，跟随符号链接，同 inode 只计一次；不含 Python 依赖"}


def hardware_info():
    def sysctl(key):
        try:
            r = subprocess.run(["sysctl", "-n", key], capture_output=True, text=True, check=True)
            return r.stdout.strip()
        except (OSError, subprocess.CalledProcessError):
            return None
    memory = sysctl("hw.memsize")
    return {"system": platform.system(), "release": platform.release(),
            "machine": platform.machine(), "chip": sysctl("machdep.cpu.brand_string"),
            "memory_bytes": int(memory) if memory and memory.isdigit() else None,
            "logical_cpus": os.cpu_count(), "python": sys.version}


def interpreter_info(python):
    code = ("import sys,json,importlib.metadata as m\n"
            "versions={}\n"
            "for name in ('torch','funasr','numpy','onnxruntime','rapidfuzz'):\n"
            " try: versions[name]=m.version(name)\n"
            " except m.PackageNotFoundError: versions[name]=None\n"
            "print(json.dumps({'executable':sys.executable,'version':sys.version,'packages':versions}))\n")
    result = subprocess.run([python, "-c", code], capture_output=True, text=True, check=True, timeout=30)
    return json.loads(result.stdout)


def peak_concurrent_chunks(metrics):
    """按分段实际起止时间求重叠；同一时刻先结束再开始，不把首尾相接算并发。"""
    events = []
    for item in metrics:
        if not isinstance(item, dict):
            return None
        start, end, pid = item.get("started_at"), item.get("finished_at"), item.get("worker_pid")
        if (not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (start, end))
                or start >= end or not isinstance(pid, int) or pid <= 0):
            return None
        events.extend(((start, 1, pid), (end, -1, pid)))
    active, peak = set(), 0
    for _, delta, pid in sorted(events):
        if delta == -1:
            active.remove(pid)
        else:
            # 同一 worker 不可能同时执行两段；错误的指标不能被用来放大并发数。
            if pid in active:
                return None
            active.add(pid)
            peak = max(peak, len(active))
    return peak


def validate_outputs(out_root, source):
    """验证处理段完整、缓存句子完整合并；不能证明原音频没有漏识别。"""
    # 按实际文件名查找，录音名中的 [] 等字符不能被当作 glob 表达式。
    result_files = []
    for base in (out_root, out_root / "轻量试用"):
        if base.is_dir():
            result_files += [p / f"{source.stem}.json" for p in base.iterdir()
                             if p.is_dir() and (p / f"{source.stem}.json").is_file()]
    if len(result_files) != 1:
        raise ValueError(f"预期一份转写 JSON，实际 {len(result_files)} 份")
    result = result_files[0].parent
    data = json.loads(result_files[0].read_text("utf-8"))
    total = float(data["时长秒"])
    if not math.isfinite(total) or total <= 0:
        raise ValueError("音频总时长无效")
    sentences = data["句子"]
    starts = []
    for s in sentences:
        a, b = float(s["start"]), float(s["end"])
        if not all(map(math.isfinite, (a, b))) or not 0 <= a <= b <= total + 0.15:
            raise ValueError("句子时间越界或起止倒置")
        if not isinstance(s.get("text"), str) or not s.get("speaker"):
            raise ValueError("句子文字或说话人字段无效")
        starts.append(a)
    if starts != sorted(starts):
        raise ValueError("转写句子没有按时间排序")
    if [s.get("id") for s in sentences] != [f"s{i:06d}" for i in range(1, len(sentences) + 1)]:
        raise ValueError("句子 id 缺失、重复或与输出顺序不符")
    completed_path = result / ".work/completed.json"
    completed = json.loads(completed_path.read_text("utf-8"))
    if Path(completed["source"]["path"]).resolve() != source.resolve():
        raise ValueError("完成标记对应了不同源文件")
    required = ["目录.md", f"{source.stem}.json", f"{source.stem}.srt", f"{source.stem}_全文.md",
                "待复核.json", "待复核.md"]
    if not list((result / "分段").glob("*.md")):
        raise ValueError("没有分段 Markdown")
    for name in set(required + completed.get("outputs", [])):
        path = (result / name).resolve()
        if not path.is_relative_to(result.resolve()) or not path.is_file():
            raise ValueError(f"缺少输出文件或输出路径无效：{name}")
    hashes = completed.get("output_sha256")
    if hashes is not None:
        if not isinstance(hashes, dict) or set(hashes) != set(completed.get("outputs", [])):
            raise ValueError("输出完整性清单与完成标记不一致")
        for name, expected_hash in hashes.items():
            if sha256(result / name) != expected_hash:
                raise ValueError(f"输出完整性校验失败：{name}")
    plans = list((result / ".work").glob("source_*/plan_*.json"))
    if len(plans) != 1:
        raise ValueError("验收必须使用独立空缓存，且只有一个分段计划")
    plan_file = plans[0]
    plan = json.loads(plan_file.read_text("utf-8"))
    bounds = plan["chunks"]
    if not bounds:
        raise ValueError("分段计划为空")
    previous = 0.0
    for start, end in bounds:
        if not all(map(math.isfinite, (start, end))) or abs(start - previous) > 0.001 or end <= start:
            raise ValueError("分段计划有空隙、重叠或逆序")
        previous = end
    if abs(previous - plan["total"]) > 0.001 or abs(plan["total"] - total) > 0.15:
        raise ValueError("分段计划没有覆盖音频全长")
    fingerprint = plan_file.stem.removeprefix("plan_")
    chunk_files = list(plan_file.parent.glob(f"chunk_{fingerprint}_*.json"))
    expected = [plan_file.parent / f"chunk_{fingerprint}_{i:04d}.json" for i in range(len(bounds))]
    if set(chunk_files) != set(expected):
        raise ValueError("处理分段有缺失或多余编号")
    chunks = [json.loads(p.read_text("utf-8")) for p in expected]
    cache_sentences = []
    for (a, b), chunk in zip(bounds, chunks):
        if abs(chunk["start"] - a) > 0.001 or abs(chunk["end"] - b) > 0.001:
            raise ValueError("缓存分段时间与计划不符")
        cache_sentences.extend(chunk["sentences"])
    def sentence_keys(items):
        return Counter((round(float(s["start"]), 3), round(float(s["end"]), 3), s["text"]) for s in items)
    if sentence_keys(cache_sentences) != sentence_keys(sentences):
        raise ValueError("最终正文丢失、重复或改动了缓存句子")
    reviews = [item for chunk in chunks for item in chunk.get("review", [])]
    review_file = json.loads((result / "待复核.json").read_text("utf-8"))
    if reviews != data.get("待复核", []) or reviews != review_file.get("items"):
        raise ValueError("缓存中的待复核问题或原文没有完整导出")
    stat = json.loads((plan_file.parent / "stat.json").read_text("utf-8"))
    if stat.get("复用缓存分段数") != 0:
        raise ValueError("本次计时复用了转写缓存")
    metrics = stat.get("本次分段指标", [])
    if not isinstance(metrics, list):
        metrics = []
    pids = {m["worker_pid"] for m in metrics if isinstance(m, dict) and isinstance(m.get("worker_pid"), int)}
    timing_complete = (len(metrics) == len(chunks) and all(isinstance(m, dict) for m in metrics)
                       and {m.get("index") for m in metrics} == set(range(len(chunks))))
    overlap = peak_concurrent_chunks(metrics) if timing_complete else None
    return {"status": "passed", "audio_seconds": total, "chunk_count": len(chunks),
            "sentence_count": len(sentences), "review_item_count": len(reviews),
            "empty_chunks": [i for i, c in enumerate(chunks) if not c["sentences"]],
            "worker_pids": sorted(pids), "actual_workers": stat.get("实际转写进程数"),
            "peak_concurrent_chunks": overlap, "inference_timing_complete": timing_complete and overlap is not None,
            "timestamps_ordered": True, "sentence_ids_ordered": True, "all_planned_chunks_present": True,
            "all_cached_sentences_exported": True, "transcript": "".join(s["text"] for s in sentences),
            "result": str(result), "note": "结构检查通过不等于音频内容无漏识别；静音段可合法没有句子"}


def process_group_rss(pid):
    r = subprocess.run(["ps", "-axo", "pid=,pgid=,rss="], capture_output=True, text=True, check=True)
    members = [line.split() for line in r.stdout.splitlines() if len(line.split()) == 3]
    return sum(int(row[2]) * 1024 for row in members if int(row[1]) == pid)


def stop_process_group(proc):
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGINT)
        proc.wait(timeout=15)
    except ProcessLookupError:
        pass
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def run_case(command, folder, timeout, sample_interval):
    folder.mkdir()
    result = {"command": command, "run_directory": str(folder), "rss_sample_interval_seconds": sample_interval}
    env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONUNBUFFERED="1")
    started = time.monotonic()
    peak = samples = 0
    with (folder / "运行.log").open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, start_new_session=True)
        try:
            while proc.poll() is None:
                if time.monotonic() - started > timeout:
                    result["error"] = "运行超时"
                    stop_process_group(proc)
                    break
                peak = max(peak, process_group_rss(proc.pid))
                samples += 1
                try:
                    proc.wait(timeout=sample_interval)
                except subprocess.TimeoutExpired:
                    pass
        finally:
            stop_process_group(proc)
    result.update({"returncode": proc.returncode, "wall_seconds": time.monotonic() - started,
                   "sampled_process_group_peak_rss_bytes": peak, "rss_samples": samples,
                   "rss_note": "采样时主进程及同组子进程 RSS 之和的最大值；可能重复计共享页并漏过采样间峰值"})
    (folder / "计时.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
    return result


def gate(status, detail):
    return {"status": status, "detail": detail}


def target_speed_gate(audio_seconds, wall_seconds, completed):
    """独立绝对门槛；短样例、未完成或无效数字不能替代 10h 验收。"""
    finite_positive = lambda value: (isinstance(value, (int, float)) and not isinstance(value, bool)
                                     and math.isfinite(value) and value > 0)
    if not completed or not all(map(finite_positive, (audio_seconds, wall_seconds))):
        return gate("failed", "未完成有效输出或时长/耗时无效，不能通过速度验收")
    if audio_seconds < TARGET_AUDIO_SECONDS:
        return gate("pending", "素材不足 10 小时，只能作为短样例验证")
    return gate("passed" if wall_seconds <= TARGET_WALL_SECONDS else "failed",
                f"{audio_seconds:.3f} 秒音频耗时 {wall_seconds:.3f} 秒；要求至少 10h 且 ≤900 秒")


def evaluate(runs, inventory, hardware, source_kind, reference_kind, unchanged):
    groups = {engine: [r for r in runs if r["engine"] == engine] for engine in ("original", "light")}
    valid = bool(runs) and all(groups.values()) and all(
        r.get("returncode") == 0 and r.get("validation", {}).get("status") == "passed" for r in runs)
    gates = {
        "run_and_structure": gate("passed" if valid else "failed", "两引擎全部轮次执行成功、时间有序且计划分段完整导出"),
        "source_unchanged": gate("passed" if unchanged else "failed", "运行前后源录音 SHA-256 相同"),
        "model_size": gate("passed" if inventory["bytes"] <= MODEL_LIMIT_BYTES else "failed",
                           f"轻量模型目录 {inventory['bytes']} bytes；上限 {MODEL_LIMIT_BYTES} bytes"),
        "target_hardware": gate("passed" if hardware.get("chip") == "Apple M5 Pro" and hardware.get("memory_bytes") == 48 * 1024 ** 3 else "pending",
                                "目标硬件为 Apple M5 Pro、48 GiB 内存；其他硬件数据仅作开发参考"),
        "real_recording": gate("passed" if source_kind == "real" else "pending", "真实录音验收；合成或来源未声明的音频只能作流程验证"),
        "absolute_accuracy": gate("pending", "尚未约定绝对准确率门槛；相对原版退化不超过 0.5 个百分点不能证明准确"),
    }
    target_checks = [target_speed_gate(r.get("validation", {}).get("audio_seconds"), r.get("wall_seconds"),
                                     r.get("returncode") == 0 and r.get("validation", {}).get("status") == "passed")
                     if r.get("validation", {}).get("audio_seconds") is not None else
                     gate("pending", "缺少有效录音时长，不能通过 10h 速度验收") for r in groups["light"]]
    target_status = ("failed" if any(g["status"] == "failed" for g in target_checks) else
                     "passed" if target_checks and all(g["status"] == "passed" for g in target_checks) else "pending")
    gates["ten_hour_speed"] = gate(target_status, "轻量版每轮必须完整处理至少 10h，且每轮墙钟耗时 ≤900 秒；短样例不算通过")
    four = valid and all(r["validation"].get("actual_workers") == 4
                         and len(r["validation"].get("worker_pids", [])) == 4
                         and r["validation"].get("peak_concurrent_chunks") == 4 for r in runs)
    gates["four_workers"] = gate("passed" if four else "pending",
                                 "两引擎每轮有 4 个转写进程，且分段实际起止时间证明峰值 4 段重叠；仅有 4 个 PID 不算通过")
    measurements = {}
    if valid:
        medians = {e: statistics.median(r["wall_seconds"] for r in rs) for e, rs in groups.items()}
        reduction = (medians["original"] - medians["light"]) / medians["original"]
        measurements.update({"median_wall_seconds": medians, "wall_time_reduction": reduction,
                             "speedup_factor": medians["original"] / medians["light"]})
        gates["speed"] = gate("passed" if reduction + 1e-12 >= SPEED_REDUCTION else "failed",
                             f"总墙钟中位数耗时下降 {reduction * 100:.3f}%；要求 ≥30%（包含加载、转码和导出）")
    else:
        gates["speed"] = gate("pending", "运行或输出检查失败，不能比较耗时")
    if valid and all("cer" in r for r in runs):
        # 多次运行按最差轻量稿对最佳原版稿，防止挑选较好一轮掩盖退化。
        baseline = min(r["cer"]["cer"] for r in groups["original"])
        light = max(r["cer"]["cer"] for r in groups["light"])
        measurements.update({"original_best_cer": baseline, "light_worst_cer": light,
                             "cer_delta_percentage_points": (light - baseline) * 100})
        status = "passed" if light <= baseline + CER_MARGIN + 1e-12 else "failed"
        if reference_kind != "human":
            status = "pending"
        gates["cer"] = gate(status, f"light CER − original CER = {(light - baseline) * 100:.4f} 个百分点；上限 +0.5；仅人工逐字稿可作质量证据")
    else:
        gates["cer"] = gate("pending", "缺少有效人工逐字稿或可验证的转写结果")
    automatic = "failed" if any(g["status"] == "failed" for g in gates.values()) else (
        "pending" if any(g["status"] == "pending" for g in gates.values()) else "passed")
    return {"automatic_status": automatic, "overall_status": "failed" if automatic == "failed" else "pending",
            "gates": gates, "measurements": measurements,
            "manual_checks": {key: gate("pending", text) for key, text in MANUAL_ITEMS.items()},
            "conclusion": "存在自动检查失败项" if automatic == "failed" else "仍需人工核对，不能标记完整验收通过"}


def write_report(folder, report):
    (folder / "验收报告.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), "utf-8")
    labels = {"passed": "通过", "pending": "待验", "failed": "失败"}
    lines = ["# 轻量引擎对照验收", "", f"结论：{report['conclusion']}。", "",
             f"录音：{report['source']['path']}", "", "| 自动检查 | 状态 | 说明 |", "|---|---|---|"]
    lines += [f"| {key} | {labels[value['status']]} | {value['detail']} |" for key, value in report["gates"].items()]
    lines += ["", "## 人工检查", "", "以下项目不由字符错误率、合成音频或结构检查自动代替。", ""]
    lines += [f"- [ ] {value['detail']}（待验）" for value in report["manual_checks"].values()]
    lines += ["", "## 测量口径", "", "- 原版、轻量版均指定 4 个 worker，总线程预算及所有输入参数相同；每轮独立空缓存。",
              "- 默认分别使用已安装的原版、轻量版环境；解释器路径和依赖版本记入 JSON，不为测量修改原版环境。",
              "- 交替执行 original/light 与 light/original，报告中保留每次结果；中位数比较包含整个 CLI 生命周期。",
              "- 四并发必须由本次分段指标的 started_at/finished_at 证明 4 段同时运行；首尾相接不算重叠，缺失指标则待验。",
              "- RSS 为定期采样的同组进程 RSS 之和，可能重复计共享页，也可能漏过两次采样之间的峰值。",
              "- CER 做 NFKC、去空白和标点；不合并同音字，不改数字。多轮按最差轻量稿对最佳原版稿。",
              "- “分段完整”仅证明计划分段全部处理并导出，不能证明原录音没有漏识别。",
              "- 短合成样例仅验证流程；真实长录音、关键表达及说话人质量需另外验收。", ""]
    (folder / "验收报告.md").write_text("\n".join(lines), "utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--audio", type=Path, required=True)
    ap.add_argument("--source-kind", choices=("real", "synthetic", "unknown"), default="unknown")
    ap.add_argument("--reference", type=Path, help="UTF-8 逐字稿纯正文，不含标题、时间戳、说话人标签")
    ap.add_argument("--reference-kind", choices=("human", "synthetic", "unknown"), default="unknown")
    ap.add_argument("--original-model-dir", type=Path, required=True)
    ap.add_argument("--light-model-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--tx", type=Path, default=Path(__file__).with_name("tx.py"))
    app_dir = Path.home() / "Library/Application Support/录音转写工具"
    ap.add_argument("--original-python", default=str(app_dir / "venv/bin/python"), help="原版稳定环境解释器")
    ap.add_argument("--light-python", default=str(app_dir / "venv-light/bin/python"), help="轻量版独立环境解释器")
    ap.add_argument("--python", help="兼容选项：显式使用同一个解释器运行两引擎，覆盖上面两项")
    ap.add_argument("--threads", type=int, default=os.cpu_count() or 4, help="两引擎相同的总线程预算，默认实机逻辑核心数")
    ap.add_argument("--chunk-min", type=float, default=15)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=TARGET_WALL_SECONDS,
                    help="单轮进程最长运行秒数，默认 900；增大此值不会放宽 15 分钟验收门槛")
    ap.add_argument("--sample-interval", type=float, default=0.5)
    args = ap.parse_args(argv)
    if args.threads < 4 or args.repeats < 1 or any(not math.isfinite(v) or v <= 0 for v in (args.chunk_min, args.timeout, args.sample_interval)):
        ap.error("threads 至少为 4；repeats、chunk-min、timeout、sample-interval 必须为正数")
    args.audio = args.audio.expanduser().resolve(strict=True)
    args.tx = args.tx.expanduser().resolve(strict=True)
    pythons = {e: str(Path(args.python or getattr(args, e + "_python")).expanduser()) for e in ("original", "light")}
    for engine, executable in pythons.items():
        found = shutil.which(executable)
        if not found:
            ap.error(f"{engine} 解释器不可执行：{executable}")
        pythons[engine] = found
    interpreters = {e: interpreter_info(p) for e, p in pythons.items()}
    dirs = {e: getattr(args, e + "_model_dir").expanduser().resolve(strict=True) for e in ("original", "light")}
    reference = args.reference.expanduser().resolve(strict=True) if args.reference else None
    ref_text = reference.read_text("utf-8-sig") if reference else None
    reference_hash = sha256(reference) if reference else None
    if ref_text is not None:
        cer(ref_text, "")  # 在重任务前拒绝空参考稿。
    if args.reference_kind == "human" and reference is None:
        ap.error("声明人工逐字稿时必须提供 --reference")
    inventory = model_inventory(dirs["light"])
    hardware = hardware_info()
    source_hash = sha256(args.audio)
    args.out = args.out.expanduser().resolve()
    args.out.mkdir(parents=True, exist_ok=False)
    runs = []
    for repeat in range(args.repeats):
        for engine in (("original", "light") if repeat % 2 == 0 else ("light", "original")):
            folder = args.out / f"{repeat + 1:02d}-{engine}"
            command = [pythons[engine], str(args.tx), "run", str(args.audio), "-o", str(folder / "结果"),
                       "--engine", engine, "--workers", "4", "--threads", str(args.threads),
                       "--chunk-min", str(args.chunk_min), "--model-dir", str(dirs[engine]),
                       "--voiceprints", str(folder / "空声纹"), "--hotwords", str(folder / "无热词.txt")]
            print(f"第 {repeat + 1}/{args.repeats} 轮：{engine}（日志：{folder / '运行.log'}）", flush=True)
            result = run_case(command, folder, args.timeout, args.sample_interval)
            result.update({"engine": engine, "repeat": repeat + 1})
            if result["returncode"] == 0:
                try:
                    result["validation"] = validate_outputs(folder / "结果", args.audio)
                    if ref_text is not None:
                        result["cer"] = cer(ref_text, result["validation"]["transcript"])
                    result["validation"].pop("transcript", None)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    result["validation"] = {"status": "failed", "error": str(exc)}
            runs.append(result)
            report = evaluate(runs, inventory, hardware, args.source_kind, args.reference_kind,
                              sha256(args.audio) == source_hash)
            report.update({"source": {"path": str(args.audio), "sha256": source_hash, "kind": args.source_kind},
                           "reference": {"path": str(reference) if reference else None,
                                         "sha256": reference_hash, "kind": args.reference_kind},
                           "hardware": hardware, "interpreters": interpreters, "light_models": inventory, "runs": runs,
                           "configuration": {"workers": 4, "threads": args.threads, "chunk_min": args.chunk_min,
                                             "repeats": args.repeats, "model_dirs": {e: str(d) for e, d in dirs.items()}}})
            write_report(args.out, report)
            if result["returncode"] != 0 or result.get("validation", {}).get("status") != "passed":
                print(f"运行或结构检查失败，停止对照。见 {args.out / '验收报告.md'}", flush=True)
                return 1
    print(f"{report['conclusion']}。报告：{args.out / '验收报告.md'}", flush=True)
    return 1 if report["automatic_status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
