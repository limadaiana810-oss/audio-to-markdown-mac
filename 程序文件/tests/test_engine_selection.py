"""Engine migration, semantic resume and timestamp review regressions."""
import json
import tempfile
import unittest
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
sys.path.insert(0, str(Path(__file__).parents[1]))
import tx


class Engine:
    review_items = []
    def __init__(self):
        self.calls = 0
    def run_chunk(self, audio, offset, hotword):
        self.calls += 1
        return ([dict(start=offset, end=offset+0.5, text='测试', spk_local=0)], [[1., 0.]])


class EngineSelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.src = self.root / '会议.wav'
        sf.write(self.src, np.ones(tx.SR, dtype=np.float32)*0.1, tx.SR)
        self.out = self.root / '结果'
        self.model = self.root / 'models'
        self.model.mkdir()
        (self.model / 'model.onnx').write_bytes(b'weights-v1')
        self.args = SimpleNamespace(engine='original', chunk_min=15, hotword_text='',
            asr_model='auto', model_dir=str(self.model), max_spk=10, no_norm=False,
            merge_thr=.62, enroll_thr=.5, part_min=30, keep_work=True,
            force=False, workers=1, threads=10, device='cpu',
            voiceprints=str(self.root / 'voiceprints'))
        self.engine = Engine()
        def decode(src, dst, norm=False):
            if not dst.exists():
                dst.write_bytes(src.read_bytes())
        self.patcher = patch.object(tx, 'decode_to_wav', side_effect=decode)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def process(self):
        return tx.process_file(self.engine, self.src, self.out, self.args, {})

    def test_engine_change_preserves_original_and_uses_different_cache(self):
        original = self.process()
        contents = (original / '会议.json').read_bytes()
        self.args.engine = 'light'
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))
        trial = self.process()
        self.assertEqual(self.out/'轻量试用'/'会议', trial)
        self.assertEqual(contents, (original/'会议.json').read_bytes())
        self.assertEqual(2, self.engine.calls)

    def test_model_contents_invalidate_completion_and_chunk_cache(self):
        self.process()
        (self.model/'model.onnx').write_bytes(b'weights-v2')
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))
        self.process()
        self.assertEqual(2, self.engine.calls)

    def test_workers_and_threads_do_not_invalidate_completion(self):
        self.process()
        self.args.workers, self.args.threads = 4, 24
        self.assertTrue(tx.is_completed(self.src, self.out, self.args))

    def test_export_settings_reexport_without_retranscription(self):
        self.process()
        self.args.part_min = 10
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))
        self.process()
        self.assertEqual(1, self.engine.calls)

    def test_voiceprint_changes_invalidate_final_completion(self):
        self.process()
        vp = Path(self.args.voiceprints)
        vp.mkdir()
        (vp/'我.wav').write_bytes(self.src.read_bytes())
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))

    def test_legacy_completion_without_config_not_trusted(self):
        result = self.process()
        p = result/'.work/completed.json'
        data = json.loads(p.read_text())
        data.pop('config')
        p.write_text(json.dumps(data))
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))

    def test_invalid_timestamps_are_flagged_and_not_exported_as_fabricated_subtitles(self):
        rows = [dict(start=-1, end=0, text='越界', spk_local=0),
                dict(start=0, end=float('nan'), text='无效', spk_local=0),
                dict(start=.2, end=.8, text='有效', spk_local=0)]
        with patch.object(self.engine, 'run_chunk', return_value=(rows, [[1.,0.]])):
            result = self.process()
        data = json.loads((result/'会议.json').read_text())
        self.assertEqual(['有效'], [s['text'] for s in data['句子']])
        self.assertEqual(2, len(data['待复核']))
        self.assertEqual('s000001', data['句子'][0]['id'])
        self.assertNotIn('越界', (result/'会议.srt').read_text())

    def test_failure_writes_review_and_never_completion(self):
        with patch.object(self.engine, 'run_chunk', side_effect=RuntimeError('模拟故障')):
            with self.assertRaises(RuntimeError):
                self.process()
        self.assertFalse(tx.is_completed(self.src, self.out, self.args))
        result = tx.output_dir_for(self.src, self.out)
        self.assertIn('模拟故障', (result/'待复核.md').read_text())
        self.process()
        self.assertEqual([], json.loads((result/'待复核.json').read_text())['items'])

    def test_single_cif_edge_frame_is_clipped_without_dropping_words(self):
        rows = [dict(start=-.03, end=.2, text='开头', spk_local=0),
                dict(start=.2, end=1.05, text='结尾', spk_local=0)]
        with patch.object(self.engine, 'run_chunk', return_value=(rows, [[1., 0.]])):
            result = self.process()
        data = json.loads((result/'会议.json').read_text())
        self.assertEqual(['开头', '结尾'], [s['text'] for s in data['句子']])
        self.assertEqual(0, data['句子'][0]['start'])
        self.assertEqual(1, data['句子'][1]['end'])
        self.assertEqual([], data['待复核'])

    def test_rounding_cannot_export_zero_duration_subtitle(self):
        rows = [dict(start=.0001, end=.0002, text='太短', spk_local=0)]
        valid, review = tx.validate_sentences(rows, 0, 1)
        self.assertEqual([], valid)
        self.assertEqual('太短', review[0]['text'])

    def test_float_cut_boundary_does_not_drop_valid_edge_sentence(self):
        rows = [dict(start=.1, end=.3, text='边界', spk_local=0)]
        valid, review = tx.validate_sentences(rows, .10000000000000002, .3)
        self.assertEqual(1, len(valid))
        self.assertEqual([], review)

    def test_light_rejects_non_cpu_provider_and_model_override(self):
        for opts in [ ['--device','mps'], ['--asr-model','other'] ]:
            with patch.object(tx.sys, 'argv', ['tx','run',str(self.src),'--engine','light']+opts):
                with self.assertRaises(SystemExit) as exc:
                    tx.main()
                self.assertEqual(2, exc.exception.code)

if __name__ == '__main__':
    unittest.main()
