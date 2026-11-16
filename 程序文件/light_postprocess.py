"""Bounded local adapters for the pinned FunASR runtime; no site-package edits."""
import numpy as np

POSTPROCESS_VERSION = "bounded-postprocess-v1"


class BoundedPunctuation:
    """Keep the existing token/model path while imposing a hard context bound."""
    def __init__(self, model, max_context_tokens=256, retained_context=64, step=20):
        if not 0 <= retained_context < max_context_tokens or not 0 < step <= max_context_tokens:
            raise ValueError("Invalid punctuation context limits")
        self.model = model
        self.max_context_tokens = max_context_tokens
        self.retained_context = min(retained_context, max_context_tokens - step)
        self.step = step
        self.jieba_usr_dict = None
        self.last_max_context_tokens = 0
        self.last_forced_flushes = 0

    def __getattr__(self, name):
        return getattr(self.model, name)

    def __call__(self, text):
        from funasr_onnx.utils.utils import code_mix_split_words
        from funasr.auto.auto_model import _punctuate_surface_text
        tokens = (self.model.code_mix_split_words_jieba(text) if self.model.seg_jieba
                  else code_mix_split_words(text))
        self.last_max_context_tokens = self.last_forced_flushes = 0
        if not tokens:
            return "", []
        ids = self.model.converter.tokens2ids(tokens)
        output, cache_ids, cache_labels = [], [], []
        for start in range(0, len(ids), self.step):
            following = ids[start:start + self.step]
            if len(cache_ids) + len(following) > self.max_context_tokens:
                # Commit already predicted labels, retaining real right context.
                # Do not insert punctuation or discard any text/token here.
                count = len(cache_ids) - self.retained_context
                output.extend(cache_labels[:count])
                cache_ids, cache_labels = cache_ids[count:], cache_labels[count:]
                self.last_forced_flushes += 1
            cache_ids.extend(following)
            self.last_max_context_tokens = max(self.last_max_context_tokens, len(cache_ids))
            values = np.asarray(cache_ids, dtype=np.int32)[None, :]
            logits = self.model.infer(values, np.array([len(cache_ids)], dtype=np.int32))[0]
            if logits.ndim != 3 or logits.shape[:2] != values.shape or not np.isfinite(logits).all():
                raise RuntimeError("标点模型返回了无效的标签张量")
            cache_labels = logits.argmax(axis=-1)[0].tolist()
            if max(cache_labels, default=0) >= len(self.model.punc_list):
                raise RuntimeError("标点模型返回了未知标签")
            if start + self.step >= len(ids):
                output.extend(cache_labels)
                break
            # Same natural sentence/comma flushing rule as the pinned wrapper.
            sentence_end, last_comma = -1, -1
            for index in range(len(cache_labels) - 2, 1, -1):
                symbol = self.model.punc_list[cache_labels[index]]
                if symbol in ("。", "？"):
                    sentence_end = index
                    break
                if last_comma < 0 and symbol == "，":
                    last_comma = index
            if sentence_end < 0 and len(cache_ids) > 200 and last_comma >= 0:
                sentence_end = last_comma
                cache_labels[sentence_end] = self.model.period
            if sentence_end >= 0:
                output.extend(cache_labels[:sentence_end + 1])
                cache_ids = cache_ids[sentence_end + 1:]
                cache_labels = cache_labels[sentence_end + 1:]
        if len(output) != len(tokens):
            raise RuntimeError("标点标签数量与原文单位不一致")
        if self.model.punc_list[output[-1]] not in ("。", "？"):
            output[-1] = self.model.period
        return _punctuate_surface_text(text, output, self) or text, output


class BoundedVadFrontend:
    """Equivalent frontend values with bounded Python lists/CMVN scratch space."""
    def __init__(self, frontend, audio_block_seconds=10, cmvn_block_frames=2048):
        self.frontend = frontend
        self.audio_block_samples = max(1, int(audio_block_seconds * frontend.opts.frame_opts.samp_freq))
        self.cmvn_block_frames = max(1, cmvn_block_frames)

    def __getattr__(self, name):
        return getattr(self.frontend, name)

    def fbank(self, waveform):
        import kaldi_native_fbank as knf
        extractor = knf.OnlineFbank(self.opts)
        for start in range(0, len(waveform), self.audio_block_samples):
            block = np.asarray(waveform[start:start + self.audio_block_samples], dtype=np.float32)
            extractor.accept_waveform(self.opts.frame_opts.samp_freq, (block * (1 << 15)).tolist())
        matrix = np.empty((extractor.num_frames_ready, self.opts.mel_opts.num_bins), dtype=np.float32)
        for index in range(len(matrix)):
            matrix[index] = extractor.get_frame(index)
        return matrix, np.int32(len(matrix))

    @staticmethod
    def apply_lfr(features, lfr_m, lfr_n):
        if not len(features):
            return np.empty((0, features.shape[1] * lfr_m), dtype=np.float32)
        count = (len(features) + lfr_n - 1) // lfr_n
        starts = np.arange(count) * lfr_n - (lfr_m - 1) // 2
        indices = np.clip(starts[:, None] + np.arange(lfr_m), 0, len(features) - 1)
        return features[indices].reshape(count, -1).astype(np.float32, copy=False)

    def apply_cmvn(self, features):
        output = np.empty(features.shape, dtype=np.float32)
        means = self.cmvn[0, :features.shape[1]]
        scales = self.cmvn[1, :features.shape[1]]
        for start in range(0, len(features), self.cmvn_block_frames):
            # Match the original float64 arithmetic then final float32 cast,
            # using bounded scratch arrays and broadcasting rather than tile.
            block = features[start:start + self.cmvn_block_frames].astype(np.float64)
            block += means
            block *= scales
            output[start:start + len(block)] = block
        return output

    def lfr_cmvn(self, features):
        if self.lfr_m != 1 or self.lfr_n != 1:
            features = self.apply_lfr(features, self.lfr_m, self.lfr_n)
        if self.cmvn_file:
            features = self.apply_cmvn(features)
        return features, np.int32(len(features))


