"""声纹缓存必须区分模型及样本内容；只用假引擎，不加载识别模型。"""
import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf


SPEC = importlib.util.spec_from_file_location("tx_voiceprint_tests", Path(__file__).parents[1] / "tx.py")
tx = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(tx)


class FakeVoiceprintEngine:
    def __init__(self, fingerprint, value):
        self.voiceprint_fingerprint = fingerprint
        self.value = value
        self.calls = 0

    def embed(self, audio):
        self.calls += 1
        return np.asarray(self.value, dtype=np.float32)


class VoiceprintCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.enroll = Path(self.temp.name) / "声纹样本"
        self.enroll.mkdir()
        self.sample = self.enroll / "测试者.wav"
        self.write_sample(0.1)
        decoder = patch.object(tx, "decode_to_wav", side_effect=lambda src, dst: shutil.copyfile(src, dst))
        decoder.start()
        self.addCleanup(decoder.stop)

    def write_sample(self, amplitude):
        sf.write(self.sample, np.full(tx.SR * 3, amplitude, dtype=np.float32), tx.SR, subtype="PCM_16")

    def test_model_fingerprint_change_recomputes_same_sample(self):
        old = FakeVoiceprintEngine("campplus-original-sha-old", [1, 0])
        new = FakeVoiceprintEngine("campplus-original-sha-new", [0, 1])
        np.testing.assert_array_equal([1, 0], tx.load_enroll(old, self.enroll)["测试者"])
        np.testing.assert_array_equal([0, 1], tx.load_enroll(new, self.enroll)["测试者"])
        self.assertEqual((1, 1), (old.calls, new.calls))

    def test_changed_sample_with_same_size_and_mtime_recomputes(self):
        engine = FakeVoiceprintEngine("campplus-model-sha", [1, 0])
        before = self.sample.stat()
        tx.load_enroll(engine, self.enroll)
        self.write_sample(0.6)
        os.utime(self.sample, ns=(before.st_atime_ns, before.st_mtime_ns))
        self.assertEqual(before.st_size, self.sample.stat().st_size)
        self.assertEqual(before.st_mtime_ns, self.sample.stat().st_mtime_ns)
        engine.value = [0, 1]
        np.testing.assert_array_equal([0, 1], tx.load_enroll(engine, self.enroll)["测试者"])
        self.assertEqual(2, engine.calls)

    def test_unchanged_sample_and_model_reuses_cache(self):
        first = FakeVoiceprintEngine("campplus-model-sha", [1, 0])
        second = FakeVoiceprintEngine("campplus-model-sha", [0, 1])
        tx.load_enroll(first, self.enroll)
        np.testing.assert_array_equal([1, 0], tx.load_enroll(second, self.enroll)["测试者"])
        self.assertEqual((1, 0), (first.calls, second.calls))

    def test_original_engine_fingerprint_tracks_actual_campplus_weights(self):
        models = self.enroll.parent / "models"
        for name in ("asr", "vad", "punc", "spk"):
            (models / name).mkdir(parents=True)
            (models / name / "config.yaml").write_text("fixture", "utf-8")
        weights = models / "spk/campplus_cn_common.bin"
        weights.write_bytes(b"old")
        before = weights.stat()
        fake_funasr = SimpleNamespace(AutoModel=lambda **kwargs: SimpleNamespace())
        with patch.dict(sys.modules, {"funasr": fake_funasr}):
            first = tx.Engine(model_dir=str(models))
            weights.write_bytes(b"new")
            os.utime(weights, ns=(before.st_atime_ns, before.st_mtime_ns))
            second = tx.Engine(model_dir=str(models))
        self.assertNotEqual(first.voiceprint_fingerprint, second.voiceprint_fingerprint)


if __name__ == "__main__":
    unittest.main()
