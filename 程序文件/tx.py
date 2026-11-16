#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
长录音转写 + 说话人区分 + 声纹识别 小工具
基于 FunASR（阿里达摩院开源）：Paraformer-large 中文识别 + FSMN-VAD 静音检测
+ CT-Punc 标点 + CAM++ 声纹模型。

用法见 README.md。
"""
import argparse
import fcntl
import hashlib
import json
import logging
import math
import multiprocessing
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path
from functools import lru_cache
from multiprocessing.connection import wait as wait_connections

import numpy as np
import soundfile as sf
from batch_export import export_batch_markdown, source_snapshot, verified_source_identity

AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma", ".mp4", ".mov", ".amr"}
SR = 16000
VERSION = "1.0"  # 参与断点缓存的参数指纹，改了会让旧断点失效，勿随安装包版本变
PIPELINE_VERSION = "2.3-batch-recovery-v1"

log = logging.getLogger("tx")


# ---------------------------------------------------------------- 工具函数
def fmt_ts(sec, srt=False):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = int(sec % 60)
    if srt:
        ms = int(round((sec - int(sec)) * 1000))
        if ms == 1000:
            ms = 999
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}"


def l2norm(x):
    x = np.asarray(x, dtype=np.float32)
    return x / (np.linalg.norm(x, axis=-1, keepdims=True) + 1e-8)


def pick_device(dev):
    if dev and dev != "auto":
        return dev
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
    except Exception:
        pass
    # macOS 上 MPS 对部分算子支持不全，默认用 CPU 更稳；需要时可手动 --device mps 试
    return "cpu"


NORM_FILTER = "dynaudnorm=f=150:g=7:p=0.9:m=100"
MODEL_NAMES = ("asr", "vad", "punc", "spk")  # 本地模型目录下的 4 个子目录


def resolve_ffmpeg():
    """找 ffmpeg：环境变量 TX_FFMPEG > 系统 PATH > imageio-ffmpeg 自带的（Mac 安装包用这个，免装 Homebrew）"""
    env = os.environ.get("TX_FFMPEG")
    if env:
        if os.path.isfile(env) and os.access(env, os.X_OK):
            return env
        raise RuntimeError(f"TX_FFMPEG 指向的 ffmpeg 不存在或不可执行：{env}")
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    raise RuntimeError("找不到 ffmpeg：请安装 ffmpeg，或 pip install imageio-ffmpeg")


def local_models(model_dir: Path):
    """安装包把 4 个模型下载到 model_dir/{asr,vad,punc,spk}，这里返回本地路径，转写时不联网"""
    model_dir = Path(model_dir)
    missing = [n for n in MODEL_NAMES if not (model_dir / n / "config.yaml").exists()]
    if missing:
        raise RuntimeError(f"模型不完整（缺 {', '.join(missing)}，位置 {model_dir}），请重新运行安装")
    return {n: str(model_dir / n) for n in MODEL_NAMES}


def audio_duration(src):
    """Read metadata only; do not allocate a complete long recording."""
    try:
        info = sf.info(str(src))
        duration = info.frames / info.samplerate
    except (RuntimeError, OSError):
        probe = subprocess.run([resolve_ffmpeg(), "-hide_banner", "-i", str(src)],
                               capture_output=True, text=True, timeout=30)
        match = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", probe.stderr)
        if not match:
            raise RuntimeError(f"无法读取录音时长，不能确认转码磁盘需求：{src}")
        h, m, s = map(float, match.groups())
        duration = h * 3600 + m * 60 + s
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError(f"录音时长无效：{src}")
    return duration


def check_decode_space(src, dst):
    # Check the output volume, including existing jobs' decoded WAVs. Reserve
    # room for JSON/Markdown and filesystem overhead as well as 16 kHz PCM.
    need = math.ceil(audio_duration(src) * SR * 2) + 256 * 1024 * 1024
    free = shutil.disk_usage(dst.parent).free
    if free < need:
        raise RuntimeError(f"输出磁盘空间不足：{dst.parent} 需要约 {need / 1e9:.2f} GB，"
                           f"剩余 {free / 1e9:.2f} GB；原始录音已保留")


def decode_to_wav(src: Path, dst: Path, norm: bool = False):
    """用 ffmpeg 转成 16k 单声道 16bit wav（大疆 Mic 的 48k/32bit 浮点 wav 也能处理）。
    norm=True 时做动态音量均衡：胸麦录音里离得远的人声音很小，不拉起来会被当成静音丢掉。
    （实测：远场会议录音不做均衡，一半的话直接没识别出来）"""
    if dst.exists():
        try:
            info = sf.info(str(dst))
            if info.samplerate == SR and info.channels == 1 and info.frames > 0:
                expected_frames = round(audio_duration(src) * SR)
                if abs(info.frames - expected_frames) <= SR // 10:
                    return
        except (RuntimeError, OSError):
            pass
        dst.unlink()
    check_decode_space(src, dst)
    fd, temporary = tempfile.mkstemp(prefix=f".{dst.stem}.", suffix=".wav", dir=dst.parent)
    os.close(fd)
    tmp = Path(temporary)
    cmd = [resolve_ffmpeg(), "-nostdin", "-y", "-loglevel", "error", "-i", str(src),
           "-vn", "-ac", "1", "-ar", str(SR)] + (["-af", NORM_FILTER] if norm else []) + \
          ["-c:a", "pcm_s16le", str(tmp)]
    try:
        subprocess.run(cmd, check=True)
        info = sf.info(str(tmp))
        if info.frames <= 0 or info.samplerate != SR or info.channels != 1:
            raise RuntimeError("转码未生成有效的 16 kHz 单声道音频")
        tmp.replace(dst)
    finally:
        tmp.unlink(missing_ok=True)


def plan_chunks(wav: Path, chunk_sec: float, search_sec: float = 20.0):
    """把长音频切成约 chunk_sec 秒的段，切点选在附近最安静的地方，尽量不把一句话切断。"""
    info = sf.info(str(wav))
    total = info.frames / info.samplerate
    bounds = [0.0]
    pos = 0.0
    while total - pos > chunk_sec * 1.3:
        target = pos + chunk_sec
        a = max(pos + chunk_sec * 0.5, target - search_sec)
        b = min(total, target + search_sec)
        data, _ = sf.read(str(wav), start=int(a * SR), stop=int(b * SR), dtype="float32")
        frame = int(0.1 * SR)
        n = len(data) // frame
        if n > 0:
            e = (data[: n * frame].reshape(n, frame) ** 2).mean(1)
            # 用 0.5 秒滑动平均，找最安静的点
            k = 5
            if n > k:
                e = np.convolve(e, np.ones(k) / k, mode="same")
            cut = a + (int(np.argmin(e)) + 0.5) * 0.1
        else:
            cut = target
        bounds.append(cut)
        pos = cut
    bounds.append(total)
    return [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)], total


def peak_mem_mb():
    try:
        import resource
        r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(r / 1024 / (1024 if sys.platform == "darwin" else 1), 0)
    except Exception:
        return None


# ---------------------------------------------------------------- 模型
class Engine:
    def __init__(self, device="cpu", threads=4, hub="auto", max_spk=10, asr_model="auto", model_dir=None):
        from funasr import AutoModel
        self.device = device
        if hub == "auto":
            hub = os.environ.get("TX_HUB", "hf")
        kw = dict(device=device, ncpu=threads, disable_update=True, hub=hub)
        log.info("加载模型中……" if model_dir else "加载模型中（第一次会自动下载约 1GB 模型）……")
        t = time.time()
        names = {"vad": "fsmn-vad", "punc": "ct-punc", "spk": "cam++"}
        if model_dir:
            lm = local_models(Path(model_dir))
            names = {k: lm[k] for k in ("vad", "punc", "spk")}
            if asr_model == "auto":
                asr_model = lm["asr"]
            log.info("使用本地模型目录：%s", model_dir)
        if asr_model == "auto":
            local = Path(__file__).parent / "models" / "seaco_paraformer"
            # SeACo-Paraformer 支持热词；HF 上没有它的权重，所以：本地有就用本地，魔搭(ms)下载也是它，
            # 走 HF 时退回普通 Paraformer-large（识别一样，只是热词不生效）
            asr_model = str(local) if (local / "model.pt").exists() else "paraformer-zh"
        log.info("识别模型：%s", asr_model)
        self.asr = AutoModel(model=asr_model, vad_model=names["vad"],
                             vad_kwargs={"max_single_segment_time": 30000},
                             punc_model=names["punc"], spk_model=names["spk"],
                             max_num_spks=max_spk, **kw)
        self.sv = AutoModel(model=names["spk"], **kw)
        spk_weight = Path(names["spk"]) / "campplus_cn_common.bin"
        self.voiceprint_fingerprint = ("campplus-window-v1:" + content_sha(spk_weight)
                                       if spk_weight.is_file() else str(names["spk"]))
        log.info("模型加载完成，用时 %.1f 秒", time.time() - t)

    def embed(self, audio: np.ndarray):
        """对一段音频按 1.5 秒窗口取声纹并平均"""
        win, hop = int(1.5 * SR), int(0.75 * SR)
        segs = []
        for st in range(0, max(1, len(audio) - win + 1), hop):
            seg = audio[st: st + win]
            if len(seg) < win:
                seg = np.pad(seg, (0, win - len(seg)))
            segs.append(seg)
        res = self.sv.generate(input=segs, disable_pbar=True)
        embs = np.stack([r["spk_embedding"].cpu().numpy().reshape(-1) for r in res])
        return l2norm(l2norm(embs).mean(0))

    def run_chunk(self, audio: np.ndarray, offset: float, hotword: str = ""):
        kw = dict(input=audio, batch_size_s=300, return_spk_center=True, disable_pbar=True)
        if hotword:
            kw["hotword"] = hotword
        res = self.asr.generate(**kw)
        sents, centers = [], []
        if res:
            r = res[0]
            for s in r.get("sentence_info", []) or []:
                txt = (s.get("text") or s.get("sentence") or "").strip()
                if not txt:
                    continue
                sents.append({"start": round(s["start"] / 1000 + offset, 3),
                              "end": round(s["end"] / 1000 + offset, 3),
                              "text": txt, "spk_local": int(s.get("spk", 0))})
            c = r.get("spk_embedding_center")
            if c is not None:
                centers = np.asarray(c).reshape(len(c), -1).tolist()
            elif sents:
                centers = []
        # 某些很短的段落（<20 个窗口）FunASR 直接判为一个人且可能没有中心，这里自己补算
        need = sorted({s["spk_local"] for s in sents})
        if sents and (not centers or len(centers) <= max(need)):
            centers = []
            for k in range(max(need) + 1):
                parts = [audio[int((s["start"] - offset) * SR): int((s["end"] - offset) * SR)]
                         for s in sents if s["spk_local"] == k]
                parts = [p for p in parts if len(p) > 0]
                if parts:
                    centers.append(self.embed(np.concatenate(parts)).tolist())
                else:
                    centers.append([0.0] * 192)
        return sents, centers


# ---------------------------------------------------------------- 声纹库
def load_enroll(engine: Engine, enroll_dir: Path):
    """声纹库目录结构：
        voiceprints/张三.wav          （一个人一个文件）
        voiceprints/李四/1.wav 2.m4a  （或一个人一个文件夹，多段样本）
    """
    if not enroll_dir or not enroll_dir.exists():
        return {}
    cache = enroll_dir / ".声纹缓存.json"
    cached = read_json(cache)
    people = {}
    for p in sorted(enroll_dir.iterdir()):
        if p.name.startswith("."):
            continue
        if p.is_dir():
            files = [f for f in sorted(p.rglob("*")) if f.suffix.lower() in AUDIO_EXT]
            name = p.name
        elif p.suffix.lower() in AUDIO_EXT:
            files = [p]
            name = p.stem
        else:
            continue
        if not files:
            continue
        key = name + "|" + getattr(engine, "voiceprint_fingerprint", "campplus-window-v1") + "|" + \
              "|".join(f"{f.name}:{content_sha(f)}" for f in files)
        if key in cached and valid_embedding(cached[key]):
            people[name] = np.array(cached[key], dtype=np.float32)
            continue
        auds = []
        for f in files:
            fd, temporary = tempfile.mkstemp(prefix=".声纹转码-", suffix=".wav", dir=enroll_dir)
            os.close(fd)
            tmp = Path(temporary)
            tmp.unlink()
            try:
                decode_to_wav(f, tmp)
                a, _ = sf.read(str(tmp), dtype="float32")
            finally:
                tmp.unlink(missing_ok=True)
            auds.append(a)
        a = np.concatenate(auds)
        if len(a) < SR * 3:
            log.warning("【%s】的声纹样本太短（%.1f 秒），建议至少 10 秒清晰独白", name, len(a) / SR)
        emb = engine.embed(a)
        people[name] = emb
        cached[key] = emb.tolist()
        log.info("已登记声纹：%s（样本 %.0f 秒）", name, len(a) / SR)
    try:
        write_json_atomic(cache, cached)
    except Exception:
        pass
    return people


# ---------------------------------------------------------------- 全局说话人合并
def valid_embedding(value, dimension=None):
    try:
        v = np.asarray(value, dtype=np.float64)
        return (v.ndim == 1 and v.size > 0 and (dimension is None or v.size == dimension)
                and bool(np.isfinite(v).all()) and np.linalg.norm(v) > 1e-8)
    except (TypeError, ValueError):
        return False


def normalize_chunk_speakers(chunk):
    """Keep words with unusable embeddings, but never assign a fabricated voice."""
    old_centers = chunk["centers"]
    centers, mapping = [], {}
    dimension = None
    for i, center in enumerate(old_centers):
        if valid_embedding(center, dimension):
            dimension = len(center)
            mapping[i] = len(centers)
            centers.append(center)
    for sentence in chunk["sentences"]:
        local = sentence["spk_local"]
        if local in mapping:
            sentence["spk_local"] = mapping[local]
        elif local != -1:
            sentence["spk_local"] = -1
            chunk.setdefault("review", []).append({"start": sentence["start"], "end": sentence["end"],
                "text": sentence.get("text", ""), "reason": "声纹中心无效，说话人未确定，请回听。"})
    chunk["centers"] = centers


def global_speakers(chunks, merge_thr, people, enroll_thr):
    """每段先各自分好说话人，再把所有段的说话人中心按相似度合并成全天统一编号，
    最后跟声纹库比对，认出来的换成名字。"""
    items = []  # (chunk_idx, local_id, emb, talk_sec)
    unknown = {}
    dimension = None
    for ci, ch in enumerate(chunks):
        dur = {}
        for s in ch["sentences"]:
            dur[s["spk_local"]] = dur.get(s["spk_local"], 0) + (s["end"] - s["start"])
        for li, c in enumerate(ch["centers"]):
            if li in dur and valid_embedding(c, dimension):
                dimension = len(c)
                items.append((ci, li, l2norm(c), dur[li]))
        known = {li for item_ci, li, _, _ in items if item_ci == ci}
        for li, seconds in dur.items():
            if li not in known:
                unknown[(ci, li)] = seconds
                if li != -1:
                    for sentence in ch["sentences"]:
                        if sentence["spk_local"] == li:
                            sentence["spk_local"] = -1
                            item = {"start": sentence["start"], "end": sentence["end"],
                                    "text": sentence.get("text", ""),
                                    "reason": "全局声纹中心无效或维度不一致，说话人未确定，请回听。"}
                            if item not in ch.setdefault("review", []):
                                ch["review"].append(item)
    unknown_summary = ({"说话人未确定": {"秒数": sum(unknown.values()), "声纹相似度": None}}
                       if unknown else {})
    if not items:
        return {key: "说话人未确定" for key in unknown}, unknown_summary, {}
    E = np.stack([x[2] for x in items])
    n = len(items)
    if n == 1:
        labels = np.array([0])
    else:
        from scipy.cluster.hierarchy import linkage, fcluster
        Z = linkage(E, method="average", metric="cosine")
        labels = fcluster(Z, t=1 - merge_thr, criterion="distance") - 1

    # 每个全局说话人的中心（按说话时长加权）
    G = {}
    for (ci, li, e, d), g in zip(items, labels):
        G.setdefault(int(g), {"emb": np.zeros_like(e), "sec": 0.0, "members": []})
        G[int(g)]["emb"] += e * d
        G[int(g)]["sec"] += d
        G[int(g)]["members"].append((ci, li))
    for g in G.values():
        g["emb"] = l2norm(g["emb"])

    # 声纹匹配：相似度最高的一对先配，一人只配一个
    names = {}
    scores = {}
    people = {nm: l2norm(pe) for nm, pe in people.items() if valid_embedding(pe, E.shape[1])}
    if people:
        pairs = []
        for g, v in G.items():
            for nm, pe in people.items():
                pairs.append((float(np.dot(v["emb"], pe)), g, nm))
        pairs.sort(reverse=True)
        used_g, used_n = set(), set()
        for sc, g, nm in pairs:
            if sc < enroll_thr:
                break
            if g in used_g or nm in used_n:
                continue
            names[g] = nm
            scores[g] = sc
            used_g.add(g)
            used_n.add(nm)
        # 没配上的簇，如果和某个已登记的人也很像（同一人被分成了两簇），也归给他
        for g, v in G.items():
            if g in names:
                continue
            best = max(((float(np.dot(v["emb"], pe)), nm) for nm, pe in people.items()), default=(0, None))
            if best[1] and best[0] >= enroll_thr + 0.1:
                names[g] = best[1]
                scores[g] = best[0]
    # 其余按说话时长排序编号
    rest = sorted([g for g in G if g not in names], key=lambda g: -G[g]["sec"])
    used_names = set(people) | set(names.values()) | {"说话人未确定"}
    number = 1
    for g in rest:
        while f"说话人{number}" in used_names:
            number += 1
        names[g] = f"说话人{number}"
        used_names.add(names[g])
    local2name = {key: "说话人未确定" for key in unknown}
    for g, v in G.items():
        for m in v["members"]:
            local2name[m] = names[g]
    summary = unknown_summary
    for g, v in G.items():
        nm = names[g]
        summary.setdefault(nm, {"秒数": 0.0, "声纹相似度": None})
        summary[nm]["秒数"] += v["sec"]
        if g in scores:
            summary[nm]["声纹相似度"] = round(scores[g], 3)
    # 声纹比对明细：每个登记的人和最像的 3 个说话人的相似度（方便调门槛）
    detail = {}
    for nm, pe in people.items():
        sims = sorted(((float(np.dot(v["emb"], pe)), names[g]) for g, v in G.items()), reverse=True)[:3]
        detail[nm] = [{"说话人": n2, "相似度": round(sc, 3)} for sc, n2 in sims]
    return local2name, summary, detail


# ---------------------------------------------------------------- 输出
def merge_paragraphs(sents, gap=2.0, max_len=400):
    paras = []
    for s in sents:
        if paras and paras[-1]["speaker"] == s["speaker"] and s["start"] - paras[-1]["end"] <= gap \
                and len(paras[-1]["text"]) < max_len:
            paras[-1]["text"] += s["text"]
            paras[-1]["end"] = s["end"]
        else:
            paras.append({"start": s["start"], "end": s["end"], "speaker": s["speaker"], "text": s["text"]})
    return paras


def write_outputs(out_dir: Path, name: str, sents, summary, total, part_min, meta):
    out_dir.mkdir(parents=True, exist_ok=True)
    # JSON（完整，逐句）
    write_json_atomic(out_dir / f"{name}.json",
        {"文件": meta["source"], "时长秒": round(total, 1), "说话人统计": summary, "声纹比对明细": meta.get("vp_detail", {}),
         "生成时间": time.strftime("%Y-%m-%d %H:%M:%S"), "参数": meta["params"],
         "句子": sents, "待复核": meta.get("review", [])})
    # SRT
    write_text_atomic(out_dir / f"{name}.srt", "".join(
        f"{i}\n{fmt_ts(s['start'], True)} --> {fmt_ts(s['end'], True)}\n{s['speaker']}：{s['text']}\n\n"
        for i, s in enumerate(sents, 1)))
    # Markdown 分段文件
    paras = merge_paragraphs(sents)
    part_sec = part_min * 60
    n_parts = max(1, int(np.ceil(total / part_sec)))
    md_dir = out_dir / "分段"
    if md_dir.exists():
        shutil.rmtree(md_dir)
    md_dir.mkdir()
    index_rows = []
    for pi in range(n_parts):
        a, b = pi * part_sec, min(total, (pi + 1) * part_sec)
        # Partition sentences before merging adjacent paragraphs: a paragraph
        # crossing a 30-minute boundary must not swallow the next part's text.
        ps = merge_paragraphs([s for s in sents if a <= s["start"] < b])
        fn = f"{name}_第{pi + 1:02d}段_{fmt_ts(a).replace(':', '')}-{fmt_ts(b).replace(':', '')}.md"
        spk_cnt = {}
        chars = 0
        for p in ps:
            spk_cnt[p["speaker"]] = spk_cnt.get(p["speaker"], 0) + len(p["text"])
            chars += len(p["text"])
        lines = [f"# {name} 第 {pi + 1}/{n_parts} 段（{fmt_ts(a)} – {fmt_ts(b)}）", "",
                 f"- 原始录音：{meta['source']}",
                 f"- 本段说话人：{'、'.join(k for k, _ in sorted(spk_cnt.items(), key=lambda x: -x[1])) or '（无人声）'}",
                 f"- 本段字数：{chars}", ""]
        for p in ps:
            lines.append(f"**[{fmt_ts(p['start'])}] {p['speaker']}**：{p['text']}")
            lines.append("")
        write_text_atomic(md_dir / fn, "\n".join(lines))
        index_rows.append((fn, a, b, spk_cnt, chars))
    # 目录
    idx = [f"# {name} 转写目录", "",
           f"- 原始录音：{meta['source']}",
           f"- 总时长：{fmt_ts(total)}",
           f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M')}", "",
           "## 说话人", "", "| 说话人 | 说话时长 | 声纹匹配相似度 |", "|---|---|---|"]
    for k, v in sorted(summary.items(), key=lambda x: -x[1]["秒数"]):
        sim = "" if v["声纹相似度"] is None else f"{v['声纹相似度']:.2f}"
        idx.append(f"| {k} | {fmt_ts(v['秒数'])} | {sim} |")
    idx += ["", "## 分段文件", "", "| 文件 | 时间 | 字数 | 主要说话人 |", "|---|---|---|---|"]
    for fn, a, b, cnt, chars in index_rows:
        top = "、".join(k for k, _ in sorted(cnt.items(), key=lambda x: -x[1])[:3])
        idx.append(f"| [{fn}](分段/{fn}) | {fmt_ts(a)}–{fmt_ts(b)} | {chars} | {top} |")
    idx += ["", "> 说明：说话人是机器根据声音自动区分的，可能把同一人分成两个、或把声音相近的人合并；"
                "时间戳为相对录音开头的时间。"]
    write_text_atomic(out_dir / "目录.md", "\n".join(idx))
    # 一个完整版 md，方便全文搜索
    full = [f"# {name} 全文", ""] + [f"**[{fmt_ts(p['start'])}] {p['speaker']}**：{p['text']}\n" for p in paras]
    write_text_atomic(out_dir / f"{name}_全文.md", "\n".join(full))
    write_review(out_dir, meta.get("review", []))


# ---------------------------------------------------------------- 主流程
def read_json(path):
    try:
        data = json.loads(path.read_text("utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def valid_sentence(sentence, start, end):
    return (isinstance(sentence, dict) and isinstance(sentence.get("text"), str)
            and all(finite_number(sentence.get(k)) for k in ("start", "end"))
            and start - .001 <= sentence["start"] < sentence["end"] <= end + .001)


def payload_sha(data):
    body = {key: value for key, value in data.items() if key != "payload_sha256"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def seal_payload(data):
    return {**data, "payload_sha256": payload_sha(data)}


def valid_plan(plan):
    try:
        if (not finite_number(plan.get("total")) or plan["total"] <= 0
                or not isinstance(plan.get("chunks"), list) or not plan["chunks"]
                or plan.get("payload_sha256") != payload_sha(plan)):
            return False
        previous = 0
        for bounds in plan["chunks"]:
            if (not isinstance(bounds, (list, tuple)) or len(bounds) != 2
                    or not all(finite_number(t) for t in bounds)):
                return False
            a, b = bounds
            if abs(a - previous) > 1 / SR or not a < b <= plan["total"] + 1 / SR:
                return False
            previous = b
        return abs(previous - plan["total"]) <= 1 / SR
    except (TypeError, ValueError):
        return False


def valid_chunk(chunk, start, end):
    try:
        if (not isinstance(chunk, dict) or not all(finite_number(chunk.get(k)) for k in ("start", "end"))
                or abs(chunk["start"] - start) > 1 / SR or abs(chunk["end"] - end) > 1 / SR
                or not isinstance(chunk.get("sentences"), list)
                or not isinstance(chunk.get("centers"), list)
                or not isinstance(chunk.get("review"), list)
                or chunk.get("payload_sha256") != payload_sha(chunk)):
            return False
        centers = chunk["centers"]
        metric = chunk.get("metrics")
        if (not isinstance(metric, dict) or type(metric.get("index")) is not int
                or type(metric.get("worker_pid")) is not int or metric["worker_pid"] <= 0
                or not all(finite_number(metric.get(k)) for k in ("started_at", "finished_at"))
                or metric["finished_at"] < metric["started_at"]):
            return False
        dimension = len(centers[0]) if centers else None
        if not all(valid_embedding(center, dimension) for center in centers):
            return False
        previous = start - .001
        for sentence in chunk["sentences"]:
            local = sentence.get("spk_local") if isinstance(sentence, dict) else None
            if (not valid_sentence(sentence, start, end) or sentence["start"] < previous
                    or type(local) is not int or not -1 <= local < len(centers)):
                return False
            previous = sentence["start"]
        return all(isinstance(item, dict) and isinstance(item.get("reason"), str)
                   for item in chunk["review"])
    except (TypeError, ValueError, KeyError):
        return False


def write_json_atomic(path, data):
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, allow_nan=False))


def write_text_atomic(path, text):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def source_identity(src):
    """用路径和纳秒级文件状态识别录音；替换或改写同名文件也必须重新处理。"""
    src = src.resolve(strict=True)
    st = src.stat()
    return {"path": str(src), "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns, "device": st.st_dev, "inode": st.st_ino}


def output_dir_for(src, out_root, engine="original"):
    """无冲突时沿用录音名目录；同名文件及无身份的旧结果各自保留。"""
    if engine == "light":
        out_root = out_root / "轻量试用"
    source_path = str(src.resolve())
    base = out_root / src.stem
    suffix = hashlib.sha256(source_path.encode()).hexdigest()[:12]
    number = 0
    while True:
        candidate = base if number == 0 else out_root / (
            f"{src.stem}_{suffix}" + (f"_{number}" if number > 1 else ""))
        if not candidate.exists():
            return candidate
        if candidate.is_dir():
            owner = read_json(candidate / ".work" / "source.json")
            if owner.get("path") == source_path or not any(candidate.iterdir()):
                return candidate
        number += 1


@lru_cache(maxsize=512)
def _content_sha(path, size, mtime_ns, ctime_ns, inode):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def content_sha(path):
    path = Path(path).resolve()
    st = path.stat()
    return _content_sha(str(path), st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino)


def model_identity(args):
    """实际内容参与指纹；进程内按文件状态缓存散列，防止换权重仍误用完成结果。"""
    result = {}
    roots = []
    if args.model_dir:
        roots.append(("models", Path(args.model_dir).expanduser()))
    if args.asr_model != "auto" and Path(args.asr_model).expanduser().exists():
        roots.append(("asr_override", Path(args.asr_model).expanduser()))
    for label, root in roots:
        files = [root] if root.is_file() else []
        seen = set()
        for directory, subdirs, names in os.walk(root, followlinks=True):
            resolved = Path(directory).resolve()
            if resolved in seen:
                subdirs[:] = []
                continue
            seen.add(resolved)
            subdirs[:] = [name for name in subdirs if not name.startswith(".")]
            files.extend(Path(directory) / name for name in names if not name.startswith("."))
        files.sort()
        for f in files:
            if f.is_file() and not f.name.startswith("."):
                key = f"{label}/{f.relative_to(root) if f != root else f.name}"
                result[key] = content_sha(f)
    return result


def chunk_params(args):
    # argparse 的默认整数 15 与显式输入 15.0 表示同一时长，须共用缓存。
    # 整数值仍按 int 序列化，保留已有默认配置的缓存指纹。
    minutes = float(args.chunk_min)
    minutes = int(minutes) if minutes.is_integer() else minutes
    params = {"engine": getattr(args, "engine", "original"),
            "device": getattr(args, "device", "auto"),
            "chunk_min": minutes, "hotword": args.hotword_text, "asr": args.asr_model,
            "model_dir": str(Path(args.model_dir).expanduser().resolve()) if args.model_dir else None,
            "models": model_identity(args), "max_spk": args.max_spk,
            "norm": not args.no_norm, "norm_filter": None if args.no_norm else NORM_FILTER,
            "v": VERSION, "pipeline": PIPELINE_VERSION}
    if params["engine"] == "light":
        params["light_runtime_sha256"] = {name: content_sha(Path(__file__).parent / name)
            for name in ("light_engine.py", "light_postprocess.py") if (Path(__file__).parent / name).is_file()}
    return params


def completion_config(args):
    voiceprints = {}
    vp = Path(getattr(args, "voiceprints", ""))
    if getattr(args, "voiceprints", None) and vp.is_dir():
        voiceprints = {str(p.relative_to(vp)): content_sha(p) for p in sorted(vp.rglob("*"))
                      if p.is_file() and p.suffix.lower() in AUDIO_EXT and not p.name.startswith(".")}
    return {**chunk_params(args), "merge_thr": args.merge_thr, "enroll_thr": args.enroll_thr,
            "part_min": args.part_min, "voiceprints": voiceprints}


def is_completed(src, out_root, args=None, *, allow_metadata_change=False):
    try:
        identity = source_identity(src)
        out_dir = output_dir_for(src, out_root, getattr(args, "engine", "original"))
        completed = read_json(out_dir / ".work" / "completed.json")
        files, hashes = completed.get("outputs"), completed.get("output_sha256")
        matched = completed.get("source") == identity
        if not matched and allow_metadata_change:
            verified_source_identity(src, completed.get("source"), completed.get("source_sha256"))
            matched = True
        if (not matched
                or (args is not None and completed.get("config") != completion_config(args))
                or not isinstance(files, list) or not files or not isinstance(hashes, dict)
                or not all(isinstance(p, str) for p in files)
                or set(files) != set(hashes)):
            return False
        required = {"目录.md", f"{src.stem}.json", f"{src.stem}.srt", f"{src.stem}_全文.md",
                    "待复核.md", "待复核.json"}
        if not required.issubset(files) or not any(p.startswith("分段/") for p in files):
            return False
        for relative in files:
            p = Path(relative)
            if p.is_absolute() or ".." in p.parts or content_sha(out_dir / p) != hashes[relative]:
                return False
            if p.suffix != ".srt" and (out_dir / p).stat().st_size == 0:
                return False
        data = read_json(out_dir / f"{src.stem}.json")
        total, sents = data.get("时长秒"), data.get("句子")
        if (not finite_number(total) or total <= 0 or not isinstance(sents, list)
                or not isinstance(data.get("待复核"), list)
                or not isinstance(data.get("文件"), str) or Path(data["文件"]).resolve() != src.resolve()
                or data.get("待复核") != read_json(out_dir / "待复核.json").get("items")):
            return False
        if sents and (out_dir / f"{src.stem}.srt").stat().st_size == 0:
            return False
        previous = -1
        for i, s in enumerate(sents, 1):
            if (not isinstance(s, dict) or s.get("id") != f"s{i:06d}"
                    or not valid_sentence(s, 0, total + .051)
                    or s["start"] < previous or not isinstance(s.get("speaker"), str)):
                return False
            previous = s["start"]
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


def engine_config(args):
    return {"engine": getattr(args, "engine", "original"), "device": getattr(args, "device", "auto"),
            "threads": getattr(args, "threads", max(1, os.cpu_count() or 4)),
            "max_spk": args.max_spk, "asr_model": args.asr_model,
            "model_dir": args.model_dir}


def make_engine(config):
    import torch
    torch.set_num_threads(config["threads"])
    try:
        torch.set_num_interop_threads(1)
    except (AttributeError, RuntimeError):
        pass  # 串行处理后续文件时，Torch 的 interop 线程数已经固定。
    config = dict(config)
    kind = config.pop("engine", "original")
    cls = Engine
    if kind == "light":
        from light_engine import LightEngine
        cls = LightEngine
    return cls(**{**config, "device": pick_device(config["device"])})


def write_review(out_dir, items):
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(out_dir / "待复核.json", {"items": items})
    lines = ["# 待复核片段", "", "这里只列出程序能够检测的问题，空清单不代表识别完全准确。",
             "姓名、数字、日期、否定词和说话人仍需按原录音核对。", ""]
    if not items:
        lines.append("本次未检测到需要自动标记的问题。")
    for item in items:
        start, end = item.get("start"), item.get("end")
        stamp = f"{fmt_ts(start)}–{fmt_ts(end)}" if all(
            isinstance(t, (float, int)) and math.isfinite(t) for t in (start, end)) else "时间待核对"
        lines.append(f"- [{stamp}] {item['reason']}")
        if item.get("text"):
            lines.append(f"  模型原文：{item['text']}")
    write_text_atomic(out_dir / "待复核.md", "\n".join(lines) + "\n")


def validate_sentences(sents, start, end):
    valid, review = [], []
    for s in sents:
        a, b = s.get("start"), s.get("end")
        # CIF timestamps use 60 ms frames. A single edge frame can extend beyond
        # the exact sample cut; clip that quantization edge without dropping text.
        if (all(isinstance(t, (float, int)) and math.isfinite(t) for t in (a, b))
                and start - 0.061 <= a < b <= end + 0.061):
            a, b = max(start, a), min(end, b)
            a, b = round(a, 3), round(b, 3)
            s = {**s, "start": a, "end": b}
        if (not all(isinstance(t, (float, int)) and math.isfinite(t) for t in (a, b))
                or not start - 0.001 <= a < b <= end + 0.001):
            review.append({"start": start, "end": end, "reason": "识别时间戳无效，原文未写入字幕，请回听。",
                           "text": s.get("text", ""), "raw_start": str(a), "raw_end": str(b)})
        else:
            valid.append(s)
    return valid, review


def transcribe_chunk(engine, task):
    """每个任务独占一个缓存文件；先落盘再发通知，中断时保留已完成分段。"""
    a, b = task["start"], task["end"]
    started = time.time()
    pid = os.getpid()
    log.info("  开始第 %d/%d 段（%s–%s），PID=%d",
             task["index"] + 1, task["total"], fmt_ts(a), fmt_ts(b), pid)
    audio, _ = sf.read(task["wav"], start=int(a * SR), stop=int(b * SR), dtype="float32")
    sents, centers = engine.run_chunk(audio, a, task["hotword"])
    sents, invalid = validate_sentences(sents, a, b)
    review = list(getattr(engine, "review_items", [])) + invalid
    finished = time.time()
    metrics = {"index": task["index"], "worker_pid": pid, "started_at": started,
               "finished_at": finished, "peak_mem_mb": peak_mem_mb()}
    chunk = {"start": a, "end": b, "sentences": sents, "centers": centers,
             "review": review, "metrics": metrics}
    normalize_chunk_speakers(chunk)
    chunk["sentences"].sort(key=lambda s: s["start"])
    write_json_atomic(Path(task["cache"]), seal_payload(chunk))
    log.info("  完成第 %d/%d 段，PID=%d，用时 %.1f 秒",
             task["index"] + 1, task["total"], pid, finished - started)
    return metrics


def parallel_worker(task_reader, result_writer, config, tasks):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    log.setLevel(logging.INFO)
    engine = None
    while True:
        try:
            task_id = task_reader.recv()
        except EOFError:
            return
        if task_id is None:
            return
        task = tasks[task_id]
        try:
            if engine is None:
                engine = make_engine(config)
            if task["kind"] == "enroll":
                people = load_enroll(engine, Path(task["path"]))
                result_writer.send({"kind": "enroll", "people": people})
            else:
                transcribe_chunk(engine, task)
                result_writer.send({"kind": "chunk", "index": task["index"]})
        except BaseException:
            result_writer.send({"kind": "error", "index": task.get("index"),
                                "pid": os.getpid(), "error": traceback.format_exc()})
            return


def stop_workers(processes, abort):
    """清理期间忽略第二次 Ctrl+C，所有等待有界，强杀后回收子进程。"""
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        started = [p for p in processes if p.pid is not None]
        if abort:
            for p in started:
                if p.is_alive():
                    p.terminate()
        deadline = time.monotonic() + 3
        for p in started:
            p.join(timeout=max(0, deadline - time.monotonic()))
        for p in started:
            if p.is_alive():
                p.kill()
        for p in started:
            p.join(timeout=1)
            if p.is_alive():
                log.error("未能回收转写进程 PID=%d", p.pid)
            else:
                p.close()
    finally:
        signal.signal(signal.SIGINT, previous)


class FileTaskSchedule:
    """Keep at most one active file per slot; spare slots split the active files.

    Files are admitted in input order. A completed file releases its slot before
    the next dispatch, while its cached chunks keep their original indices.
    """
    def __init__(self, tasks, slots):
        self.tasks, self.slots = tasks, slots
        self.enroll = [i for i, task in enumerate(tasks) if task["kind"] == "enroll"]
        self.groups, self.active, self.waiting, self.inflight = {}, [], [], {}
        for i, task in enumerate(tasks):
            if task["kind"] != "chunk":
                continue
            key = task.get("source", "__single_file__")
            if key not in self.groups:
                self.groups[key] = []
                self.waiting.append(key)
                self.inflight[key] = 0
            self.groups[key].append(i)

    def take(self):
        if self.enroll:
            return self.enroll.pop(0)
        while self.waiting and len(self.active) < self.slots:
            self.active.append(self.waiting.pop(0))
        ready = [key for key in self.active if self.groups[key]]
        if not ready:
            return None
        key = min(ready, key=lambda name: self.inflight[name])
        task_id = self.groups[key].pop(0)
        self.inflight[key] += 1
        return task_id

    def finish(self, task_id):
        task = self.tasks[task_id]
        if task["kind"] == "chunk":
            key = task.get("source", "__single_file__")
            self.inflight[key] -= 1
            if not self.groups[key] and self.inflight[key] == 0:
                self.active.remove(key)


def run_parallel_tasks(tasks, config, workers, worker_target=None,
                       on_task_started=None, on_task_completed=None):
    """spawn + 独立 Pipe，无队列后台线程；每个 worker 同时只领一个任务。"""
    if not tasks:
        return {"metrics": [], "people": None, "workers": 0}
    chunk_tasks = [t for t in tasks if t["kind"] == "chunk"]
    count = min(4, max(1, workers), max(1, len(chunk_tasks)), max(1, config["threads"]))
    config = {**config, "threads": max(1, config["threads"] // count)}
    ctx = multiprocessing.get_context("spawn")
    processes, connections, channels = [], [], {}
    received, metrics, people = set(), [], None
    abort = True
    env_names = ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS")
    old_env = {k: os.environ.get(k) for k in env_names}
    try:
        for key in env_names:
            os.environ[key] = str(config["threads"])
        log.info("启动 %d 个转写进程，每个进程 %d 个 CPU 线程", count, config["threads"])
        for _ in range(count):
            task_reader, task_writer = ctx.Pipe(duplex=False)
            result_reader, result_writer = ctx.Pipe(duplex=False)
            connections.extend((task_reader, task_writer, result_reader, result_writer))
            p = ctx.Process(target=worker_target or parallel_worker,
                            args=(task_reader, result_writer, config, tasks))
            processes.append(p)
            p.start()
            task_reader.close()
            result_writer.close()
            channels[result_reader] = (task_writer, p)
        # 任务描述（包括热词）随 spawn 参数传入，Pipe 只发小整数，故障时不会阻塞大包发送。
        scheduler = FileTaskSchedule(tasks, count)
        in_flight = {}

        def dispatch():
            for reader, (writer, _) in channels.items():
                if reader in in_flight:
                    continue
                task_id = scheduler.take()
                if task_id is None:
                    continue
                writer.send(task_id)
                in_flight[reader] = task_id
                if on_task_started:
                    on_task_started(tasks[task_id])

        dispatch()
        while len(received) < len(tasks):
            ready = wait_connections(list(channels), timeout=0.2)
            if not ready:
                broken = [p for p in processes if p.exitcode not in (None, 0)]
                if broken:
                    raise RuntimeError("转写进程异常退出：" + ", ".join(
                        f"PID={p.pid} exit={p.exitcode}" for p in broken))
                if all(p.exitcode is not None for p in processes):
                    raise RuntimeError("转写进程已退出，但部分分段没有完成通知；已写好的缓存会保留")
                continue
            for reader in ready:
                writer, p = channels[reader]
                try:
                    result = reader.recv()
                except (EOFError, OSError) as exc:
                    if reader in in_flight:
                        raise RuntimeError(f"转写进程异常退出或通信中断：PID={p.pid} exit={p.exitcode}") from exc
                    del channels[reader]
                    reader.close()
                    continue
                if result["kind"] == "error":
                    raise RuntimeError(f"转写进程 PID={result['pid']} 失败：\n{result['error']}")
                task_id = in_flight.pop(reader)
                if result["kind"] == "enroll":
                    people = result["people"]
                else:
                    index = result["index"]
                    if index != tasks[task_id]["index"]:
                        raise RuntimeError("转写进程返回的分段编号不匹配")
                    task = tasks[task_id]
                    chunk = read_json(Path(task["cache"]))
                    if not valid_chunk(chunk, task["start"], task["end"]):
                        raise RuntimeError(f"第 {index + 1} 段完成通知没有对应的完整缓存")
                    metrics.append(chunk["metrics"])
                received.add(task_id)
                scheduler.finish(task_id)
                if on_task_completed:
                    on_task_completed(tasks[task_id], result)
                dispatch()
        for writer, _ in channels.values():
            writer.send(None)
        abort = False
        return {"metrics": metrics, "people": people, "workers": count}
    except Exception as exc:
        exc.partial_people = people
        raise
    finally:
        previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            for key, value in old_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            for connection in connections:
                connection.close()
            stop_workers(processes, abort)
        finally:
            signal.signal(signal.SIGINT, previous)


def acquire_job_lock(src, out_root, args):
    """Nonblocking per-source lock, shared by run and watch across processes."""
    lock_dir = out_root / ".任务锁"
    lock_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((str(src.resolve()) + "|" + getattr(args, "engine", "original")).encode()).hexdigest()
    stream = (lock_dir / f"{key}.lock").open("a+")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise RuntimeError(f"录音正在被另一个任务处理，本次未覆盖结果：{src}")
    return stream


def release_job_lock(stream):
    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
    stream.close()


def reserve_output(src, out_root, args):
    # Reserve ownership atomically, including different sources with the same stem.
    # This short allocator lock is never held while taking any per-source lock.
    lock_dir = out_root / ".任务锁"
    with (lock_dir / "allocation.lock").open("a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        out_dir = output_dir_for(src, out_root, getattr(args, "engine", "original"))
        state = out_dir / ".work"
        state.mkdir(parents=True, exist_ok=True)
        write_json_atomic(state / "source.json", {"path": str(src.resolve())})
    return out_dir


def record_failure(out_dir, error, job=None):
    """Preserve detailed review and cached recognition when a later stage fails."""
    (out_dir / ".work" / "completed.json").unlink(missing_ok=True)
    items = read_json(out_dir / "待复核.json").get("items", [])
    items = [item for item in items if isinstance(item, dict) and isinstance(item.get("reason"), str)] \
        if isinstance(items, list) else []
    source = read_json(out_dir / ".work" / "source.json").get("path", out_dir.name)
    previous = read_json(out_dir / f"{Path(source).stem}.json").get("待复核", [])
    if isinstance(previous, list):
        for item in previous:
            if isinstance(item, dict) and isinstance(item.get("reason"), str) and item not in items:
                items.append(item)
    if job:
        for cache in job["cache_files"]:
            review = read_json(cache).get("review", [])
            for item in review if isinstance(review, list) else []:
                if isinstance(item, dict) and isinstance(item.get("reason"), str) and item not in items:
                    items.append(item)
    message = {"reason": f"处理失败，尚未完成；重新运行可续传分段：{str(error)[:1500]}",
               "kind": "processing_failure"}
    if message not in items:
        items.append(message)
    write_review(out_dir, items)


def prepare_file(src, out_dir, args):
    identity, source_digest = source_snapshot(src)
    expected_config = completion_config(args)
    state = out_dir / ".work"
    (state / "completed.json").unlink(missing_ok=True)
    source_hash = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:20]
    work = state / f"source_{source_hash}"
    work.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    params = chunk_params(args)
    # A changed normalization filter must not reuse the previous decoded waveform.
    decode_hash = hashlib.sha256(json.dumps({k: params[k] for k in ("norm", "norm_filter")},
                                            sort_keys=True).encode()).hexdigest()[:10]
    wav = work / f"audio16k_{decode_hash}.wav"
    phash = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()[:8]
    plan_f = work / f"plan_{phash}.json"
    plan = read_json(plan_f)
    if not valid_plan(plan):
        log.info("【%s】准备音频及分段计划……", src.name)
        decode_to_wav(src, wav, norm=params["norm"])
        chunks_plan, total = plan_chunks(wav, args.chunk_min * 60)
        plan = seal_payload({"chunks": chunks_plan, "total": total})
        if not valid_plan(plan):
            raise RuntimeError("分段计划无效，未提交任何完成结果")
        write_json_atomic(plan_f, plan)
    chunks_plan, total = plan["chunks"], plan["total"]
    log.info("【%s】时长 %s，分成 %d 段处理", src.name, fmt_ts(total), len(chunks_plan))
    pending, cache_files = [], []
    for i, (a, b) in enumerate(chunks_plan):
        cf = work / f"chunk_{phash}_{i:04d}.json"
        cache_files.append(cf)
        if not valid_chunk(read_json(cf), a, b):
            pending.append({"kind": "chunk", "index": i, "total": len(chunks_plan),
                            "start": a, "end": b, "wav": str(wav), "cache": str(cf),
                            "hotword": args.hotword_text})
    if pending:
        decode_to_wav(src, wav, norm=params["norm"])
    return {"src": src, "out_dir": out_dir, "state": state, "work": work, "wav": wav,
            "identity": identity, "source_sha256": source_digest, "expected_config": expected_config, "params": params,
            "chunks_plan": chunks_plan, "total": total, "pending": pending,
            "cache_files": cache_files, "started_at": t0}


def enrollment_task(args, people):
    path = Path(getattr(args, "voiceprints", ""))
    if people is None and getattr(args, "voiceprints", None) and path.is_dir() and any(
            p.is_file() and p.suffix.lower() in AUDIO_EXT for p in path.rglob("*")):
        return {"kind": "enroll", "path": str(path)}
    return None


def job_metrics(job, since):
    """Only chunks computed by this attempt count, never historic cached PIDs."""
    result = []
    for task in job["pending"]:
        chunk = read_json(Path(task["cache"]))
        metric = chunk.get("metrics", {})
        if (valid_chunk(chunk, task["start"], task["end"])
                and finite_number(metric.get("started_at")) and metric["started_at"] >= since):
            result.append(metric)
    return result


def verify_job_source(job):
    try:
        job["identity"] = verified_source_identity(job["src"], job["identity"], job.get("source_sha256"))
    except ValueError as exc:
        raise RuntimeError("录音在处理过程中发生变化，请等待文件写入完成后重试") from exc


def finalize_file(job, args, people, metrics, proc_time):
    src, out_dir = job["src"], job["out_dir"]
    chunks = []
    for cache, (a, b) in zip(job["cache_files"], job["chunks_plan"]):
        chunk = read_json(cache)
        if not valid_chunk(chunk, a, b):
            raise RuntimeError(f"分段尚未完成或缓存损坏：{cache.name}；健康分段已保留")
        chunks.append(chunk)
    # Finalization is always in source order, independent of worker completion order.
    before = [payload_sha(chunk) for chunk in chunks]
    local2name, summary, vp_detail = global_speakers(chunks, args.merge_thr, people or {}, args.enroll_thr)
    for old_sha, cache, chunk in zip(before, job["cache_files"], chunks):
        if payload_sha(chunk) != old_sha:
            write_json_atomic(cache, seal_payload(chunk))
    sents = []
    for ci, ch in enumerate(chunks):
        for s in ch["sentences"]:
            sents.append({"start": s["start"], "end": s["end"],
                          "speaker": local2name.get((ci, s["spk_local"]), "说话人未确定"),
                          "text": s["text"]})
    sents.sort(key=lambda s: s["start"])
    verify_job_source(job)
    for i, s in enumerate(sents, 1):
        s["id"] = f"s{i:06d}"
    review = [item for ch in chunks for item in ch.get("review", [])]
    meta = {"source": str(src), "vp_detail": vp_detail, "review": review,
            "params": {**job["params"], "merge_thr": args.merge_thr, "enroll_thr": args.enroll_thr}}
    write_outputs(out_dir, src.stem, sents, summary, job["total"], args.part_min, meta)
    worker_peaks = {}
    for m in metrics:
        if m.get("peak_mem_mb") is not None:
            pid = str(m["worker_pid"])
            worker_peaks[pid] = max(worker_peaks.get(pid, 0), m["peak_mem_mb"])
    proc_audio_sec = sum(t["end"] - t["start"] for t in job["pending"])
    inference_span = (max(m["finished_at"] for m in metrics) - min(m["started_at"] for m in metrics)
                      if metrics else 0)
    stat = {"音频秒": round(job["total"], 1), "本次处理用时秒": round(time.time() - job["started_at"], 1),
            "本次转写阶段用时秒": round(inference_span, 3),
            "批次共享转写阶段用时秒": round(proc_time, 3),
            "转写计时说明": "本文件从首段开始到末段完成的推理跨度，不含首次载模；批次端到端耗时见批次索引",
            "实际转写进程数": len({m["worker_pid"] for m in metrics}),
            "复用缓存分段数": len(job["chunks_plan"]) - len(job["pending"]),
            "实时率RTF": round(inference_span / proc_audio_sec, 4) if proc_audio_sec else None,
            "主进程峰值内存MB": peak_mem_mb(), "各转写进程峰值内存MB": worker_peaks,
            "内存统计说明": "各进程各自的历史峰值，不代表同时发生的总内存峰值；批次共享进程见批次索引",
            "本次分段指标": sorted(metrics, key=lambda m: m["index"])}
    write_json_atomic(job["work"] / "stat.json", stat)
    verify_job_source(job)
    if completion_config(args) != job["expected_config"]:
        raise RuntimeError("模型或声纹样本在处理过程中发生变化，请重新运行")
    outputs = ["目录.md", f"{src.stem}.json", f"{src.stem}.srt", f"{src.stem}_全文.md", "待复核.md", "待复核.json"]
    outputs += [str(p.relative_to(out_dir)) for p in sorted((out_dir / "分段").glob("*.md"))]
    hashes = {name: content_sha(out_dir / name) for name in outputs}
    write_json_atomic(job["state"] / "completed.json", {"source": job["identity"],
        "source_sha256": job["source_sha256"], "config": job["expected_config"], "outputs": outputs, "output_sha256": hashes})
    if not is_completed(src, out_dir.parent.parent if getattr(args, "engine", "original") == "light"
                        else out_dir.parent, args, allow_metadata_change=True):
        (job["state"] / "completed.json").unlink(missing_ok=True)
        raise RuntimeError("最终输出完整性校验失败，缓存保留以便重新导出")
    if not args.keep_work:
        job["wav"].unlink(missing_ok=True)
    log.info("【%s】完成 → %s", src.name, out_dir / "目录.md")
    return out_dir


def execute_tasks(tasks, engine, args, people, on_task_started=None, on_task_completed=None):
    if not tasks:
        return people
    if getattr(args, "workers", 1) > 1:
        callbacks = {}
        if on_task_started:
            callbacks["on_task_started"] = on_task_started
        if on_task_completed:
            callbacks["on_task_completed"] = on_task_completed
        result = run_parallel_tasks(tasks, engine_config(args), args.workers, **callbacks)
        return result["people"] if result["people"] is not None else people
    engine = engine or make_engine(engine_config(args))
    for task in tasks:
        if on_task_started:
            on_task_started(task)
        if task["kind"] == "enroll":
            people = load_enroll(engine, Path(task["path"]))
            result = {"kind": "enroll", "people": people}
        else:
            transcribe_chunk(engine, task)
            result = {"kind": "chunk", "index": task["index"]}
        if on_task_completed:
            on_task_completed(task, result)
    return people


def process_file(engine, src: Path, out_root: Path, args, people):
    lock = acquire_job_lock(src, out_root, args)
    out_dir, job = None, None
    try:
        out_dir = reserve_output(src, out_root, args)
        job = prepare_file(src, out_dir, args)
        enroll = enrollment_task(args, people)
        tasks = job["pending"] + ([enroll] if enroll else [])
        started = time.time()
        people = execute_tasks(tasks, engine, args, people)
        return finalize_file(job, args, people, job_metrics(job, started), time.time() - started)
    except Exception as exc:
        if out_dir:
            record_failure(out_dir, exc, job)
        raise
    finally:
        release_job_lock(lock)


def find_audio(paths, include_missing=False):
    res, seen = [], set()
    for raw in paths:
        p = Path(raw).expanduser()
        if p.is_dir():
            candidates = [f for f in sorted(p.iterdir()) if f.is_file() and f.suffix.lower() in AUDIO_EXT
                          and not f.name.startswith(".")]
        elif p.is_file() or include_missing:
            candidates = [p]
        else:
            log.warning("找不到：%s", p)
            candidates = []
        for candidate in candidates:
            key = str(candidate.resolve())
            if key not in seen:
                seen.add(key)
                res.append(candidate)
    return res


PROGRESS_PREFIX = "TX_PROGRESS "


class BatchProgress:
    """Duration-based per-file progress; total is the mean across selected files.

    Only verified chunks advance a recording. The final 2% is reserved for its
    output validation; overall 100% belongs to the validated merged document.
    """
    def __init__(self, files, enabled):
        self.enabled = enabled
        self.values = {str(path): 0.0 for path in files}
        self.chunks = {str(path): 0 for path in files}

    def emit(self, kind, **fields):
        if self.enabled:
            print(PROGRESS_PREFIX + json.dumps({"type": kind, **fields},
                  ensure_ascii=False, allow_nan=False), flush=True)

    def file(self, source, state, progress=None, **fields):
        path = str(source)
        if progress is not None:
            self.values[path] = max(self.values[path], min(1.0, max(0.0, progress)))
        if "completed_chunks" in fields:
            self.chunks[path] = fields["completed_chunks"]
        self.emit("file", path=path, state=state, progress=self.values[path], **fields)
        self.emit("status", message="正在转写…", progress=.98 * sum(self.values.values()) /
                  max(1, len(self.values)), completed_chunks=sum(self.chunks.values()))


def job_progress(job):
    remaining = job.get("remaining", {task["cache"] for task in job["pending"]})
    pending_seconds = sum(task["end"] - task["start"] for task in job["pending"]
                          if task["cache"] in remaining)
    total = max(float(job["total"]), .001)
    return {"progress": .98 * max(0, min(1, 1 - pending_seconds / total)),
            "completed_chunks": len(job["chunks_plan"]) - len(remaining),
            "total_chunks": len(job["chunks_plan"])}


def run_files(engine, files, out_root: Path, args, people):
    """One stable batch and one persistent pool; each recording keeps its own timeline."""
    out_root.mkdir(parents=True, exist_ok=True)
    files = list(dict.fromkeys(Path(f).resolve() for f in files))
    batch = {"engine": getattr(args, "engine", "original"), "started_at": time.time(),
             "status": "running", "files": [{"index": i, "source": str(f),
                "result": None, "status": "pending"} for i, f in enumerate(files)],
             "worker_pids": [], "metrics": []}
    history = out_root / ".批次"
    history.mkdir(exist_ok=True)
    index_path = history / f"{time.time_ns()}-{os.getpid()}.json"
    batch["index_path"] = str(index_path)

    def save_batch():
        write_json_atomic(index_path, batch)
        write_json_atomic(out_root / "批次索引.json", batch)

    save_batch()
    # GUI uses its own batch pointer, so another task cannot substitute a stale result.
    if getattr(args, "batch_id_file", None):
        write_text_atomic(Path(args.batch_id_file).expanduser(), str(index_path.resolve()) + "\n")
    prepared, locks = [], []
    progress = BatchProgress(files, getattr(args, "progress_events", False))
    for source in files:
        progress.file(source, "queued", 0)
    try:
        for entry, src in zip(batch["files"], files):
            out_dir, lock = None, None
            try:
                # Check existence before creating output ownership for absent inputs.
                source_identity(src)
                lock = acquire_job_lock(src, out_root, args)
                locks.append(lock)
                if not args.force and is_completed(src, out_root, args):
                    entry.update(status="skipped", result=str(output_dir_for(
                        src, out_root, getattr(args, "engine", "original"))))
                    log.info("跳过（已完成）：%s，要重新导出加 --force", src.name)
                    release_job_lock(locks.pop())
                    progress.file(src, "completed", 1)
                    save_batch()
                    continue
                out_dir = reserve_output(src, out_root, args)
                entry["result"] = str(out_dir)
                progress.file(src, "preparing", 0)
                job = prepare_file(src, out_dir, args)
                for task in job["pending"]:
                    task["source"] = str(src)
                job["remaining"] = {task["cache"] for task in job["pending"]}
                prepared.append((entry, job))
                entry["status"] = "prepared"
                progress.file(src, "queued", **job_progress(job))
            except Exception as exc:
                entry.update(status="failed", error=str(exc))
                progress.file(src, "failed")
                if out_dir:
                    record_failure(out_dir, exc)
                if lock is not None and lock in locks:
                    locks.remove(lock)
                    release_job_lock(lock)
                log.exception("准备失败：%s（%s）", src, exc)
            save_batch()
        enroll = enrollment_task(args, people) if prepared else None
        tasks = ([enroll] if enroll else []) + [task for _, job in prepared for task in job["pending"]]
        started, failure = time.time(), None
        by_source = {str(job["src"]): (entry, job) for entry, job in prepared}
        finalized = set()

        def finish_file(entry, job):
            key = str(job["src"])
            if key in finalized:
                return
            metrics = job_metrics(job, started)
            batch["metrics"].extend({**metric, "source": key, "file_index": entry["index"]}
                                    for metric in metrics)
            try:
                if enroll and people is None:
                    raise RuntimeError(f"声纹登记未完成：{failure or '没有返回登记结果'}")
                progress.file(key, "exporting", **job_progress(job))
                finalize_file(job, args, people, metrics, time.time() - started)
                entry["status"] = "completed"
                progress.file(key, "completed", 1, completed_chunks=len(job["chunks_plan"]),
                              total_chunks=len(job["chunks_plan"]))
            except Exception as exc:
                entry.update(status="failed", error=str(exc))
                progress.file(key, "failed")
                record_failure(job["out_dir"], exc, job)
                log.exception("处理失败：%s（%s），健康分段已保留", job["src"], exc)
            finalized.add(key)
            save_batch()

        def task_started(task):
            if task["kind"] == "chunk":
                entry, job = by_source[task["source"]]
                entry["status"] = "running"
                progress.file(job["src"], "running", **job_progress(job))

        def task_completed(task, result):
            nonlocal people
            if task["kind"] == "enroll":
                people = result["people"]
                for entry, job in prepared:
                    if not job["remaining"]:
                        finish_file(entry, job)
            else:
                entry, job = by_source[task["source"]]
                job["remaining"].discard(task["cache"])
                progress.file(job["src"], "running", **job_progress(job))
                if not job["remaining"] and (not enroll or people is not None):
                    finish_file(entry, job)

        # 完整缓存直接导出；不必等待其他录音的识别进程。
        if not enroll:
            for entry, job in prepared:
                if not job["remaining"]:
                    finish_file(entry, job)
        try:
            people = execute_tasks(tasks, engine, args, people, task_started, task_completed)
        except Exception as exc:
            failure = exc
            if getattr(exc, "partial_people", None) is not None:
                people = exc.partial_people
            log.exception("批次转写中断；检查并导出已完整的录音，其他健康分段保留")
        # 故障前缓存已原子写入、尚未来得及通知的录音，也要检查并保留完整输出。
        for entry, job in prepared:
            finish_file(entry, job)
        failed = sum(entry["status"] == "failed" for entry in batch["files"])
        batch["status"] = "failed" if failed else "completed"
        # 各录音完成或复用已有结果后统一导出；不改变识别缓存和输入队列顺序。
        # 部分失败只能生成注明缺失的部分结果，不能把旧总稿当成本批成功。
        try:
            if not failed:
                progress.emit("status", message="正在合并…", progress=.99,
                              completed_chunks=sum(progress.chunks.values()))
            merged = export_batch_markdown(batch, out_root)
            batch["merged_markdown"] = merged
            if merged["status"] == "completed":
                log.info("合并全文：%s", merged["path"])
                log.info("最新总稿：%s", merged["latest_path"])
            elif merged["status"] == "partial":
                failed = max(1, failed)
                batch["status"] = "failed"
                log.warning("部分结果：%s", merged["path"])
        except Exception as exc:
            failed = max(1, failed)
            batch["status"] = "failed"
            batch["merged_markdown"] = {"status": "failed", "error": str(exc)}
            log.exception("合并全文生成失败，单个录音结果已保留；重新选择本批录音可重试")
        return failed
    except BaseException as exc:
        batch["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "failed"
        for entry in batch["files"]:
            if entry["status"] in ("prepared", "pending", "running"):
                entry["status"] = batch["status"]
                entry["error"] = "任务中断，已完成分段保留，可重新运行"
        for entry, job in prepared:
            if entry["status"] in ("interrupted", "failed"):
                record_failure(job["out_dir"], entry["error"], job)
        raise
    finally:
        batch["worker_pids"] = sorted({m["worker_pid"] for m in batch["metrics"]})
        batch["finished_at"] = time.time()
        batch["wall_seconds"] = batch["finished_at"] - batch["started_at"]
        try:
            save_batch()
        finally:
            for lock in reversed(locks):
                release_job_lock(lock)


def main():
    ap = argparse.ArgumentParser(description="长录音转写 + 说话人区分（FunASR）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("run", "watch"):
        sp = sub.add_parser(c, help={"run": "转写指定文件/文件夹", "watch": "盯住一个文件夹，有新录音自动转写"}[c])
        sp.add_argument("inputs", nargs="+", help="音频文件或文件夹")
        sp.add_argument("-o", "--out", default="转写结果", help="输出目录（默认 ./转写结果）")
        sp.add_argument("--voiceprints", default=str(Path(__file__).parent / "voiceprints"), help="声纹库目录")
        sp.add_argument("--hotwords", default=str(Path(__file__).parent / "hotwords.txt"),
                        help="热词文件（每行一个词，如公司名、产品名、同事名）")
        sp.add_argument("--chunk-min", type=float, default=15, help="每段处理约多少分钟（默认 15，切点避开讲话）")
        sp.add_argument("--workers", type=int, choices=range(1, 5), default=4, help="最多几个转写进程（1–4，默认 4）")
        sp.add_argument("--engine", choices=("original", "light"), default="light",
                        help="light 轻量版（默认），结果写入轻量试用子目录；original 原版")
        sp.add_argument("--part-min", type=float, default=30, help="Markdown 每个文件多少分钟（默认 30）")
        sp.add_argument("--merge-thr", type=float, default=0.62, help="跨段合并同一说话人的相似度门槛（0-1，默认 0.62）")
        sp.add_argument("--enroll-thr", type=float, default=0.5, help="声纹认人门槛（0-1，默认 0.5）")
        sp.add_argument("--max-spk", type=int, default=10, help="每段最多几个说话人")
        sp.add_argument("--device", default="auto", help="cpu / cuda / mps（默认自动）")
        sp.add_argument("--threads", type=int, default=min(10, max(1, os.cpu_count() or 4)),
                        help="CPU 总线程预算，按实际转写进程数平分（默认本机逻辑核心数，上限 10）")
        sp.add_argument("--asr-model", default="auto", help="识别模型：auto / paraformer-zh / 本地模型目录")
        sp.add_argument("--model-dir", default=os.environ.get("TX_MODEL_DIR") or None,
                        help="本地模型总目录（含 asr/vad/punc/spk，Mac 安装包自动设置）")
        sp.add_argument("--no-norm", action="store_true", help="不做音量均衡（默认会做）")
        sp.add_argument("--keep-work", action="store_true", help="保留中间 16k wav")
        sp.add_argument("--force", action="store_true", help="已转写过的也重新出结果")
        sp.add_argument("--batch-id-file", help=argparse.SUPPRESS)
        sp.add_argument("--progress-events", action="store_true", help=argparse.SUPPRESS)
        if c == "watch":
            sp.add_argument("--interval", type=int, default=60, help="多少秒检查一次（默认 60）")
    args = ap.parse_args()
    if args.engine == "light":
        if args.asr_model != "auto":
            ap.error("轻量版使用已校验的 ONNX 模型包，不支持 --asr-model 覆盖")
        if not args.model_dir:
            args.model_dir = str(Path.home() / "Library/Application Support/录音转写工具/models-light")
        if args.device not in ("auto", "cpu"):
            ap.error("轻量版当前使用 CPUExecutionProvider，请使用 --device cpu 或 auto")
    if (not all(math.isfinite(v) and v > 0 for v in (args.chunk_min, args.part_min, args.threads))
            or not all(math.isfinite(v) and 0 <= v <= 1 for v in (args.merge_thr, args.enroll_thr))
            or args.max_spk <= 0):
        ap.error("处理分段时长、导出分段时长及 CPU 线程预算必须大于 0")
    if args.cmd == "watch" and args.interval <= 0:
        ap.error("检查间隔必须大于 0")
    logging.basicConfig(level=logging.ERROR, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    log.setLevel(logging.INFO)
    os.environ.setdefault("TQDM_DISABLE", "1")
    hw = Path(args.hotwords)
    args.hotword_text = " ".join(w.strip() for w in hw.read_text("utf-8").splitlines()
                                 if w.strip() and not w.startswith("#")) if hw.exists() else ""
    # 先检查缓存；并行时只在子进程加载模型，避免父进程多占一整套模型。
    engine, people = None, None
    out_root = Path(args.out).expanduser()
    out_root.mkdir(parents=True, exist_ok=True)

    if args.cmd == "run":
        files = find_audio(args.inputs, include_missing=True)
        if not files:
            log.error("没有找到音频文件")
            sys.exit(1)
        failed = run_files(engine, files, out_root, args, people)
        if failed:
            log.error("批次未全部完成（失败项 %d），详情见上面的日志和批次索引", failed)
            sys.exit(2)
    else:
        log.info("开始盯文件夹：%s，每 %d 秒检查一次，按 Ctrl+C 退出", args.inputs, args.interval)
        sizes = {}
        while True:
            ready = []
            for f in find_audio(args.inputs):
                try:
                    if is_completed(f, out_root, args):
                        continue
                    identity = source_identity(f)
                    if sizes.get(f) != identity:  # 大小及修改时间稳定后再处理
                        sizes[f] = identity
                        continue
                    ready.append(f)
                except Exception as e:
                    log.exception("处理失败：%s（%s）", f.name, e)
            if ready:
                run_files(engine, ready, out_root, args, people)
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("已停止，已完成的分段会保留，下次可继续")
        sys.exit(130)