def cluster_safely(embeddings, backend, max_speakers=10):
    """Return contiguous known labels and -1 for genuinely undetermined windows."""
    import torch
    values = np.asarray(embeddings, dtype=np.float32)
    labels = np.full(len(values), -1, dtype=int)
    valid = np.isfinite(values).all(axis=1) & (np.linalg.norm(values.astype(np.float64), axis=1) > 1e-8)
    issues = []
    if not valid.all():
        issues.append({"code": "speaker_embedding_invalid", "reason": "部分声纹为空或无效，说话人未确定",
                       "invalid_windows": int((~valid).sum())})
    kept = values[valid]
    if not len(kept):
        return labels, issues
    try:
        if len(kept) < 20:
            issues.append({"code": "speaker_samples_insufficient", "reason": "声纹样本较少，说话人区分需复核",
                           "valid_windows": len(kept)})
            if len(kept) == 1:
                selected = np.zeros(1, dtype=int)
            else:
                from scipy.cluster.hierarchy import linkage, fcluster
                selected = fcluster(linkage(kept, method="average", metric="cosine"),
                                    t=1 - backend.model_config.get("merge_thr", .78), criterion="distance") - 1
        elif len(kept) >= 2048:
            # Preserve noise (-1); upstream merge_by_cos assumes >=1 clusters.
            selected = np.asarray(backend.umap_hdbscan_cluster(kept))
            if (selected.shape != (len(kept),) or not np.isfinite(selected).all()
                    or not np.equal(selected, np.floor(selected)).all()):
                raise ValueError("invalid speaker labels")
            selected = selected.astype(int)
            known = selected >= 0
            if known.any():
                _, compact = np.unique(selected[known], return_inverse=True)
                compact = backend.merge_to_max_spks(compact, kept[known], max_speakers)
                compact = backend.merge_by_cos(compact, kept[known], backend.model_config.get("merge_thr", .78))
                selected[known] = compact
        else:
            selected = np.asarray(backend(torch.from_numpy(kept), oracle_num=None))
        if (selected.shape != (len(kept),) or not np.isfinite(selected).all()
                or not np.equal(selected, np.floor(selected)).all()):
            raise ValueError("invalid speaker labels")
        selected = selected.astype(int)
        selected[selected < 0] = -1
        known_labels, counts = np.unique(selected[selected >= 0], return_counts=True)
        if len(known_labels) > max_speakers:
            allowed = set(known_labels[np.argsort(-counts, kind="stable")[:max_speakers]])
            selected = np.array([value if value in allowed else -1 for value in selected])
        labels[valid] = selected
    except (ValueError, AssertionError, FloatingPointError) as exc:
        issues.append({"code": "speaker_clustering_failed", "reason": "声纹聚类未能可靠完成，说话人未确定",
                       "detail": str(exc)})
    if (labels[valid] < 0).any():
        issues.append({"code": "speaker_unassigned", "reason": "部分语音无法归入可靠的说话人分组",
                       "unassigned_windows": int((labels < 0).sum())})
    remap = {}
    for index, label in enumerate(labels):
        if label >= 0:
            labels[index] = remap.setdefault(int(label), len(remap))
    return labels, issues


def speaker_regions_with_unknown(segments, labels):
    """Resolve overlapping windows without merging unknown labels into a person."""
    regions = []
    for segment, label in zip(segments, labels):
        start, end = segment[:2]
        if regions and regions[-1][2] == label and start <= regions[-1][1]:
            regions[-1][1] = max(end, regions[-1][1])
        else:
            regions.append([float(start), float(end), int(label)])
    for index in range(1, len(regions)):
        if regions[index - 1][1] > regions[index][0]:
            middle = (regions[index - 1][1] + regions[index][0]) / 2
            regions[index - 1][1] = regions[index][0] = middle
    return regions


def assign_speakers_with_review(sentences, regions, review_regions=None):
    issues = []
    def overlapping(sentence, source):
        values = {}
        for start, end, label in source:
            duration = max(0, min(sentence["end"], end * 1000) - max(sentence["start"], start * 1000))
            if duration > 0:
                values[label] = values.get(label, 0) + duration
        return values
    for sentence in sentences:
        overlaps = overlapping(sentence, regions)
        sentence["spk"] = int(max(overlaps, key=overlaps.get, default=-1))
        # A temporal smoother may hide a short speaker turn. Retain that raw
        # cluster evidence for review without changing ordinary assignment.
        if review_regions is not None:
            overlaps = overlapping(sentence, review_regions)
        known = [int(label) for label in overlaps if label >= 0]
        reason = None
        if len(known) > 1:
            reason = ("speaker_change_inside_sentence", "该句跨越多个说话人，未能可靠拆句，请回听确认归属")
        elif not known or -1 in overlaps:
            reason = ("speaker_uncertain", "该句存在无法确定说话人的语音，请回听确认归属")
        if reason:
            issues.append({"start": sentence["start"], "end": sentence["end"], "text": sentence["text"],
                           "code": reason[0], "reason": reason[1], "speakers_local": known})
    return issues
