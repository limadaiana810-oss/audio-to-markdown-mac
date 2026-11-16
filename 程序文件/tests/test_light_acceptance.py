"""轻量验收的统计与证据门槛；不加载模型、不访问私人录音。"""
import copy
import importlib.util
import json
import io
from pathlib import Path
import tempfile
import unittest
import sys
from unittest.mock import patch


MODULE = Path(__file__).resolve().parents[1] / "accept_light.py"
spec = importlib.util.spec_from_file_location("accept_light", MODULE)
accept = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accept)


class CharacterErrorTests(unittest.TestCase):
    def test_normalization_preserves_meaningful_changes(self):
        self.assertEqual("明天不能付100元", accept.normalize_text(" 明天，不能付１００元。\n"))
        self.assertEqual(0, accept.cer("你，好。", "你好")['cer'])
        self.assertAlmostEqual(0.2, accept.cer("明天不能去", "明天能去")['cer'])
        self.assertEqual(1, accept.cer("12", "21")['cer'])

    def test_substitution_insertion_deletion_and_longer_hypothesis(self):
        self.assertEqual(1, accept.edit_distance("你好", "您好"))
        self.assertEqual(1, accept.edit_distance("你好", "你好啊"))
        self.assertEqual(1, accept.edit_distance("你好啊", "你好"))
        self.assertEqual(2, accept.cer("好", "好好好")['cer'])

    def test_empty_reference_cannot_be_quality_evidence(self):
        with self.assertRaises(ValueError):
            accept.cer("， \n", "识别内容")


class AcceptanceGateTests(unittest.TestCase):
    def setUp(self):
        self.hardware = {"chip": "Apple M5 Pro", "memory_bytes": 48 * 1024 ** 3}
        self.inventory = {"bytes": 700_000_000}
        self.runs = []
        for engine, seconds, error in (("original", 100, 0.1), ("light", 70, 0.105)):
            self.runs.append({"engine": engine, "returncode": 0, "wall_seconds": seconds,
                              "cer": {"cer": error}, "validation": {"status": "passed",
                              "actual_workers": 4, "worker_pids": [11, 12, 13, 14], "peak_concurrent_chunks": 4}})

    def evaluate(self, source="real", reference="human", unchanged=True):
        return accept.evaluate(self.runs, self.inventory, self.hardware, source, reference, unchanged)

    def test_exact_thresholds_pass_but_manual_checks_stay_pending(self):
        report = self.evaluate()
        self.assertEqual("pending", report["automatic_status"])
        self.assertEqual("pending", report["gates"]["absolute_accuracy"]["status"])
        self.assertEqual("pending", report["overall_status"])
        self.assertEqual(set(accept.MANUAL_ITEMS), set(report["manual_checks"]))
        self.assertTrue(all(v["status"] == "pending" for v in report["manual_checks"].values()))

    def test_half_percentage_point_is_not_relative_half_percent(self):
        self.runs[1]["cer"]["cer"] = 0.104
        self.assertEqual("passed", self.evaluate()["gates"]["cer"]["status"])
        self.runs[1]["cer"]["cer"] = 0.10501
        self.assertEqual("failed", self.evaluate()["gates"]["cer"]["status"])

    def test_thirty_percent_is_wall_time_reduction(self):
        self.runs[1]["wall_seconds"] = 100 / 1.30  # 1.3倍速度仅使耗时下降约23%
        self.assertEqual("failed", self.evaluate()["gates"]["speed"]["status"])

    def test_model_limit_is_decimal_mb(self):
        self.inventory["bytes"] += 1
        self.assertEqual("failed", self.evaluate()["gates"]["model_size"]["status"])

    def test_synthetic_and_unreviewed_reference_do_not_pass_real_quality(self):
        report = self.evaluate(source="synthetic", reference="synthetic")
        self.assertEqual("pending", report["gates"]["real_recording"]["status"])
        self.assertEqual("pending", report["gates"]["cer"]["status"])
        del self.runs[0]["cer"]
        self.assertEqual("pending", self.evaluate()["gates"]["cer"]["status"])

    def test_wrong_hardware_or_fewer_workers_do_not_pass_target_benchmark(self):
        self.hardware["chip"] = "Apple M4"
        self.runs[0]["validation"]["actual_workers"] = 1
        report = self.evaluate()
        self.assertEqual("pending", report["gates"]["target_hardware"]["status"])
        self.assertEqual("pending", report["gates"]["four_workers"]["status"])

    def test_four_worker_pids_without_four_overlapping_chunks_stays_pending(self):
        for overlap in (None, 1, 2, 3):
            with self.subTest(overlap=overlap):
                self.runs[1]["validation"]["peak_concurrent_chunks"] = overlap
                self.assertEqual("pending", self.evaluate()["gates"]["four_workers"]["status"])

    def test_source_mutation_and_failed_run_fail(self):
        self.assertEqual("failed", self.evaluate(unchanged=False)["overall_status"])
        self.runs[1]["returncode"] = 2
        self.assertEqual("failed", self.evaluate()["gates"]["run_and_structure"]["status"])
        self.assertEqual("pending", self.evaluate()["gates"]["speed"]["status"])

    def test_median_runtime_and_worst_quality_not_cherry_picked(self):
        originals, lights = [], []
        for seconds in (80, 100, 200):
            run = copy.deepcopy(self.runs[0]); run["wall_seconds"] = seconds; originals.append(run)
        for seconds in (10, 70, 90):
            run = copy.deepcopy(self.runs[1]); run["wall_seconds"] = seconds; lights.append(run)
        lights[0]["cer"]["cer"] = 0.106
        self.runs = originals + lights
        report = self.evaluate()
        self.assertEqual({"original": 100, "light": 70}, report["measurements"]["median_wall_seconds"])
        self.assertEqual("failed", report["gates"]["cer"]["status"])


