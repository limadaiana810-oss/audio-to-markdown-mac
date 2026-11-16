"""Source identity and resume regressions; no speech models or network required."""
import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf


SPEC = importlib.util.spec_from_file_location("tx", Path(__file__).parents[1] / "tx.py")
tx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tx)


class FakeEngine:
    def __init__(self):
        self.calls = 0

    def run_chunk(self, audio, offset, hotword):
        self.calls += 1
        return ([{"start": offset, "end": offset + 0.1, "spk_local": 0,
                  "text": "新录音" if audio.mean() > 0.3 else "旧录音"}], [[1.0, 0.0]])


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "结果"
        self.args = SimpleNamespace(chunk_min=10, hotword_text="", asr_model="auto",
                                    model_dir=None, max_spk=10, no_norm=False,
                                    merge_thr=0.62, enroll_thr=0.5, part_min=30,
                                    keep_work=True, force=False)
        self.engine = FakeEngine()

        def decode(src, dst, norm=False):
            if not dst.exists():
                audio, _ = sf.read(src)
                sf.write(dst, audio, tx.SR, subtype="PCM_16")

        self.decoder = patch.object(tx, "decode_to_wav", side_effect=decode).start()
        self.addCleanup(patch.stopall)

    def audio(self, relative, amplitude=0.1):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, np.full(tx.SR, amplitude), tx.SR, subtype="PCM_16")
        return path

    def process(self, src):
        return tx.process_file(self.engine, src, self.out, self.args, {})

    def test_same_stem_sources_are_both_processed(self):
        a = self.audio("甲/会议.wav")
        b = self.audio("乙/会议.wav", 0.6)
        self.assertEqual(0, tx.run_files(self.engine, [a, b], self.out, self.args, {}))
        self.assertEqual(2, self.engine.calls)
        outputs = list(self.out.glob("*/会议.json"))
        self.assertEqual(2, len(outputs))
        texts = {json.loads(p.read_text())["句子"][0]["text"] for p in outputs}
        self.assertEqual({"旧录音", "新录音"}, texts)

    def test_replaced_source_reprocesses_even_with_same_size_and_mtime(self):
        src = self.audio("会议.wav")
        before = src.stat()
        out = self.process(src)
        self.audio("会议.wav", 0.6)
        os.utime(src, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_size, src.stat().st_size)
        tx.run_files(self.engine, [src], self.out, self.args, {})
        self.assertEqual(2, self.engine.calls)
        data = json.loads((out / "会议.json").read_text())
        self.assertEqual("新录音", data["句子"][0]["text"])

    def test_force_reuses_unchanged_transcription_cache(self):
        src = self.audio("会议.wav")
        out = self.process(src)
        self.args.force = True
        tx.run_files(self.engine, [src], self.out, self.args, {})
        self.assertEqual(1, self.engine.calls)
        self.assertEqual(self.out / "会议", out)

    def test_legacy_result_is_preserved_and_not_used_as_completion(self):
        src = self.audio("会议.wav", 0.6)
        old = self.out / "会议"
        old.mkdir(parents=True)
        marker = old / "目录.md"
        marker.write_text("旧结果，请保留", encoding="utf-8")
        tx.run_files(self.engine, [src], self.out, self.args, {})
        self.assertEqual(1, self.engine.calls)
        self.assertEqual("旧结果，请保留", marker.read_text())
        recording_dirs = [p for p in self.out.iterdir() if p.is_dir()
                          and not p.name.startswith('.') and p.name != '合并全文']
        self.assertEqual(2, len(recording_dirs))

    def test_partial_output_is_retried_with_cached_transcript(self):
        src = self.audio("会议.wav")
        out = self.process(src)
        (out / "会议_全文.md").unlink()
        tx.run_files(self.engine, [src], self.out, self.args, {})
        self.assertTrue((out / "会议_全文.md").exists())
        self.assertEqual(1, self.engine.calls)

    def test_changed_source_during_processing_is_not_marked_complete(self):
        src = self.audio("会议.wav")
        original = self.engine.run_chunk

        def mutate(*args):
            result = original(*args)
            self.audio("会议.wav", 0.6)
            return result

        self.engine.run_chunk = mutate
        with self.assertRaisesRegex(RuntimeError, "录音.*变化"):
            self.process(src)
        self.assertFalse(tx.is_completed(src, self.out))

    def test_interrupted_transcription_resumes_without_repeating_first_chunk(self):
        src = self.audio("会议.wav")
        original = self.engine.run_chunk

        def fail_second(audio, offset, hotword):
            if offset == 0.5:
                raise RuntimeError("模拟第二段中断")
            return original(audio, offset, hotword)

        self.engine.run_chunk = fail_second
        with patch.object(tx, "plan_chunks", return_value=([(0, 0.5), (0.5, 1)], 1)):
            with self.assertRaisesRegex(RuntimeError, "第二段中断"):
                self.process(src)
        self.assertEqual(1, self.engine.calls)
        self.assertFalse(tx.is_completed(src, self.out))
        self.engine.run_chunk = original
        out = self.process(src)
        self.assertEqual(2, self.engine.calls)
        self.assertEqual(2, len(json.loads((out / "会议.json").read_text())["句子"]))
        self.assertTrue(tx.is_completed(src, self.out))

    def test_source_replaced_after_interruption_does_not_reuse_first_chunk(self):
        src = self.audio("会议.wav")
        original = self.engine.run_chunk

        def fail_second(audio, offset, hotword):
            if offset == 0.5:
                raise RuntimeError("模拟第二段中断")
            return original(audio, offset, hotword)

        with patch.object(tx, "plan_chunks", return_value=([(0, 0.5), (0.5, 1)], 1)):
            self.engine.run_chunk = fail_second
            with self.assertRaises(RuntimeError):
                self.process(src)
            self.audio("会议.wav", 0.6)
            self.engine.run_chunk = original
            out = self.process(src)
        self.assertEqual(3, self.engine.calls)
        sentences = json.loads((out / "会议.json").read_text())["句子"]
        self.assertEqual(["新录音", "新录音"], [s["text"] for s in sentences])

    def test_watch_processes_colliding_sources_and_changed_source(self):
        a = self.audio("甲/会议.wav")
        b = self.audio("乙/会议.wav")
        cycles = 0

        def next_cycle(_):
            nonlocal cycles
            cycles += 1
            if cycles == 2:
                self.audio("甲/会议.wav", 0.6)
            if cycles == 4:
                raise KeyboardInterrupt

        argv = ["tx.py", "watch", str(a), str(b), "-o", str(self.out), "--workers", "1",
                "--engine", "original",
                "--voiceprints", str(self.root / "无声纹"),
                "--hotwords", str(self.root / "无热词")]
        with patch.object(tx.sys, "argv", argv), \
             patch.dict(tx.sys.modules, {"torch": SimpleNamespace(set_num_threads=lambda _: None)}), \
             patch.object(tx, "Engine", return_value=self.engine), \
             patch.object(tx, "pick_device", return_value="cpu"), \
             patch.object(tx.time, "sleep", side_effect=next_cycle):
            with self.assertRaises(KeyboardInterrupt):
                tx.main()
        self.assertEqual(3, self.engine.calls)
        self.assertTrue(tx.is_completed(a, self.out))
        self.assertTrue(tx.is_completed(b, self.out))
        a_json = tx.output_dir_for(a, self.out) / "会议.json"
        self.assertEqual("新录音", json.loads(a_json.read_text())["句子"][0]["text"])


if __name__ == "__main__":
    unittest.main()
