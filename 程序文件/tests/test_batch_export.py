"""Batch Markdown exports: provenance, ordering, recovery and real run_files flow."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import unquote

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parents[1]))
import batch_export
import tx


class BatchExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.out = self.root / "结果"
        self.out.mkdir()
        self.entries = []

    def add_recording(self, filename, text="这是原始文字。", status="completed", parent="录音"):
        source = self.root / parent / filename
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"fixture audio")
        result = self.out / f"result-{len(self.entries)}"
        result.mkdir()
        body = f"**[00:00:01] 说话人1**：{text}\n" if text else ""
        sentences = ([dict(start=1., end=2., text=text, speaker="说话人1", id="s000001")]
                     if text else [])
        payload = {"文件": str(source), "时长秒": 5., "句子": sentences, "待复核": []}
        (result / (source.stem + ".json")).write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        (result / (source.stem + "_全文.md")).write_text(f"# {source.stem} 全文\n\n{body}", encoding="utf-8")
        (result / "待复核.md").write_text("# 待复核\n\n无待复核项。\n", encoding="utf-8")
        (result / "待复核.json").write_text('{"items": []}', encoding="utf-8")
        entry = dict(index=len(self.entries), source=str(source), result=str(result), status=status)
        self.entries.append(entry)
        self.seal(entry)
        return entry, body

    @staticmethod
    def seal(entry):
        source, result = Path(entry["source"]), Path(entry["result"])
        st = source.stat()
        files = [source.stem + ".json", source.stem + "_全文.md", "待复核.md", "待复核.json"]
        marker = {"source": dict(path=str(source.resolve()), size=st.st_size,
            mtime_ns=st.st_mtime_ns, ctime_ns=st.st_ctime_ns, device=st.st_dev, inode=st.st_ino),
            "outputs": files, "output_sha256": {name: hashlib.sha256((result / name).read_bytes()).hexdigest()
                                                  for name in files}}
        (result / ".work").mkdir(exist_ok=True)
        (result / ".work/completed.json").write_text(json.dumps(marker), encoding="utf-8")

    def export(self, batch_id="batch-1"):
        batch = dict(files=self.entries, index_path=str(self.out / ".批次" / (batch_id + ".json")),
                     engine="light", started_at=1794460699.0)
        return batch_export.export_batch_markdown(batch, self.out)

    def test_recordings_sort_by_filename_time_not_selection_or_modification_time(self):
        late, _ = self.add_recording("TX00_MIC002_20261112_103153_orig.wav", "后面的录音")
        early, _ = self.add_recording("TX00_MIC003_20261112_100152_orig.wav", "前面的录音")
        os.utime(early["source"], (2_000_000_000, 2_000_000_000))
        self.seal(early)
        result = self.export()
        self.assertEqual("completed", result["status"])
        self.assertEqual([early["source"], late["source"]], result["ordered_sources"])
        text = Path(result["path"]).read_text()
        self.assertLess(text.index("前面的录音"), text.index("后面的录音"))
        self.assertIn("2026-11-12 10:01:52", text)
        self.assertIn("文件名", text)

    def test_unknown_and_invalid_dates_are_explicit_and_keep_input_order_after_dated_files(self):
        unknown, _ = self.add_recording("没有日期.wav", "无日期内容")
        invalid, _ = self.add_recording("录音_20260230_093000.wav", "非法日期内容")
        dated, _ = self.add_recording("录音_2026-11-12_09-30-00.wav", "有效日期内容")
        result = self.export()
        self.assertEqual([dated["source"], unknown["source"], invalid["source"]], result["ordered_sources"])
        self.assertIn("录音时间未知", Path(result["path"]).read_text())

    def test_keeps_entire_original_body_local_timestamps_and_source_links(self):
        first, body = self.add_recording("会议 #一 (确认)_20261112_100000.wav", "否定词：不要取消，金额是 17.5 元。")
        second, _ = self.add_recording("会议_20261112_110000.wav", "后续内容")
        result_dir = Path(first["result"])
        stem = Path(first["source"]).stem
        extra = "\n**[00:00:03] 说话人2**：确认保留原有 [备注] 和数字 100。\n"
        full = result_dir / (stem + "_全文.md")
        full.write_text(full.read_text() + extra, encoding="utf-8")
        data_file = result_dir / (stem + ".json")
        data = json.loads(data_file.read_text())
        data["句子"].append(dict(start=3., end=4., speaker="说话人2",
                                text="确认保留原有 [备注] 和数字 100。", id="s000002"))
        data["待复核"] = [dict(start=3., end=4., reason="需核对说话人", code="speaker_uncertain")]
        data_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (result_dir / "待复核.json").write_text(json.dumps({"items": data["待复核"]}), encoding="utf-8")
        self.seal(first)
        result = self.export()
        text = Path(result["path"]).read_text()
        self.assertIn("[00:00:01] 说话人1：否定词：不要取消，金额是 17.5 元。\n", text)
        self.assertIn("[00:00:03] 说话人2：确认保留原有 [备注] 和数字 100。\n", text)
        self.assertNotIn("**[00:00:01] 说话人1**", text)
        decoded = unquote(text)
        self.assertIn(first["source"], decoded)
        self.assertIn(second["source"], decoded)
        self.assertIn("相对", text)
        self.assertNotIn("待复核项", text)
        self.assertNotIn("待确认内容", text)
        self.assertEqual(f"# {stem} 全文\n\n" + body + extra, full.read_text())

    def test_all_skipped_files_still_generate_merged_document(self):
        self.add_recording("a_20261112_100000.wav", status="skipped")
        self.add_recording("b_20261112_110000.wav", status="skipped")
        result = self.export()
        self.assertEqual("completed", result["status"])
        self.assertEqual(2, result["included_count"])
        self.assertEqual(Path(result["path"]).read_bytes(), (self.out / "合并全文.md").read_bytes())

    def test_failed_input_is_reported_and_cannot_replace_previous_complete_latest(self):
        self.add_recording("a_20261112_100000.wav", "已经完成")
        self.entries.append(dict(index=1, source=str(self.root / "missing.wav"), result=None,
                                 status="failed", error="录音文件不存在"))
        latest = self.out / "合并全文.md"
        latest.write_text("previous complete batch", encoding="utf-8")
        result = self.export()
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["included_count"])
        self.assertEqual(1, len(result["missing"]))
        self.assertEqual("previous complete batch", latest.read_text())
        text = Path(result["path"]).read_text()
        self.assertIn("已经完成", text)
        self.assertIn("missing.wav", unquote(text))
        self.assertIn("部分", text)

    def test_corrupt_or_missing_export_never_claims_complete(self):
        first, _ = self.add_recording("a_20261112_100000.wav")
        self.add_recording("b_20261112_110000.wav")
        result_dir, stem = Path(first["result"]), Path(first["source"]).stem
        cases = [(result_dir / (stem + ".json"), b"{broken"),
                 (result_dir / (stem + "_全文.md"), b""),
                 (result_dir / "待复核.json", None)]
        for path, data in cases:
            with self.subTest(path=path.name):
                before = path.read_bytes()
                if data is None:
                    path.unlink()
                else:
                    path.write_bytes(data)
                result = self.export()
                self.assertEqual("partial", result["status"])
                self.assertFalse((self.out / "合并全文.md").exists())
                path.write_bytes(before)

    def test_source_mismatch_is_rejected_even_with_valid_hashes(self):
        first, _ = self.add_recording("a_20261112_100000.wav")
        self.add_recording("b_20261112_110000.wav")
        path = Path(first["result"]) / (Path(first["source"]).stem + ".json")
        payload = json.loads(path.read_text())
        payload["文件"] = str(self.root / "different.wav")
        path.write_text(json.dumps(payload), encoding="utf-8")
        self.seal(first)
        result = self.export()
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, result["included_count"])

    def test_same_filename_from_different_directories_keeps_both_sources(self):
        first, _ = self.add_recording("录音_20261112_100000.wav", "甲目录原文", parent="甲")
        second, _ = self.add_recording("录音_20261112_100000.wav", "乙目录原文", parent="乙")
        result = self.export()
        text = Path(result["path"]).read_text()
        self.assertIn("甲目录原文", text)
        self.assertIn("乙目录原文", text)
        self.assertEqual([first["source"], second["source"]], result["ordered_sources"])

    def test_empty_transcript_remains_visible_with_explicit_no_body_note(self):
        first, _ = self.add_recording("空录音_20261112_100000.wav", "")
        self.add_recording("正常_20261112_110000.wav")
        result = self.export()
        self.assertEqual("completed", result["status"])
        text = Path(result["path"]).read_text()
        self.assertIn(Path(first["source"]).name, unquote(text))
        self.assertIn("没有识别出的正文", text)
        self.assertEqual(2, result["included_count"])

    def test_unique_history_is_preserved_and_hash_matches_published_content(self):
        self.add_recording("a_20261112_100000.wav")
        self.add_recording("b_20261112_110000.wav")
        first = self.export("first")
        original = Path(first["path"]).read_bytes()
        second = self.export("second")
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(original, Path(first["path"]).read_bytes())
        self.assertEqual(hashlib.sha256(Path(second["path"]).read_bytes()).hexdigest(), second["sha256"])
        self.assertEqual(Path(second["path"]).read_bytes(), (self.out / "合并全文.md").read_bytes())

    def test_single_recording_publishes_the_same_clean_document_format(self):
        self.add_recording("a_20261112_100000.wav")
        result = self.export()
        self.assertEqual("completed", result["status"])
        text = (self.out / "合并全文.md").read_text()
        self.assertIn("## 2026-11-12 10:00:00–10:00:05", text)
        self.assertIn("[00:00:01] 说话人1：这是原始文字。", text)
        for internal_label in ("批次索引", "待复核", "独立全文", "文件数", "字数", "录音顺序"):
            self.assertNotIn(internal_label, text)

    def test_empty_batch_does_not_publish_document(self):
        result = self.export()
        self.assertEqual("not_applicable", result["status"])
        self.assertFalse((self.out / "合并全文.md").exists())

    def test_date_range_crossing_midnight_keeps_both_dates(self):
        self.add_recording("会议_20261112_235958.wav")
        result = self.export()
        self.assertIn("## 2026-11-12 23:59:58–2026-11-13 00:00:03",
                      Path(result["path"]).read_text())

    def test_unknown_date_uses_only_relative_range(self):
        self.add_recording("没有日期.wav")
        result = self.export()
        self.assertIn("## 录音时间未知｜00:00:00–00:00:05", Path(result["path"]).read_text())

    def test_unplaced_recognized_text_is_preserved_without_speaker_review_duplication(self):
        entry, _ = self.add_recording("会议_20261112_100000.wav", "不要取消。")
        directory, source = Path(entry["result"]), Path(entry["source"])
        json_path = directory / (source.stem + ".json")
        data = json.loads(json_path.read_text())
        data["待复核"] = [
            dict(start=1., end=2., text="不要取消。", code="speaker_uncertain"),
            dict(start=2., end=3., text="未对齐文字，保留 **强调**。", code="invalid_timestamp"),
            dict(start=3., end=4., text="不要取消。", raw_start="nan", raw_end="4.0"),
        ]
        json_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (directory / "待复核.json").write_text(json.dumps({"items": data["待复核"]}), encoding="utf-8")
        self.seal(entry)
        result = self.export()
        text = Path(result["path"]).read_text()
        self.assertIn("### 待确认内容", text)
        self.assertIn("[时间待确认] 说话人未确定：未对齐文字，保留 **强调**。", text)
        self.assertEqual(2, text.count("不要取消。"))
        self.assertEqual(2, text.count("[时间待确认]"))


class TinyEngine:
    def __init__(self):
        self.calls = 0

    def run_chunk(self, audio, offset, hotword):
        self.calls += 1
        return ([dict(start=offset + .05, end=offset + .2, spk_local=0, text="原文不变。")], [[1., 0.]])


class BatchFlowIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.out = self.root / "结果"
        self.sources = []
        for stamp in ("110000", "100000"):
            source = self.root / f"会议_20261112_{stamp}.wav"
            sf.write(source, np.full(tx.SR, .1), tx.SR, subtype="PCM_16")
            self.sources.append(source)
        self.engine = TinyEngine()
        self.args = SimpleNamespace(engine="original", chunk_min=15, hotword_text="",
            asr_model="auto", model_dir=None, max_spk=10, no_norm=True, merge_thr=.62,
            enroll_thr=.5, part_min=30, keep_work=True, force=False, workers=1,
            threads=4, device="cpu", voiceprints=str(self.root / "无声纹"))
        def decode(source, target, norm=False):
            if not target.exists():
                target.write_bytes(source.read_bytes())
        patcher = patch.object(tx, "decode_to_wav", side_effect=decode)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_run_files_automatically_exports_and_recreates_on_all_cached_run(self):
        self.assertEqual(0, tx.run_files(self.engine, self.sources, self.out, self.args, {}))
        batch = tx.read_json(self.out / "批次索引.json")
        merged = batch["merged_markdown"]
        self.assertEqual("completed", merged["status"])
        self.assertEqual([str(p) for p in reversed(self.sources)], merged["ordered_sources"])
        self.assertEqual([str(p) for p in self.sources], [e["source"] for e in batch["files"]])
        original_hashes = {p: tx.content_sha(p) for p in self.out.rglob("*_全文.md")}
        (self.out / "合并全文.md").unlink()
        calls = self.engine.calls
        self.assertEqual(0, tx.run_files(self.engine, self.sources, self.out, self.args, {}))
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual(["skipped", "skipped"], [e["status"] for e in batch["files"]])
        self.assertTrue((self.out / "合并全文.md").exists())
        self.assertEqual(calls, self.engine.calls)
        self.assertTrue(all(tx.content_sha(path) == value for path, value in original_hashes.items()))

    def test_incomplete_batch_exports_partial_and_returns_failure(self):
        missing = self.root / "不存在_20261112_120000.wav"
        self.assertGreater(tx.run_files(self.engine, [self.sources[0], missing], self.out, self.args, {}), 0)
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual("failed", batch["status"])
        self.assertEqual("partial", batch["merged_markdown"]["status"])
        self.assertFalse((self.out / "合并全文.md").exists())
        self.assertIn("原文不变。", Path(batch["merged_markdown"]["path"]).read_text())

    def test_export_write_failure_is_not_reported_as_success(self):
        with patch.object(tx, "export_batch_markdown", side_effect=OSError("模拟合并文件写入失败")):
            self.assertGreater(tx.run_files(self.engine, self.sources, self.out, self.args, {}), 0)
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual("failed", batch["status"])
        self.assertEqual(["completed", "completed"], [e["status"] for e in batch["files"]])
        self.assertNotEqual("completed", batch["merged_markdown"]["status"])

    def test_source_changed_while_loading_later_recording_blocks_complete_publication(self):
        load_entry = batch_export._load_entry
        loaded = []

        def load_then_change_prior_source(entry):
            result = load_entry(entry)
            loaded.append(Path(entry["source"]))
            if len(loaded) == 2:
                loaded[0].write_bytes(loaded[0].read_bytes() + b"changed during merge")
            return result

        with patch.object(batch_export, "_load_entry", side_effect=load_then_change_prior_source):
            self.assertGreater(tx.run_files(self.engine, self.sources, self.out, self.args, {}), 0)
        self.assertEqual(2, len(loaded))
        batch = tx.read_json(self.out / "批次索引.json")
        self.assertEqual("failed", batch["status"])
        merged = batch["merged_markdown"]
        self.assertEqual("partial", merged["status"])
        self.assertEqual(1, merged["included_count"])
        self.assertEqual(str(loaded[0]), merged["missing"][0]["source"])
        self.assertIn("变化", merged["missing"][0]["reason"])
        self.assertFalse((self.out / "合并全文.md").exists())


if __name__ == "__main__":
    unittest.main()