class AbsoluteTargetTests(unittest.TestCase):
    def test_900_seconds_is_inclusive_and_short_sample_is_not_acceptance(self):
        self.assertEqual("passed", accept.target_speed_gate(36000, 900, True)["status"])
        self.assertEqual("failed", accept.target_speed_gate(36000, 900.001, True)["status"])
        self.assertEqual("pending", accept.target_speed_gate(35999, 100, True)["status"])

    def test_bad_values_or_unfinished_runs_cannot_pass(self):
        for value in (None, float("nan"), float("inf"), -1, 0, True):
            with self.subTest(value=value):
                self.assertEqual("failed", accept.target_speed_gate(36000, value, True)["status"])
                self.assertEqual("failed", accept.target_speed_gate(value, 100, True)["status"])
        self.assertEqual("failed", accept.target_speed_gate(36000, 100, False)["status"])

    def test_faster_than_slow_baseline_does_not_meet_absolute_target(self):
        runs = [{"engine": engine, "returncode": 0, "wall_seconds": seconds,
                 "cer": {"cer": error}, "validation": {"status": "passed", "audio_seconds": 36000,
                 "actual_workers": 4, "worker_pids": [1, 2, 3, 4], "peak_concurrent_chunks": 4}}
                for engine, seconds, error in (("original", 36000, .90), ("light", 25000, .905))]
        report = accept.evaluate(runs, {"bytes": 1}, {}, "real", "human", True)
        self.assertEqual("failed", report["gates"]["ten_hour_speed"]["status"])
        self.assertEqual("pending", report["gates"]["absolute_accuracy"]["status"])
        self.assertEqual("failed", report["overall_status"])


