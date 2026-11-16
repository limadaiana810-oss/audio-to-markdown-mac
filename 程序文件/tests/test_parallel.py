"""Real spawn scheduling tests with a tiny fake recognizer; no model downloads."""
import json
import multiprocessing
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parents[1]))
import tx


class TinyEngine:
    def __init__(self, delay=0):
        self.delay = delay

    def run_chunk(self, audio, offset, hotword):
        time.sleep(self.delay)
        index = int(offset)
        return ([{"start": offset + 0.05, "end": offset + 0.15,
                  "spk_local": 0, "text": f"第{index + 1}段"}],
                [[1.0, 0.0] if index % 2 == 0 else [0.0, 1.0]])


def tiny_worker(task_reader, result_writer, config, tasks):
    """Spawnable protocol double: real pipes, atomic caches and lifecycle."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if config.get("ignore_term"):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if config.get("worker_info"):
        Path(config["worker_info"], f"{os.getpid()}.json").write_text(json.dumps({
            "threads": config["threads"], "omp_threads": os.environ["OMP_NUM_THREADS"]}))
    while True:
        try:
            task_id = task_reader.recv()
        except EOFError:
            return
        if task_id is None:
            return
        task = tasks[task_id]
        if task["kind"] == "enroll":
            result_writer.send({"kind": "enroll", "people": {"我": np.array([1.0, 0.0])}})
            continue
        index = task["index"]
        if index == config.get("fail_index"):
            wait_for = config.get("wait_for_cache")
            deadline = time.monotonic() + 5
            while wait_for and not Path(wait_for).exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            if config.get("hard_exit"):
                os._exit(17)
            result_writer.send({"kind": "error", "pid": os.getpid(), "index": index,
                                "error": "模拟识别失败"})
            return
        delay = config.get("delays", {}).get(str(index), config.get("delay", 0.03))
        tx.transcribe_chunk(TinyEngine(delay), task)
        if index == config.get("exit_after_cache"):
            os._exit(19)
        result_writer.send({"kind": "chunk", "index": index})


def task_fixture(root, count):
    root = Path(root)
    wav = root / "测试录音.wav"
    sf.write(wav, np.full(tx.SR * count, 0.1), tx.SR, subtype="PCM_16")
    return [{"kind": "chunk", "index": i, "total": count,
             "start": i, "end": i + 1, "wav": str(wav),
             "cache": str(root / f"chunk_{i}.json"), "hotword": ""}
            for i in range(count)]


def cancel_probe(root):
    root = Path(root)
    tasks = task_fixture(root, 8)
    try:
        tx.run_parallel_tasks(tasks, {"threads": 10, "delay": 60,
                              "worker_info": str(root / "pids"), "ignore_term": True},
                              4, worker_target=tiny_worker)
    except KeyboardInterrupt:
        (root / "cancelled").write_text("cleaned")
        raise SystemExit(130)


class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.args = SimpleNamespace(chunk_min=15, hotword_text="", asr_model="auto",
                                    model_dir=None, max_spk=10, no_norm=False,
                                    merge_thr=0.62, enroll_thr=0.5, part_min=30,
                                    keep_work=True, force=False, workers=4,
                                    threads=10, device="cpu", voiceprints=str(self.root / "声纹"))
        self.config = {}
        self.real_runner = tx.run_parallel_tasks

    def assert_reaped(self, pids):
        for pid in pids:
            with self.assertRaises(ProcessLookupError, msg=f"child {pid} survived cleanup"):
                os.kill(pid, 0)

    def process_fixture(self):
        src = self.root / "录音.wav"
        if not src.exists():
            sf.write(src, np.full(tx.SR * 4, 0.1), tx.SR, subtype="PCM_16")

        def decode(source, destination, norm=False):
            if not destination.exists():
                audio, _ = sf.read(source)
                sf.write(destination, audio, tx.SR, subtype="PCM_16")

        def run(tasks, config, workers):
            extras = dict(self.config)
            if extras.pop("wait_first", False):
                extras["wait_for_cache"] = tasks[0]["cache"]
            return self.real_runner(tasks, {**config, **extras}, workers, worker_target=tiny_worker)

        with patch.object(tx, "decode_to_wav", side_effect=decode), \
             patch.object(tx, "plan_chunks", return_value=([(i, i + 1) for i in range(4)], 4)), \
             patch.object(tx, "run_parallel_tasks", side_effect=run):
            return tx.process_file(None, src, self.root / "结果", self.args,
                                   {"甲": np.array([1.0, 0.0]), "乙": np.array([0.0, 1.0])})

    def test_out_of_order_completion_keeps_offsets_and_speakers_in_order(self):
        self.config = {"delays": {"0": 0.8, "1": 0.3, "2": 0.2, "3": 0.1}}
        out = self.process_fixture()
        data = json.loads((out / "录音.json").read_text())
        self.assertEqual([0.05, 1.05, 2.05, 3.05], [s["start"] for s in data["句子"]])
        self.assertEqual(["甲", "乙", "甲", "乙"], [s["speaker"] for s in data["句子"]])
        self.assertEqual(["第1段", "第2段", "第3段", "第4段"], [s["text"] for s in data["句子"]])
        metrics = [json.loads(p.read_text())["metrics"] for p in sorted((out / ".work").rglob("chunk_*.json"))]
        finish_order = [m["index"] for m in sorted(metrics, key=lambda m: m["finished_at"])]
        self.assertNotEqual([0, 1, 2, 3], finish_order)
        self.assertEqual(4, len({m["worker_pid"] for m in metrics}))
        self.assertLess(max(m["started_at"] for m in metrics), min(m["finished_at"] for m in metrics))

    def test_worker_limit_and_cpu_budget(self):
        info = self.root / "pids"
        info.mkdir()
        result = tx.run_parallel_tasks(task_fixture(self.root, 8),
                                      {"threads": 10, "delay": 0.2, "worker_info": str(info)},
                                      99, worker_target=tiny_worker)
        self.assertEqual(4, result["workers"])
        self.assertEqual(4, len(list(info.glob("*.json"))))
        for p in info.glob("*.json"):
            self.assertEqual({"threads": 2, "omp_threads": "2"}, json.loads(p.read_text()))
        self.assert_reaped([int(p.stem) for p in info.glob("*.json")])

    def test_single_pending_chunk_starts_one_worker(self):
        result = tx.run_parallel_tasks(task_fixture(self.root, 1), {"threads": 10}, 4,
                                      worker_target=tiny_worker)
        self.assertEqual(1, result["workers"])
        self.assertEqual(1, len(result["metrics"]))

    def test_cpu_budget_also_limits_worker_count(self):
        result = tx.run_parallel_tasks(task_fixture(self.root, 4), {"threads": 1}, 4,
                                      worker_target=tiny_worker)
        self.assertEqual(1, result["workers"])
        self.assertEqual(1, len({m["worker_pid"] for m in result["metrics"]}))

    def test_repeated_abort_with_large_hotwords_leaves_no_feeder_threads(self):
        tasks = task_fixture(self.root, 4)
        for task in tasks:
            task["hotword"] = "长热词" * 100000
        before = {t.ident for t in threading.enumerate()}
        for _ in range(2):
            with self.assertRaisesRegex(RuntimeError, "异常退出"):
                tx.run_parallel_tasks(tasks, {"threads": 10, "fail_index": 0, "hard_exit": True},
                                      4, worker_target=tiny_worker)
        self.assertEqual(before, {t.ident for t in threading.enumerate()})

    def test_no_pending_tasks_starts_no_process(self):
        with patch.object(tx.multiprocessing, "get_context", side_effect=AssertionError("unexpected spawn")):
            self.assertEqual(0, tx.run_parallel_tasks([], {"threads": 10}, 4)["workers"])

    def test_failure_keeps_completed_cache_and_retry_only_runs_missing_chunks(self):
        self.config = {"fail_index": 1, "wait_first": True}
        with self.assertRaisesRegex(RuntimeError, "模拟识别失败"):
            self.process_fixture()
        src = self.root / "录音.wav"
        self.assertFalse(tx.is_completed(src, self.root / "结果"))
        cache = next((self.root / "结果").rglob("chunk_*_0000.json"))
        old_mtime = cache.stat().st_mtime_ns
        self.config = {}
        out = self.process_fixture()
        self.assertEqual(old_mtime, cache.stat().st_mtime_ns)
        self.assertTrue(tx.is_completed(src, self.root / "结果"))
        stat = json.loads(next((out / ".work").rglob("stat.json")).read_text())
        self.assertGreaterEqual(stat["复用缓存分段数"], 1)
        self.assertLessEqual(stat["实际转写进程数"], 3)

    def test_worker_hard_exit_does_not_hang_and_cache_survives(self):
        tasks = task_fixture(self.root, 4)
        info = self.root / "pids"
        info.mkdir()
        started = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "异常退出"):
            tx.run_parallel_tasks(tasks, {"threads": 10, "fail_index": 1, "hard_exit": True,
                                  "wait_for_cache": tasks[0]["cache"], "worker_info": str(info)},
                                  4, worker_target=tiny_worker)
        self.assertLess(time.monotonic() - started, 8)
        self.assertTrue(Path(tasks[0]["cache"]).exists())
        self.assert_reaped([int(p.stem) for p in info.glob("*.json")])

    def test_exit_after_cache_before_notification_keeps_atomic_result(self):
        task = task_fixture(self.root, 1)[0]
        with self.assertRaisesRegex(RuntimeError, "异常退出"):
            tx.run_parallel_tasks([task], {"threads": 10, "exit_after_cache": 0}, 4,
                                  worker_target=tiny_worker)
        self.assertEqual("第1段", json.loads(Path(task["cache"]).read_text())["sentences"][0]["text"])

    def test_start_failure_reaps_already_started_children(self):
        original = multiprocessing.context.SpawnProcess.start
        pids = []

        def start(process):
            if pids:
                raise OSError("模拟第二个进程启动失败")
            original(process)
            pids.append(process.pid)

        with patch.object(multiprocessing.context.SpawnProcess, "start", start):
            with self.assertRaisesRegex(OSError, "启动失败"):
                tx.run_parallel_tasks(task_fixture(self.root, 4), {"threads": 10}, 4,
                                      worker_target=tiny_worker)
        self.assert_reaped(pids)

    def test_voiceprint_task_runs_once_and_returns_people(self):
        result = tx.run_parallel_tasks([{"kind": "enroll", "path": str(self.root)}],
                                      {"threads": 10}, 4, worker_target=tiny_worker)
        self.assertEqual(1, result["workers"])
        np.testing.assert_array_equal(result["people"]["我"], [1, 0])

    def test_full_cache_force_does_not_load_models_or_decode(self):
        self.process_fixture()
        self.args.force = True
        with patch.object(tx, "make_engine", side_effect=AssertionError("model loaded")), \
             patch.object(tx, "run_parallel_tasks", side_effect=AssertionError("pool started")), \
             patch.object(tx, "decode_to_wav", side_effect=AssertionError("audio decoded")):
            tx.process_file(None, self.root / "录音.wav", self.root / "结果", self.args, {})

    def test_ctrl_c_and_second_ctrl_c_reap_all_children(self):
        info = self.root / "pids"
        info.mkdir()
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).parent)}
        process = subprocess.Popen([sys.executable, "-c",
                                    "import sys, test_parallel; test_parallel.cancel_probe(sys.argv[1])",
                                    str(self.root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, env=env)
        try:
            deadline = time.monotonic() + 8
            while len(list(info.glob("*.json"))) < 4 and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.03)
            self.assertEqual(4, len(list(info.glob("*.json"))))
            process.send_signal(signal.SIGINT)
            time.sleep(0.2)
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=8)
            self.assertEqual(130, process.returncode, stdout + stderr)
            self.assertTrue((self.root / "cancelled").exists())
            self.assert_reaped([int(p.stem) for p in info.glob("*.json")])
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=3)
            for p in info.glob("*.json"):
                try:
                    os.kill(int(p.stem), signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    unittest.main()
