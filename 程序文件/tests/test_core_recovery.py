"""Recovery and batch regressions: tiny WAVs and fake recognizers, no models."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parents[1]))
import tx
from test_parallel import TinyEngine, tiny_worker


def resident_worker(reader, writer, config, tasks):
    """Exercise production worker lazy initialization using a model-free engine."""
    def make(config):
        marker = Path(config["engine_events"]) / f"{os.getpid()}.txt"
        with marker.open("a") as stream:
            stream.write("initialized\n")
        return TinyEngine(.35)
    tx.make_engine = make
    tx.parallel_worker(reader, writer, config, tasks)


class FakeEngine:
    def __init__(self):
        self.calls = []

    def run_chunk(self, audio, offset, hotword):
        self.calls.append(offset)
        return ([{"start": offset + .05, "end": offset + .2,
                  "spk_local": 0, "text": "测试"}], [[1., 0.]])


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.src = self.root / "录音.wav"
        sf.write(self.src, np.full(tx.SR * 2, .1), tx.SR, subtype="PCM_16")
        self.out = self.root / "结果"
        self.args = SimpleNamespace(engine="original", chunk_min=1, hotword_text="",
            asr_model="auto", model_dir=None, max_spk=10, no_norm=True,
            merge_thr=.62, enroll_thr=.5, part_min=30, keep_work=True,
            force=False, workers=1, threads=4, device="cpu", voiceprints=str(self.root / "vp"))
        self.engine = FakeEngine()
        self.addCleanup(patch.stopall)
        patch.object(tx, "plan_chunks", return_value=([(0, 1), (1, 2)], 2)).start()
        patch.object(tx, "decode_to_wav", side_effect=self.decode).start()

    @staticmethod
    def decode(src, dst, norm=False):
        if not dst.exists():
            data, _ = sf.read(src)
            sf.write(dst, data, tx.SR, subtype="PCM_16")

    def process(self):
        return tx.process_file(self.engine, self.src, self.out, self.args, {})

    def test_zero_length_final_json_is_reexported_without_asr(self):
        out = self.process()
        (out / "录音.json").write_text("")
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))
        self.assertEqual(0, tx.run_files(self.engine, [self.src], self.out, self.args, {}))
        self.assertEqual(2, len(self.engine.calls))
        self.assertEqual(2, len(json.loads((out / "录音.json").read_text())["句子"]))

    def test_bad_chunk_recomputes_only_its_own_segment(self):
        out = self.process()
        caches = sorted((out / ".work").rglob("chunk_*.json"))
        good_before = caches[0].read_bytes(), caches[0].stat().st_mtime_ns
        caches[1].write_text("{broken")
        self.process()
        self.assertEqual([0, 1, 1], self.engine.calls)
        self.assertEqual(good_before, (caches[0].read_bytes(), caches[0].stat().st_mtime_ns))

    def test_bad_plan_rebuilds_and_keeps_matching_chunks(self):
        out = self.process()
        next((out / ".work").rglob("plan_*.json")).write_text("{broken")
        self.process()
        self.assertEqual([0, 1], self.engine.calls)

    def test_default_and_explicit_minutes_have_the_same_serialized_fingerprint(self):
        self.args.chunk_min = 15
        default = tx.chunk_params(self.args)
        self.assertIs(type(default["chunk_min"]), int)
        self.args.chunk_min = 15.0
        self.assertEqual(json.dumps(default, sort_keys=True),
                         json.dumps(tx.chunk_params(self.args), sort_keys=True))
        self.args.chunk_min = 15.25
        self.assertEqual(15.25, tx.chunk_params(self.args)["chunk_min"])

    def test_equivalent_explicit_minutes_reuse_healthy_chunks(self):
        self.process()
        self.args.chunk_min = float(self.args.chunk_min)
        self.process()
        self.assertEqual([0, 1], self.engine.calls)

    def test_invalid_json_shapes_and_nan_recover_only_bad_chunk(self):
        out = self.process()
        caches = sorted((out / ".work").rglob("chunk_*.json"))
        good = caches[0].read_bytes()
        for broken in ("[]", "null", "{}", '{"start":NaN,"end":2}',
                       '{"review": 7}', '{"sentences": [], "centers": null}'):
            with self.subTest(broken=broken):
                caches[1].write_text(broken)
                before = len(self.engine.calls)
                self.process()
                self.assertEqual(before + 1, len(self.engine.calls))
                self.assertEqual(good, caches[0].read_bytes())

    def test_legacy_completion_without_hash_reexports_healthy_cache(self):
        out = self.process()
        marker = out / ".work/completed.json"
        data = json.loads(marker.read_text())
        data.pop("output_sha256")
        marker.write_text(json.dumps(data))
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))
        self.assertEqual(0, tx.run_files(self.engine, [self.src], self.out, self.args, {}))
        self.assertEqual(2, len(self.engine.calls))
        self.assertTrue(tx.is_completed(self.src, self.out, self.args))

    def test_checksum_detects_valid_shape_text_tampering(self):
        out = self.process()
        cache = sorted((out / ".work").rglob("chunk_*.json"))[1]
        data = json.loads(cache.read_text())
        data["sentences"][0]["text"] = "悄悄改错"
        cache.write_text(json.dumps(data))
        self.process()
        self.assertEqual([0, 1, 1], self.engine.calls)

    def test_duplicate_task_lock_preserves_existing_outputs(self):
        out = self.process()
        original = (out / "录音.json").read_bytes()
        lock = tx.acquire_job_lock(self.src, self.out, self.args)
        try:
            with self.assertRaisesRegex(RuntimeError, "另一个任务"):
                self.process()
        finally:
            tx.release_job_lock(lock)
        self.assertEqual(original, (out / "录音.json").read_bytes())
        self.assertTrue(tx.is_completed(self.src, self.out, self.args))

    def test_atomic_writers_use_unique_temporary_names(self):
        target = self.root / "共享.json"
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda n: tx.write_json_atomic(target, {"n": n, "data": "字" * 10000}), range(64)))
        self.assertEqual("字" * 10000, tx.read_json(target)["data"])
        self.assertEqual([], list(self.root.glob("*.tmp")))

    def test_output_volume_space_check_preserves_source(self):
        original = self.src.read_bytes()
        with patch.object(tx.shutil, "disk_usage", return_value=SimpleNamespace(free=1)) as usage:
            with self.assertRaisesRegex(RuntimeError, "磁盘空间不足"):
                tx.check_decode_space(self.src, self.root / "decoded.wav")
        usage.assert_called_once_with(self.root)
        self.assertEqual(original, self.src.read_bytes())

    def test_explicit_missing_cli_input_exits_two_without_model_load(self):
        cli = Path(tx.__file__)
        result = subprocess.run([sys.executable, str(cli), "run", str(self.root / "missing.wav"),
                                 "-o", str(self.out)], capture_output=True, text=True)
        self.assertEqual(2, result.returncode, result.stderr)
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual("failed", batch["status"])
        self.assertEqual("failed", batch["files"][0]["status"])

    def test_mixed_valid_and_missing_batch_reports_failure(self):
        self.assertEqual(1, tx.run_files(self.engine, [self.src, self.root / "missing.wav"],
                                        self.out, self.args, {}))
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual(["completed", "failed"], [item["status"] for item in batch["files"]])

    def test_cli_completed_file_plus_missing_input_exits_two(self):
        self.process()
        command = [sys.executable, tx.__file__, "run", str(self.src), str(self.root / "missing.wav"),
                   "-o", str(self.out), "--engine", "original", "--chunk-min", "1",
                   "--device", "cpu", "--no-norm", "--voiceprints", self.args.voiceprints,
                   "--hotwords", str(self.root / "no-hotwords.txt"), "--workers", "1"]
        environment = {k: v for k, v in os.environ.items() if k != "TX_MODEL_DIR"}
        result = subprocess.run(command, capture_output=True, text=True, env=environment)
        self.assertEqual(2, result.returncode, result.stderr)
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual(["skipped", "failed"], [e["status"] for e in batch["files"]])

    def test_cross_file_pool_uses_four_resident_workers_and_stable_order(self):
        self.args.workers = 4
        sources = []
        for i in (5, 3, 1, 7, 6, 4, 2, 0):
            src = self.root / f"录音{i}.wav"
            src.write_bytes(self.src.read_bytes())
            sources.append(src)
        runner = tx.run_parallel_tasks
        calls = []
        events_dir = self.root / "载模次数"
        events_dir.mkdir()
        def run(tasks, config, workers, **callbacks):
            calls.append(len(tasks))
            return runner(tasks, {**config, "engine_events": str(events_dir)}, workers,
                          worker_target=resident_worker, **callbacks)
        with patch.object(tx, "plan_chunks", return_value=([(0, 2)], 2)), \
             patch.object(tx, "run_parallel_tasks", side_effect=run):
            self.assertEqual(0, tx.run_files(None, sources, self.out, self.args, {}))
        self.assertEqual([8], calls)
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual([str(s.resolve()) for s in sources], [e["source"] for e in batch["files"]])
        self.assertEqual(4, len(batch["worker_pids"]))
        self.assertEqual(4, len(list(events_dir.glob("*.txt"))))
        for marker in events_dir.glob("*.txt"):
            self.assertEqual(["initialized"], marker.read_text().splitlines())
        events = sorted([(m["started_at"], 1) for m in batch["metrics"]] +
                        [(m["finished_at"], -1) for m in batch["metrics"]])
        active = peak = 0
        for _, delta in events:
            active += delta
            peak = max(peak, active)
        self.assertEqual(4, peak)
        for entry in batch["files"]:
            out = Path(entry["result"])
            data = tx.read_json(out / (Path(entry["source"]).stem + ".json"))
            self.assertEqual(.05, data["句子"][0]["start"])
            stat = tx.read_json(next((out / ".work").rglob("stat.json")))
            self.assertEqual(1, stat["实际转写进程数"])
            self.assertEqual(1, len(stat["本次分段指标"]))

    def test_pool_failure_still_exports_other_complete_file(self):
        self.args.workers = 4
        first = self.root / "短录音.wav"
        sf.write(first, np.full(tx.SR, .1), tx.SR)
        runner = tx.run_parallel_tasks
        def plan(wav, seconds):
            count = round(sf.info(str(wav)).duration)
            return [(i, i + 1) for i in range(count)], count
        def run(tasks, config, workers, **callbacks):
            return runner(tasks, {**config, "fail_index": 1, "wait_for_cache": tasks[0]["cache"]},
                          workers, worker_target=tiny_worker, **callbacks)
        with patch.object(tx, "plan_chunks", side_effect=plan), \
             patch.object(tx, "run_parallel_tasks", side_effect=run):
            self.assertEqual(1, tx.run_files(None, [first, self.src], self.out, self.args, {}))
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual(["completed", "failed"], [e["status"] for e in batch["files"]])
        self.assertTrue(tx.is_completed(first, self.out, self.args))
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))

    def test_interruption_marks_batch_and_releases_all_locks(self):
        second = self.root / "第二份.wav"
        second.write_bytes(self.src.read_bytes())
        with patch.object(tx, "execute_tasks", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                tx.run_files(self.engine, [self.src, second], self.out, self.args, {})
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual("interrupted", batch["status"])
        self.assertEqual(["interrupted", "interrupted"], [e["status"] for e in batch["files"]])
        for src in (self.src, second):
            lock = tx.acquire_job_lock(src, self.out, self.args)
            tx.release_job_lock(lock)

    def test_prepare_failure_releases_its_lock_before_other_jobs_execute(self):
        second = self.root / "第二份.wav"
        second.write_bytes(self.src.read_bytes())
        prepare, execute = tx.prepare_file, tx.execute_tasks
        def fail_first(src, out, args):
            if src.resolve() == self.src.resolve():
                raise RuntimeError("转码失败")
            return prepare(src, out, args)
        def check_lock(*args):
            lock = tx.acquire_job_lock(self.src, self.out, self.args)
            tx.release_job_lock(lock)
            return execute(*args)
        with patch.object(tx, "prepare_file", side_effect=fail_first), \
             patch.object(tx, "execute_tasks", side_effect=check_lock):
            self.assertEqual(1, tx.run_files(self.engine, [self.src, second], self.out, self.args, {}))

    def test_watch_submits_all_stable_files_as_one_batch(self):
        second = self.root / "第二份.wav"
        second.write_bytes(self.src.read_bytes())
        sleeps = 0
        def tick(_):
            nonlocal sleeps
            sleeps += 1
            if sleeps == 2:
                raise KeyboardInterrupt
        argv = ["tx", "watch", str(self.src), str(second), "-o", str(self.out),
                "--engine", "original", "--voiceprints", self.args.voiceprints,
                "--hotwords", str(self.root / "missing-hotwords")]
        with patch.object(tx.sys, "argv", argv), patch.object(tx.time, "sleep", side_effect=tick), \
             patch.object(tx, "run_files", return_value=0) as batches:
            with self.assertRaises(KeyboardInterrupt):
                tx.main()
        self.assertEqual(1, batches.call_count)
        self.assertEqual([self.src, second], batches.call_args.args[1])

    def test_structurally_valid_but_out_of_bounds_chunk_is_recomputed(self):
        out = self.process()
        cache = sorted((out / ".work").rglob("chunk_*.json"))[1]
        data = json.loads(cache.read_text())
        data["sentences"][0]["end"] = 999
        cache.write_text(json.dumps(data))
        self.process()
        self.assertEqual([0, 1, 1], self.engine.calls)

    def test_failure_preserves_existing_detailed_review(self):
        out = self.process()
        detail = {"start": .1, "end": .2, "text": "保留原文", "reason": "时间不确定"}
        tx.write_review(out, [detail])
        with patch.object(tx, "global_speakers", side_effect=RuntimeError("故障注入")):
            with self.assertRaises(RuntimeError):
                self.process()
        items = tx.read_json(out / "待复核.json")["items"]
        self.assertIn(detail, items)
        self.assertTrue(any("故障注入" in item["reason"] for item in items))

    def test_auto_speaker_does_not_collide_with_registered_name(self):
        chunks = [{"sentences": [{"start": 0, "end": 1, "spk_local": 0},
                                   {"start": 1, "end": 2, "spk_local": 1}],
                   "centers": [[1, 0], [0, 1]]}]
        mapping, summary, _ = tx.global_speakers(chunks, .62, {"说话人1": np.array([1., 0.])}, .5)
        self.assertNotEqual(mapping[(0, 0)], mapping[(0, 1)])
        self.assertEqual(2, len(summary))

    def test_zero_center_does_not_break_valid_speaker(self):
        chunks = [{"sentences": [{"start": 0, "end": 1, "spk_local": 0},
                                   {"start": 1, "end": 2, "spk_local": 1}],
                   "centers": [[1, 0], [0, 0]]}]
        mapping, _, _ = tx.global_speakers(chunks, .62, {}, .5)
        self.assertEqual("说话人未确定", mapping[(0, 1)])

    def test_paragraph_merge_does_not_move_text_to_previous_part(self):
        out = self.root / "导出"
        sents = [{"start": 1799, "end": 1800, "speaker": "甲", "text": "前半段"},
                 {"start": 1801, "end": 1802, "speaker": "甲", "text": "后半段"}]
        tx.write_outputs(out, "录音", sents, {}, 3600, 30, {"source": "source", "params": {}})
        parts = sorted((out / "分段").glob("*.md"))
        self.assertNotIn("后半段", parts[0].read_text())
        self.assertIn("后半段", parts[1].read_text())


if __name__ == "__main__":
    unittest.main()