class ConcurrencyMeasurementTests(unittest.TestCase):
    @staticmethod
    def metrics(intervals):
        return [{"index": i, "worker_pid": i + 100, "started_at": start, "finished_at": end}
                for i, (start, end) in enumerate(intervals)]

    def test_four_real_overlapping_intervals(self):
        self.assertEqual(4, accept.peak_concurrent_chunks(self.metrics([(0, 3), (0.5, 2), (1, 4), (1.5, 5)])))

    def test_four_pids_running_sequentially_are_only_one_concurrent_chunk(self):
        self.assertEqual(1, accept.peak_concurrent_chunks(self.metrics([(0, 1), (1, 2), (2, 3), (3, 4)])))

    def test_shared_endpoint_does_not_artificially_increase_overlap(self):
        self.assertEqual(2, accept.peak_concurrent_chunks(self.metrics([(0, 2), (1, 3), (2, 4), (3, 5)])))

    def test_bad_or_missing_timestamps_cannot_prove_overlap(self):
        for end in (None, float("nan"), float("inf"), -1, 0):
            with self.subTest(end=end):
                self.assertIsNone(accept.peak_concurrent_chunks(self.metrics([(0, end)])))

    def test_same_worker_cannot_be_counted_twice_simultaneously(self):
        metrics = self.metrics([(0, 2), (1, 3)])
        metrics[1]["worker_pid"] = metrics[0]["worker_pid"]
        self.assertIsNone(accept.peak_concurrent_chunks(metrics))


class StructuralTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "中文 录音.wav"
        self.source.write_bytes(b"test fixture")
        self.out = self.root / "结果"
        self.result = self.out / self.source.stem
        self.work = self.result / ".work/source_123"
        self.work.mkdir(parents=True)
        (self.result / "分段").mkdir()
        self.outputs = ["目录.md", self.source.stem + ".srt", self.source.stem + "_全文.md", "分段/第一段.md"]
        for name in self.outputs:
            (self.result / name).write_text("fixture", "utf-8")
        (self.result / "待复核.md").write_text("待复核", "utf-8")
        self.write(self.result / "待复核.json", {"items": []})
        self.sentences = [{"id": "s000001", "start": 0.1, "end": 0.5, "text": "你好。", "speaker": "说话人1"},
                          {"id": "s000002", "start": 1.1, "end": 1.5, "text": "不可以。", "speaker": "说话人2"}]
        self.data = {"时长秒": 2, "句子": self.sentences}
        self.write(self.result / f"{self.source.stem}.json", self.data)
        self.write(self.result / ".work/completed.json", {"source": {"path": str(self.source)}, "outputs": self.outputs})
        self.write(self.work / "plan_abc.json", {"chunks": [[0, 1], [1, 2]], "total": 2})
        for i, s in enumerate(self.sentences):
            self.write(self.work / f"chunk_abc_{i:04d}.json", {"start": i, "end": i + 1,
                       "sentences": [s], "metrics": {"worker_pid": i + 10}})
        self.write(self.work / "stat.json", {"复用缓存分段数": 0, "实际转写进程数": 2,
                   "本次分段指标": [{"index": 0, "worker_pid": 10, "started_at": 100, "finished_at": 102},
                                    {"index": 1, "worker_pid": 11, "started_at": 101, "finished_at": 103}]})

    @staticmethod
    def write(path, data):
        path.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

    def validate(self):
        return accept.validate_outputs(self.out, self.source)

    def test_valid_outputs_retain_check_scope(self):
        report = self.validate()
        self.assertEqual("passed", report["status"])
        self.assertEqual("你好。不可以。", report["transcript"])
        self.assertEqual(2, report["chunk_count"])
        self.assertEqual(2, report["peak_concurrent_chunks"])
        self.assertIn("不等于", report["note"])

    def test_missing_or_partial_stat_timing_cannot_prove_parallel_execution(self):
        self.write(self.work / "stat.json", {"复用缓存分段数": 0, "实际转写进程数": 2,
                   "本次分段指标": [{"index": 0, "worker_pid": 10, "started_at": 100, "finished_at": 102}]})
        report = self.validate()
        self.assertIsNone(report["peak_concurrent_chunks"])
        self.assertFalse(report["inference_timing_complete"])

    def test_missing_chunk_rejected(self):
        (self.work / "chunk_abc_0001.json").unlink()
        with self.assertRaisesRegex(ValueError, "缺失"):
            self.validate()

    def test_existing_but_modified_output_fails_integrity_check(self):
        completion = self.result / ".work/completed.json"
        data = json.loads(completion.read_text())
        data["output_sha256"] = {name: accept.sha256(self.result / name) for name in self.outputs}
        self.write(completion, data)
        self.assertEqual("passed", self.validate()["status"])
        (self.result / self.outputs[0]).write_text("损坏但仍存在")
        with self.assertRaisesRegex(ValueError, "完整性校验失败"):
            self.validate()

    def test_missing_and_duplicated_exported_sentence_rejected(self):
        self.data["句子"] = self.sentences[:1]
        self.write(self.result / f"{self.source.stem}.json", self.data)
        with self.assertRaisesRegex(ValueError, "丢失、重复"):
            self.validate()
        self.data["句子"] = [{**s, "id": f"s{i:06d}"} for i, s in enumerate(
            [self.sentences[0]] * 2 + [self.sentences[1]], 1)]
        self.write(self.result / f"{self.source.stem}.json", self.data)
        with self.assertRaisesRegex(ValueError, "丢失、重复"):
            self.validate()

    def test_out_of_order_or_invalid_timestamps_rejected(self):
        self.data["句子"] = self.sentences[::-1]
        self.write(self.result / f"{self.source.stem}.json", self.data)
        with self.assertRaisesRegex(ValueError, "时间排序"):
            self.validate()
        self.data["句子"] = [{**self.sentences[0], "end": 3}]
        self.write(self.result / f"{self.source.stem}.json", self.data)
        with self.assertRaisesRegex(ValueError, "时间越界"):
            self.validate()

    def test_gap_in_plan_rejected(self):
        self.write(self.work / "plan_abc.json", {"chunks": [[0, 0.9], [1, 2]], "total": 2})
        with self.assertRaisesRegex(ValueError, "空隙"):
            self.validate()

    def test_cache_reuse_rejected_for_timing(self):
        self.write(self.work / "stat.json", {"复用缓存分段数": 1, "实际转写进程数": 2})
        with self.assertRaisesRegex(ValueError, "复用了"):
            self.validate()

    def test_sentence_ids_must_match_order(self):
        self.data["句子"][1]["id"] = "s000001"
        self.write(self.result / f"{self.source.stem}.json", self.data)
        with self.assertRaisesRegex(ValueError, "句子 id"):
            self.validate()

    def test_light_subdirectory_and_review_artifacts(self):
        light = self.out / "轻量试用"
        light.mkdir()
        self.result.rename(light / self.result.name)
        self.result = light / self.result.name
        (self.result / "待复核.json").unlink()
        with self.assertRaisesRegex(ValueError, "缺少输出"):
            self.validate()
        self.write(self.result / "待复核.json", {"items": []})
        (self.result / "待复核.md").write_text("待复核", "utf-8")
        self.assertEqual("passed", self.validate()["status"])

    def test_invalid_timestamp_review_text_cannot_disappear(self):
        review = {"start": 0, "end": 1, "reason": "时间戳无效", "text": "不可丢的原文"}
        path = self.work / "chunk_abc_0000.json"
        chunk = json.loads(path.read_text("utf-8")); chunk["review"] = [review]
        self.write(path, chunk)
        self.data["待复核"] = [review]
        self.write(self.result / f"{self.source.stem}.json", self.data)
        self.write(self.result / "待复核.json", {"items": [review]})
        self.assertEqual(1, self.validate()["review_item_count"])
        self.write(self.result / "待复核.json", {"items": []})
        with self.assertRaisesRegex(ValueError, "待复核问题或原文"):
            self.validate()

    def test_empty_chunk_allowed_not_claimed_to_be_silence(self):
        self.write(self.work / "chunk_abc_0001.json", {"start": 1, "end": 2, "sentences": [], "metrics": {}})
        self.data["句子"] = self.sentences[:1]
        self.write(self.result / f"{self.source.stem}.json", self.data)
        self.assertEqual([1], self.validate()["empty_chunks"])

    def test_inventory_follows_symlinked_models_without_double_counting(self):
        models = self.root / "models"; models.mkdir()
        shared = self.root / "shared"; shared.mkdir()
        (shared / "weights.bin").write_bytes(b"12345")
        (models / "linked-dir").symlink_to(shared, target_is_directory=True)
        (models / "linked-file").symlink_to(shared / "weights.bin")
        (shared / "cycle").symlink_to(models, target_is_directory=True)
        self.assertEqual(5, accept.model_inventory(models)["bytes"])


