"""Local ONNX Paraformer with CIF timestamps and one shared CAM++ instance."""
import hashlib
import json
import logging
from pathlib import Path

import numpy as np
from light_postprocess import (BoundedPunctuation, BoundedVadFrontend, cluster_safely,
                               speaker_regions_with_unknown, assign_speakers_with_review)

SR = 16000
PIPELINE_VERSION = "light-cif-v2"
RUNTIME_VERSION = "light-runtime-v3"
log = logging.getLogger("tx")


class TimestampAlignmentError(RuntimeError):
    def __init__(self, message, text):
        super().__init__(message)
        self.text = text


def _norm(value):
    value = np.asarray(value, dtype=np.float32)
    return value / (np.linalg.norm(value, axis=-1, keepdims=True) + 1e-8)


def _strict_onnx_call(infer):
    # The upstream punctuation wrapper catches ONNXRuntimeError and may reuse
    # labels from a previous mini-sentence. Make actual inference failure fatal.
    def call(*args, **kwargs):
        from funasr_onnx.utils.utils import ONNXRuntimeError
        try:
            return infer(*args, **kwargs)
        except ONNXRuntimeError as exc:
            raise RuntimeError("标点 ONNX 推理失败") from exc
    return call


def _whole_token_timestamps(text, words, stamps, labels, punc):
    """Merge complete CIF units only; never subdivide a word's time uniformly."""
    from funasr.auto.auto_model import _get_punc_tokens
    tokens = _get_punc_tokens(text, labels, punc)
    if tokens is None or len(words) != len(stamps):
        return None
    normalize = lambda s: "".join(s.split()).replace("▁", "").casefold()
    units = [normalize(w) for w in words]
    wanted = [normalize(t) for t in tokens]
    if any(not u for u in units + wanted) or "".join(units) != "".join(wanted):
        return None
    starts, ends, cursor = {}, {}, 0
    for unit, stamp in zip(units, stamps):
        starts[cursor] = stamp[0]
        cursor += len(unit)
        ends[cursor] = stamp[1]
    merged, cursor = [], 0
    for token in wanted:
        end = cursor + len(token)
        if cursor not in starts or end not in ends:
            return None
        merged.append([starts[cursor], ends[end]])
        cursor = end
    return merged


