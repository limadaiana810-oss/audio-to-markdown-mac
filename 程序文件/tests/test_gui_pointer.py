"""A GUI job must follow its own batch index even when another job completes."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, str(Path(__file__).parents[1]))
import tx


class Engine:
    def run_chunk(self, audio, offset, hotword):
        return ([dict(start=offset, end=offset + .2, text="原话", spk_local=0)], [[1., 0.]])


class GuiPointerTests(unittest.TestCase):
    def test_private_pointer_survives_a_later_batch_and_never_points_at_latest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            out = root / "results"
            sources = [root / "first_20261112_100000.wav", root / "second_20261112_110000.wav"]
            for src in sources:
                sf.write(src, np.ones(tx.SR, dtype=np.float32) * .1, tx.SR)
            args = SimpleNamespace(engine="original", chunk_min=15, hotword_text="",
                asr_model="auto", model_dir=None, max_spk=10, no_norm=True,
                merge_thr=.62, enroll_thr=.5, part_min=30, keep_work=True,
                force=False, workers=1, threads=4, device="cpu", voiceprints=str(root / "vp"))
            pointers = []
            def decode(src, dst, norm=False):
                if not dst.exists():
                    dst.write_bytes(src.read_bytes())
            with patch.object(tx, "decode_to_wav", side_effect=decode):
                for i, src in enumerate(sources):
                    pointer = root / f"job-{i}.txt"
                    args.batch_id_file = str(pointer)
                    self.assertEqual(0, tx.run_files(Engine(), [src], out, args, {}))
                    pointers.append(Path(pointer.read_text().strip()))
            self.assertNotEqual(*pointers)
            for pointer, source in zip(pointers, sources):
                self.assertTrue(pointer.is_absolute())
                self.assertEqual(out / ".批次", pointer.parent)
                batch = json.loads(pointer.read_text())
                self.assertEqual(str(source), batch["files"][0]["source"])
                self.assertEqual("completed", batch["merged_markdown"]["status"])
            self.assertEqual(str(sources[1]), json.loads((out / "批次索引.json").read_text())["files"][0]["source"])


if __name__ == "__main__":
    unittest.main()