class ProcessMeasurementTests(unittest.TestCase):
    def test_subprocess_records_success_and_failure_without_models(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = accept.run_case([sys.executable, "-c", "print('测量烟测')"], root / "ok", 10, 0.01)
            self.assertEqual(0, result["returncode"])
            self.assertGreater(result["wall_seconds"], 0)
            self.assertGreater(result["rss_samples"], 0)
            self.assertIn("测量烟测", (root / "ok/运行.log").read_text("utf-8"))
            result = accept.run_case([sys.executable, "-c", "raise SystemExit(3)"], root / "fail", 10, 0.01)
            self.assertEqual(3, result["returncode"])
            self.assertTrue((root / "fail/计时.json").is_file())

    def test_cli_uses_separate_environments_and_common_override(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            audio = root / "sample.wav"; audio.write_bytes(b"fixture")
            original = root / "original-models"; original.mkdir()
            light = root / "light-models"; light.mkdir()
            (light / "model.onnx").write_bytes(b"fixture")
            commands = []

            def fake_run(command, folder, timeout, sample_interval):
                commands.append(command)
                engine = command[command.index("--engine") + 1]
                return {"returncode": 0, "wall_seconds": 100 if engine == "original" else 70}

            validation = {"status": "passed", "actual_workers": 4, "worker_pids": [1, 2, 3, 4],
                          "peak_concurrent_chunks": 4, "transcript": "你好"}
            args = ["--audio", str(audio), "--source-kind", "synthetic", "--original-model-dir", str(original),
                    "--light-model-dir", str(light), "--original-python", "/original-python",
                    "--light-python", "/light-python", "--repeats", "2"]
            with patch.object(accept.shutil, "which", side_effect=lambda value: value), \
                 patch.object(accept, "interpreter_info", side_effect=lambda value: {"executable": value}), \
                 patch.object(accept, "hardware_info", return_value={"chip": "Apple M5 Pro", "memory_bytes": 48 * 1024 ** 3}), \
                 patch.object(accept, "run_case", side_effect=fake_run), \
                 patch.object(accept, "validate_outputs", side_effect=lambda *a: dict(validation)), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(0, accept.main(args + ["--out", str(root / "separate")]))
                self.assertEqual(["/original-python", "/light-python", "/light-python", "/original-python"],
                                 [c[0] for c in commands])
                self.assertEqual(4, len({c[c.index("-o") + 1] for c in commands}))
                commands.clear()
                self.assertEqual(0, accept.main(args + ["--python", "/common-python", "--out", str(root / "common")]))
                self.assertEqual({"/common-python"}, {c[0] for c in commands})
            report = json.loads((root / "separate/验收报告.json").read_text("utf-8"))
            self.assertEqual("pending", report["overall_status"])
            self.assertEqual("/original-python", report["interpreters"]["original"]["executable"])


if __name__ == "__main__":
    unittest.main()