class LightEngine:
    def __init__(self, device="cpu", threads=2, max_spk=10, asr_model="auto", model_dir=None,
                 quantize=None, **unused):
        if device not in ("cpu", "auto", None):
            raise ValueError("轻量后端目前使用 CPUExecutionProvider")
        if not model_dir:
            raise ValueError("轻量后端需要独立的本地模型目录")
        import torch
        import yaml
        import onnxruntime as ort
        from funasr import AutoModel
        from funasr.frontends.wav_frontend import WavFrontend
        from funasr.models.campplus.cluster_backend import ClusterBackend
        from funasr_onnx import Fsmn_vad, CT_Transformer
        self.root = Path(model_dir)
        self.quantize = (asr_model not in ("fp32", "light-fp32")) if quantize is None else bool(quantize)
        self.review_items = []
        self.last_tokens = []
        self.device = "cpu"
        self.max_spk = max_spk
        torch.set_num_threads(max(1, threads))
        filename = "model_quant.onnx" if self.quantize else "model.onnx"
        for part, required in (("asr", filename), ("punc", filename), ("vad", "model.onnx"),
                               ("spk", "campplus_cn_common.bin")):
            if not (self.root / part / required).is_file():
                raise FileNotFoundError(f"轻量模型缺失：{self.root / part / required}；请先完成导出自检")
        config = yaml.safe_load((self.root / "asr" / "config.yaml").read_text("utf-8"))
        self.tokens = json.loads((self.root / "asr" / "tokens.json").read_text("utf-8"))
        self.frontend = WavFrontend(cmvn_file=str(self.root / "asr" / "am.mvn"), **config["frontend_conf"])
        session_options = ort.SessionOptions()
        session_options.intra_op_num_threads = max(1, threads)
        session_options.inter_op_num_threads = 1
        session_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        session_options.enable_cpu_mem_arena = False
        session_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(str(self.root / "asr" / filename),
                                           sess_options=session_options, providers=["CPUExecutionProvider"])
        expected = ["logits", "token_num", "alphas", "peaks", "encoder_lengths"]
        if [o.name for o in self.session.get_outputs()] != expected:
            raise RuntimeError("ASR ONNX 缺少普通 CIF 时间戳输出，不能使用无时间戳模型替代")
        self.vad = Fsmn_vad(str(self.root / "vad"), quantize=False, intra_op_num_threads=threads)
        self.vad.vad_scorer_config["max_single_segment_time"] = 30000
        self.vad.frontend = BoundedVadFrontend(self.vad.frontend)
        self.punc = CT_Transformer(str(self.root / "punc"), quantize=self.quantize,
                                   intra_op_num_threads=threads)
        self.punc.infer = _strict_onnx_call(self.punc.infer)
        self.punc = BoundedPunctuation(self.punc)
        # Used by FunASR's surface-text/token alignment helpers.
        self.punc.jieba_usr_dict = None
        self.sv = AutoModel(model=str(self.root / "spk"), device="cpu", ncpu=threads,
                            disable_update=True, disable_pbar=True)
        self.cluster = ClusterBackend(max_num_spks=max_spk)
        digest = hashlib.sha256()
        with open(self.root / "spk" / "campplus_cn_common.bin", "rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        self.voiceprint_fingerprint = "campplus-window-v1:" + digest.hexdigest()
        log.info("轻量后端就绪：Paraformer %s + 小标点 + FSMN VAD + 单实例 CAM++", "INT8" if self.quantize else "FP32")

    def embed(self, audio):
        win, hop = int(1.5 * SR), int(0.75 * SR)
        pieces = []
        for start in range(0, max(1, len(audio) - win + 1), hop):
            piece = np.asarray(audio[start:start + win], dtype=np.float32)
            pieces.append(np.pad(piece, (0, win - len(piece))) if len(piece) < win else piece)
        results = self.sv.generate(input=pieces, disable_pbar=True)
        embeddings = np.stack([r["spk_embedding"].cpu().numpy().reshape(-1) for r in results])
        return _norm(_norm(embeddings).mean(0))

    def _asr(self, audio, start_ms):
        import torch
        from funasr.utils.load_utils import extract_fbank
        from funasr.utils.timestamp_tools import ts_prediction_lfr6_standard
        from funasr.utils.postprocess_utils import sentence_postprocess
        with torch.no_grad():
            features, lengths = extract_fbank([torch.from_numpy(np.asarray(audio, dtype=np.float32))],
                                               data_type="sound", frontend=self.frontend)
        inputs = {"speech": features.numpy().astype(np.float32),
                  "speech_lengths": lengths.numpy().astype(np.int32)}
        logits, token_nums, alphas, peaks, encoder_lengths = self.session.run(None, inputs)
        count = max(0, int(token_nums[0]))
        ids = logits[0, :count].argmax(axis=-1).tolist()
        tokens = [self.tokens[i] for i in ids if i not in (0, 1, 2)]
        if not tokens:
            return {"text": "", "timestamp": [], "words": []}
        raw_text, _ = sentence_postprocess(tokens)
        # Ordinary CIF uses 60 ms frames, unlike the BiCif wrapper's 20 ms grid.
        valid_frames = min(int(encoder_lengths[0]) + 1, alphas.shape[-1], peaks.shape[-1])
        try:
            _, raw_stamps = ts_prediction_lfr6_standard(
                torch.from_numpy(alphas[0, :valid_frames].copy()),
                torch.from_numpy(peaks[0, :valid_frames].copy()), list(tokens),
                vad_offset=0, upsample_rate=1)
            _, stamps, words = sentence_postprocess(tokens, raw_stamps)
        except (ValueError, IndexError, AssertionError, RuntimeError) as exc:
            raise TimestampAlignmentError(str(exc), raw_text) from exc
        if len(stamps) != len(words):
            raise TimestampAlignmentError("识别文字与 CIF 时间戳数量不一致", raw_text)
        duration_ms = len(audio) * 1000 / SR
        for stamp in stamps:
            if not np.isfinite(stamp).all() or stamp[1] < stamp[0]:
                raise TimestampAlignmentError("CIF 时间戳存在无效值或时间倒序", raw_text)
            if stamp[0] < -250 or stamp[1] > duration_ms + 250:
                raise TimestampAlignmentError("CIF 时间戳明显超出对应语音片段", raw_text)
            # CIF's learned edge offset can extend slightly before/after the VAD crop.
            stamp[0] = max(0, min(duration_ms, stamp[0])) + start_ms
            stamp[1] = max(0, min(duration_ms, stamp[1])) + start_ms
        # The timestamp overload joins all units with spaces for alignment; the
        # text-only overload preserves Chinese text and English word spacing.
        return {"text": raw_text, "timestamp": stamps, "words": words}

    def run_chunk(self, audio, offset, hotword=""):
        import torch
        from funasr.auto.auto_model import _timestamp_sentences_from_surface
        from funasr.models.campplus.utils import sv_chunk, postprocess
        self.review_items = []
        self.last_tokens = []
        audio = np.asarray(audio, dtype=np.float32)
        if not len(audio):
            return [], []
        if hotword:
            log.warning("当前 Paraformer 基础模型不支持热词注入；热词未参与识别")
        detected = self.vad(audio)
        if not isinstance(detected, list) or len(detected) != 1:
            raise RuntimeError("VAD ONNX 推理失败，不能把推理错误当作静音")
        regions = detected[0]
        if not regions:
            return [], []
        recognized, all_segments, embeddings = [], [], []
        for start_ms, end_ms in regions:
            start_ms, end_ms = max(0, start_ms), min(len(audio) * 1000 / SR, end_ms)
            if end_ms <= start_ms:
                continue
            piece = audio[int(start_ms * SR / 1000):int(end_ms * SR / 1000)]
            try:
                result = self._asr(piece, start_ms)
            except TimestampAlignmentError as exc:
                self.review_items.append({"start": offset + start_ms / 1000, "end": offset + end_ms / 1000,
                                          "reason": "已识别文字的时间戳无法可靠对齐", "code": "invalid_timestamp",
                                          "detail": str(exc), "text": exc.text})
                continue
            if not result["text"].strip():
                self.review_items.append({"start": offset + start_ms / 1000, "end": offset + end_ms / 1000,
                                          "reason": "检测到人声但没有识别出文字", "code": "speech_without_text"})
                continue
            result.update(start=start_ms, end=end_ms)
            recognized.append(result)
            segments = sv_chunk([[start_ms / 1000, end_ms / 1000, piece]])
            if segments:
                responses = self.sv.generate(input=[s[2] for s in segments], disable_pbar=True)
                if len(responses) != len(segments):
                    raise RuntimeError("声纹结果数量与音频窗口不一致")
                all_segments.extend(segments)
                embeddings.extend(r["spk_embedding"].cpu().numpy().reshape(-1) for r in responses)
        if not recognized:
            return [], []
        full_text = " ".join(r["text"] for r in recognized)
        words = [w for r in recognized for w in r["words"]]
        stamps = [t for r in recognized for t in r["timestamp"]]
        self.last_tokens = [{"text": w, "start": offset + t[0] / 1000, "end": offset + t[1] / 1000}
                            for w, t in zip(words, stamps)]
        _, punc_labels = self.punc(full_text)
        if getattr(self.punc, "last_forced_flushes", 0):
            self.review_items.append({"start": offset + recognized[0]["start"] / 1000,
                                      "end": offset + recognized[-1]["end"] / 1000,
                                      "code": "punctuation_context_limit",
                                      "reason": "连续长句的标点已分批处理，请核对断句"})
        merged_stamps = _whole_token_timestamps(full_text, words, stamps, punc_labels, self.punc)
        sentences = (_timestamp_sentences_from_surface(full_text, merged_stamps, punc_labels, self.punc)
                     if merged_stamps is not None else None)
        if sentences is None:
            # Retain real VAD boundaries; never invent uniform per-character timestamps.
            sentences = [{"text": r["text"], "start": r["start"], "end": r["end"]} for r in recognized]
            self.review_items.extend({"start": offset + r["start"] / 1000, "end": offset + r["end"] / 1000,
                                      "reason": "标点未能与时间戳对齐，已保留语音片段边界",
                                      "code": "punctuation_alignment", "text": r["text"]} for r in recognized)
        if not embeddings:
            raise RuntimeError("已有识别文字，但没有可用的说话人声纹窗口")
        matrix = np.stack(embeddings)
        labels, issues = cluster_safely(matrix, self.cluster, getattr(self, "max_spk", 10))
        self.review_items.extend({**issue, "start": offset + recognized[0]["start"] / 1000,
                                  "end": offset + recognized[-1]["end"] / 1000} for issue in issues)
        centers = [matrix[labels == label].mean(0).tolist() for label in range(int(labels.max()) + 1)]
        # Avoid the upstream smoother's one-short-window indexing edge case.
        if (labels < 0).any():
            speaker_regions = speaker_regions_with_unknown(all_segments, labels)
        elif len(set(labels.tolist())) == 1:
            speaker_regions = [[all_segments[0][0], all_segments[-1][1], int(labels[0])]]
        else:
            speaker_regions = postprocess(all_segments, None, labels, matrix)
        issues = assign_speakers_with_review(sentences, speaker_regions,
                                             speaker_regions_with_unknown(all_segments, labels))
        self.review_items.extend({**issue, "start": offset + issue["start"] / 1000,
                                  "end": offset + issue["end"] / 1000} for issue in issues)
        output = [{"start": round(s["start"] / 1000 + offset, 3),
                   "end": round(s["end"] / 1000 + offset, 3), "text": s["text"].strip(),
                   "spk_local": int(s.get("spk", 0))} for s in sentences if s["text"].strip()]
        return output, centers
