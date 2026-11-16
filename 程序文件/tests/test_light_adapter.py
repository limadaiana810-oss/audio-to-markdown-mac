"""No model downloads/loads: failure semantics, real timestamp mapping, stable CIF."""
import sys
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
import numpy as np
import torch
sys.path.insert(0, str(Path(__file__).parents[1]))
from light_engine import LightEngine, TimestampAlignmentError, _whole_token_timestamps, _strict_onnx_call
from export_light_models import stable_cif


class StableCifTests(unittest.TestCase):
    def test_integral_is_continuous_near_integer_boundary(self):
        hidden = torch.tensor([[[1.0], [3.0], [20.0], [5.0], [7.0]]])
        alpha = torch.tensor([[.6, .4, 1e-8, .6, .4]])
        actual, peaks = stable_cif(hidden, alpha)
        self.assertEqual(tuple(actual.shape), (1, 2, 1))
        self.assertTrue(torch.allclose(actual[0, :, 0], torch.tensor([1.8, 5.8]), atol=2e-6))
        perturbed, _ = stable_cif(hidden, alpha + 1e-7)
        self.assertLess(float((actual-perturbed).abs().max()), 1e-4)
        self.assertTrue(torch.isfinite(peaks).all())

    def test_empty_and_padded_batch(self):
        hidden = torch.ones(2, 4, 3)
        alpha = torch.tensor([[.5, .5, .5, .5], [0., 0., 0., 0.]])
        actual, _ = stable_cif(hidden, alpha)
        self.assertEqual(tuple(actual.shape), (2, 2, 3))
        self.assertTrue(torch.equal(actual[1], torch.zeros(2, 3)))
        empty, _ = stable_cif(hidden, torch.zeros(2, 4))
        self.assertEqual(tuple(empty.shape), (2, 0, 3))

    def test_matches_scalar_integrator_across_many_integer_boundaries(self):
        # A scalar reference walks frames and fires one token at a time. Near
        # integer masses previously triggered the floor/scatter discontinuity.
        alpha = torch.tensor([[.99999994, .00000008, .6, .4] * 20,
                              [.25, .25, .25, .25] * 20])
        hidden = torch.arange(2*80*3,dtype=torch.float32).reshape(2,80,3)/100
        actual, _ = stable_cif(hidden, alpha)
        for batch in range(2):
            tokens, filled, current = [], 0.0, np.zeros(3)
            for weight, frame in zip(alpha[batch].tolist(),hidden[batch].numpy()):
                remaining = float(weight)
                while remaining > 0:
                    take = min(1.0-filled,remaining)
                    current += take*frame.astype(float)
                    filled += take
                    remaining -= take
                    if filled >= 1.0:
                        tokens.append(current)
                        current, filled = np.zeros(3), 0.0
            expected = np.asarray(tokens)
            np.testing.assert_allclose(actual[batch,:len(tokens)].numpy(),expected,atol=2e-5,rtol=1e-6)
            np.testing.assert_array_equal(actual[batch,len(tokens):].numpy(),0)


class LightAdapterTests(unittest.TestCase):
    def engine(self):
        engine = LightEngine.__new__(LightEngine)
        engine.vad = Mock(return_value=[[[100, 1900]]])
        engine._asr = Mock()
        engine.punc = Mock(return_value=('', []))
        engine.punc.last_forced_flushes = 0
        engine.sv = Mock()
        engine.sv.generate.side_effect = lambda input, **kw: [{'spk_embedding':torch.ones(1, 4)} for _ in input]
        engine.cluster = Mock(side_effect=lambda matrix, **kw: np.zeros(len(matrix),dtype=int))
        engine.cluster.model_config = {'merge_thr': .78}
        return engine

    def test_true_asr_failure_propagates(self):
        engine = self.engine()
        engine._asr.side_effect = RuntimeError('session failed')
        with self.assertRaisesRegex(RuntimeError, 'session failed'):
            engine.run_chunk(np.ones(32000,dtype=np.float32),900)

    def test_timestamp_failure_preserves_text_for_review(self):
        engine = self.engine()
        engine._asr.side_effect = TimestampAlignmentError('bad alignment','可复核的原文')
        self.assertEqual(engine.run_chunk(np.ones(32000,dtype=np.float32),900),([],[]))
        self.assertEqual(engine.review_items[0]['text'],'可复核的原文')
        self.assertEqual(engine.review_items[0]['start'],900.1)
        self.assertEqual(engine.review_items[0]['end'],901.9)

    def test_no_text_is_a_review_item(self):
        engine = self.engine()
        engine._asr.return_value = {'text':'','timestamp':[],'words':[]}
        self.assertEqual(engine.run_chunk(np.ones(32000,dtype=np.float32),0),([],[]))
        self.assertEqual(engine.review_items[0]['code'],'speech_without_text')

    def test_alignment_fallback_uses_actual_vad_boundaries(self):
        engine = self.engine()
        engine._asr.return_value = {'text':'你好','timestamp':[[120,500],[500,1800]],'words':['你','好']}
        sentences, centers = engine.run_chunk(np.ones(32000,dtype=np.float32),900)
        self.assertEqual(sentences,[{'start':900.1,'end':901.9,'text':'你好','spk_local':0}])
        self.assertEqual(len(centers),1)
        self.assertEqual(engine.review_items[0]['code'],'punctuation_alignment')

    def test_no_uniform_subdivision_when_punctuation_splits_a_word(self):
        with patch('funasr.auto.auto_model._get_punc_tokens',return_value=['ab','cd']):
            self.assertIsNone(_whole_token_timestamps('abcd',['abcd'],[[100,900]],[1,3],None))
        with patch('funasr.auto.auto_model._get_punc_tokens',return_value=['abcd']):
            self.assertEqual(_whole_token_timestamps('abcd',['ab','cd'],[[100,400],[500,900]],[3],None),[[100,900]])

    @unittest.skipUnless(importlib.util.find_spec('funasr_onnx'), 'light ONNX dependency not installed')
    def test_punctuation_onnx_error_cannot_be_swallowed_by_wrapper(self):
        from funasr_onnx.utils.utils import ONNXRuntimeError
        wrapped = _strict_onnx_call(Mock(side_effect=ONNXRuntimeError('bad session')))
        with self.assertRaisesRegex(RuntimeError,'标点 ONNX 推理失败'):
            wrapped(None)


if __name__ == '__main__':
    unittest.main()
