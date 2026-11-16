import importlib.util
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
sys.path.insert(0, str(Path(__file__).parents[1]))
from light_postprocess import (BoundedPunctuation, BoundedVadFrontend, cluster_safely,
                              speaker_regions_with_unknown, assign_speakers_with_review)


@unittest.skipUnless(importlib.util.find_spec('funasr_onnx'), 'light ONNX dependency not installed')
class PunctuationTests(unittest.TestCase):
    def model(self, label_for_id):
        from funasr_onnx.punc_bin import CT_Transformer
        model = CT_Transformer.__new__(CT_Transformer)
        model.seg_jieba = False
        model.converter = Mock()
        model.converter.tokens2ids.side_effect = lambda words: list(range(len(words)))
        model.punc_list = ['<unk>', '_', '，', '。', '？', '、']
        model.period = 3
        model.lengths = []
        def infer(text, text_lengths):
            model.lengths.append(text.shape[1])
            logits = np.zeros((1,text.shape[1],6),np.float32)
            for index, token in enumerate(text[0]):
                logits[0,index,label_for_id(int(token))] = 1
            return [logits]
        model.infer = infer
        return model

    def test_hard_limit_retains_each_token_and_label_in_order(self):
        model = self.model(lambda token: 1 if token % 2 == 0 else 5)
        bounded = BoundedPunctuation(model)
        text, labels = bounded('中'*1000)
        expected = [1 if token % 2 == 0 else 5 for token in range(1000)]
        expected[-1] = 3
        self.assertEqual(labels,expected)
        self.assertEqual(text.replace('、','').replace('。',''),'中'*1000)
        self.assertLessEqual(max(model.lengths),256)
        self.assertGreater(bounded.last_forced_flushes,0)

    def test_natural_boundaries_match_existing_wrapper(self):
        model = self.model(lambda token: 3 if token % 13 == 12 else 1)
        text = '中'*800
        old_text, old_labels = model(text)
        new_text,new_labels = BoundedPunctuation(model)(text)
        self.assertEqual(new_labels,old_labels)
        self.assertEqual(new_text,old_text)

    def test_bad_tensor_is_failure_not_empty_text(self):
        model = self.model(lambda token:1)
        model.infer = Mock(return_value=[np.full((1,2,6),np.nan)])
        with self.assertRaisesRegex(RuntimeError,'无效'):
            BoundedPunctuation(model)('你好')


@unittest.skipUnless(importlib.util.find_spec('funasr_onnx'), 'light ONNX dependency not installed')
class VadFrontendTests(unittest.TestCase):
    def frontend(self):
        from funasr_onnx.utils.frontend import WavFrontend
        value = WavFrontend(lfr_m=5,lfr_n=1,dither=0)
        rng=np.random.default_rng(7)
        value.cmvn=np.stack([rng.normal(size=400),rng.uniform(.2,2,size=400)])
        value.cmvn_file='test-statistics'
        return value

    def test_features_match_original_float32_cast(self):
        original=self.frontend()
        bounded=BoundedVadFrontend(original,audio_block_seconds=.1,cmvn_block_frames=7)
        audio=np.random.default_rng(8).normal(0,.02,16000*2+353).astype(np.float32)
        a,_=original.fbank(audio);b,_=bounded.fbank(audio)
        np.testing.assert_array_equal(a,b)
        expected,_=original.lfr_cmvn(a)
        actual,_=bounded.lfr_cmvn(b)
        self.assertEqual(actual.dtype,np.float32)
        np.testing.assert_array_equal(actual,expected.astype(np.float32))

    def test_lfr_padding_and_strides_match(self):
        original=self.frontend()
        for length in [1,2,5,6,7,11,31]:
            matrix=np.arange(length*3,dtype=np.float32).reshape(length,3)
            for m,n in [(5,1),(7,6),(3,2)]:
                np.testing.assert_array_equal(BoundedVadFrontend.apply_lfr(matrix,m,n),original.apply_lfr(matrix,m,n))

    def test_audio_to_python_list_has_a_hard_block_limit(self):
        bounded=BoundedVadFrontend(self.frontend(),audio_block_seconds=.01)
        sizes=[]
        fake=SimpleNamespace(num_frames_ready=0,accept_waveform=lambda sr,data:sizes.append(len(data)))
        with patch('kaldi_native_fbank.OnlineFbank',return_value=fake):
            bounded.fbank(np.zeros(5000,np.float32))
        self.assertEqual(sum(sizes),5000)
        self.assertLessEqual(max(sizes),160)


class SpeakerSafetyTests(unittest.TestCase):
    def backend(self):
        return SimpleNamespace(model_config={'merge_thr':.78},
                               umap_hdbscan_cluster=lambda values:np.full(len(values),-1),
                               merge_to_max_spks=Mock(),merge_by_cos=Mock())

    def test_short_orthogonal_groups_are_not_forced_into_one(self):
        values=np.array([[1.,0.]]*9+[[0.,1.]]*10,np.float32)
        labels,issues=cluster_safely(values,self.backend())
        self.assertEqual(set(labels),{0,1})
        self.assertEqual(len(set(labels[:9])),1)
        self.assertNotEqual(labels[0],labels[-1])
        self.assertIn('speaker_samples_insufficient',[i['code'] for i in issues])

    def test_all_noise_and_zero_vectors_are_unknown(self):
        for values in [np.ones((2048,3),np.float32),np.zeros((4,3),np.float32),np.full((2,3),np.nan)]:
            labels,issues=cluster_safely(values,self.backend())
            self.assertTrue((labels==-1).all())
            self.assertTrue(issues)

    def test_mixed_invalid_does_not_poison_known_center(self):
        values=np.array([[1.,0.],[np.nan,0.],[0.,0.],[1.,.01]],np.float32)
        labels,issues=cluster_safely(values,self.backend())
        self.assertEqual(labels.tolist(),[0,-1,-1,0])
        self.assertTrue(np.isfinite(values[labels==0].mean(0)).all())

    def test_cross_speaker_sentence_keeps_text_and_adds_review(self):
        sentence={'start':0,'end':10000,'text':'甲说前半句，乙说后半句。'}
        issues=assign_speakers_with_review([sentence],[[0,6,0],[6,10,1]])
        self.assertEqual(sentence['text'],'甲说前半句，乙说后半句。')
        self.assertEqual(sentence['spk'],0)
        self.assertEqual(issues[0]['text'],sentence['text'])
        self.assertEqual(issues[0]['code'],'speaker_change_inside_sentence')
        self.assertEqual((issues[0]['start'],issues[0]['end']),(0,10000))

    def test_unknown_windows_stay_unknown(self):
        regions=speaker_regions_with_unknown([[0,1.5,None],[.75,2.25,None]],[0,-1])
        self.assertEqual(regions,[[0.,1.125,0],[1.125,2.25,-1]])
        sentence={'start':1500,'end':2200,'text':'保留原话'}
        issues=assign_speakers_with_review([sentence],regions)
        self.assertEqual(sentence['spk'],-1)
        self.assertEqual(issues[0]['code'],'speaker_uncertain')

    def test_temporal_smoothing_cannot_hide_review_of_short_turn(self):
        sentence={'start':0,'end':3000,'text':'保留短插话'}
        issues=assign_speakers_with_review([sentence],[[0,3,0]],[[0,1,0],[1,1.4,1],[1.4,3,0]])
        self.assertEqual(sentence['spk'],0)
        self.assertEqual(issues[0]['code'],'speaker_change_inside_sentence')

if __name__=='__main__': unittest.main()
