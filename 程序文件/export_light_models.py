#!/usr/bin/env python3
"""Build verified local ONNX assets without modifying the original models or venv."""
import argparse
import csv
import gc
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import time
import types
from urllib.request import Request, urlopen

PIPELINE_VERSION = "light-cif-v2"
PUNC_REPO = "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"
PUNC_REVISION = "d488ddd5985989501d57bce7f7a4d4dcfca779d0"
# ModelScope's repository-files API supplied these hashes before downloading weights.
PUNC_FILES = {
    "config.yaml": (810, "a56ec10925b06fa976ad51af373396be2b13e1eb8dc62a5426b5adebaba7071d"),
    "configuration.json": (373, "ebca5ff883e03585b2eec84ce80573f851658bdc61f2887db7d076afe6facd91"),
    "tokens.json": (4207480, "c960ab87bccea4aa15cf49a59f71973c2c330b46668048cd8da253749ec71ee3"),
    "model.pt": (291979892, "a5818bb9d933805a916eebe41eb41648f7f9caad30b4bd59d56f3ca135421916"),
}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(data)
    return digest.hexdigest()


def json_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    temp.replace(path)


def verify_original(source, trusted_tsv):
    expected = {}
    with open(trusted_tsv, encoding="utf-8") as stream:
        for row in csv.reader(stream, delimiter="\t"):
            if len(row) == 6 and row[0] in ("modelscope", "huggingface") and row[1] in ("asr", "vad", "spk"):
                expected.setdefault((row[1], row[2]), set()).add((int(row[3]), row[4]))
    records = []
    for (component, name), allowed in expected.items():
        path = source / component / name
        if not path.is_file():
            raise RuntimeError(f"Missing trusted source: {path}")
        size, digest = path.stat().st_size, sha256(path)
        if (size, digest) not in allowed:
            raise RuntimeError(f"Source SHA256 mismatch: {path}")
        records.append({"component": component, "path": str(path), "size": size, "sha256": digest})
    if not {"asr", "vad", "spk"}.issubset({r["component"] for r in records}):
        raise RuntimeError("Trusted manifest does not contain all required source components")
    return records


def download_small_punc(folder):
    folder.mkdir(parents=True, exist_ok=True)
    records = []
    for name, (size, digest) in PUNC_FILES.items():
        path = folder / name
        url = f"https://www.modelscope.cn/models/{PUNC_REPO}/resolve/{PUNC_REVISION}/{name}"
        if not path.is_file() or path.stat().st_size != size or sha256(path) != digest:
            temp = path.with_suffix(path.suffix + ".download")
            print(f"Downloading verified small punctuation source: {name} ({size} bytes)", flush=True)
            with urlopen(Request(url, headers={"User-Agent": "DeliLocalTranscription/1"}), timeout=90) as response, open(temp, "wb") as output:
                shutil.copyfileobj(response, output, length=1024 * 1024)
            if temp.stat().st_size != size or sha256(temp) != digest:
                temp.unlink(missing_ok=True)
                raise RuntimeError(f"Downloaded source SHA256 mismatch: {name}")
            temp.replace(path)
        records.append({"component": "punc", "path": str(path), "size": size,
                        "sha256": digest, "url": url, "revision": PUNC_REVISION})
    json_atomic(folder / "source-manifest.json", {"repository": PUNC_REPO, "revision": PUNC_REVISION, "files": records})
    return records


def stable_cif(hidden, alphas, threshold=1.0):
    """Continuous CIF integration, without float32 floor/scatter discontinuities.

    Each frame contributes its alpha interval's overlap with each token's unit
    interval. This is the original CIF integral, not a timestamp approximation.
    Keeping the cumulative mass in float64 prevents boundary rounding from
    duplicating/skipping an acoustic embedding in long utterances.
    """
    import torch
    mass = alphas.to(torch.float64) / threshold
    end = torch.cumsum(mass, dim=1)
    start = end - mass
    count = torch.floor(end[:, -1]).to(torch.int64)
    bins = torch.arange(count.max(), dtype=torch.float64, device=hidden.device)[None, :, None]
    weights = torch.clamp(torch.minimum(end[:, None, :], bins + 1.0)
                          - torch.maximum(start[:, None, :], bins), min=0.0)
    embeddings = torch.matmul(weights.to(hidden.dtype), hidden)
    valid = torch.arange(count.max(), device=hidden.device)[None, :] < count[:, None]
    embeddings = embeddings * valid[:, :, None].to(hidden.dtype)
    peaks = ((torch.floor(end) > torch.floor(start)).to(end.dtype)
             + end - torch.floor(end)).to(alphas.dtype)
    return embeddings, peaks


def stable_predictor_forward(self, hidden, mask):
    import torch
    alphas, _ = self.forward_cnn(hidden, mask)
    mask = mask.transpose(-1, -2).float().squeeze(-1)
    hidden, alphas, token_num = self.tail_process_fn(hidden, alphas, mask=mask)
    embeddings, peaks = stable_cif(hidden, alphas, self.threshold)
    token_num = torch.floor(alphas.to(torch.float64).sum(dim=1)).to(alphas.dtype)
    return embeddings, token_num, alphas, peaks


def timestamp_forward(self, speech, speech_lengths):
    import torch
    encoder, encoder_lengths = self.encoder(speech, speech_lengths)
    mask = self.make_pad_mask(encoder_lengths)[:, None, :]
    embeddings, lengths, alphas, peaks = self.predictor(encoder, mask)
    lengths = lengths.floor().to(torch.int32)
    logits, _ = self.decoder(encoder, encoder_lengths, embeddings, lengths)
    return torch.log_softmax(logits, dim=-1), lengths, alphas, peaks, encoder_lengths


def export_component(component, source, output, keep_fp32=False):
    import torch
    from funasr import AutoModel
    output.mkdir(parents=True, exist_ok=True)
    print(f"Exporting {component} FP32", flush=True)
    auto = AutoModel(model=str(source), device="cpu", ncpu=2, disable_update=True, disable_pbar=True)
    export_config = {k: v for k, v in auto.kwargs.items() if k != "model"}
    model = auto.model.export(**{**export_config, "device": "cpu", "type": "onnx", "max_seq_len": 20000})
    if component == "asr":
        model.predictor.forward = types.MethodType(stable_predictor_forward, model.predictor)
        model.forward = types.MethodType(timestamp_forward, model)
        output_names = ["logits", "token_num", "alphas", "peaks", "encoder_lengths"]
        dynamic = model.export_dynamic_axes()
        dynamic.update({"alphas": {0: "batch_size", 1: "predictor_length"},
                        "peaks": {0: "batch_size", 1: "predictor_length"},
                        "encoder_lengths": {0: "batch_size"}})
    else:
        output_names, dynamic = model.export_output_names(), model.export_dynamic_axes()
    model.eval()
    destination = output / "model.onnx"
    temp = output / "model.build.onnx"
    with torch.no_grad():
        torch.onnx.export(model, model.export_dummy_inputs(), str(temp),
                          input_names=model.export_input_names(), output_names=output_names,
                          dynamic_axes=dynamic, opset_version=14, do_constant_folding=True,
                          dynamo=False)
    temp.replace(destination)
    del model, auto
    gc.collect()
    import onnx
    from onnxruntime.quantization import QuantType, quantize_dynamic
    graph = onnx.load(str(destination))
    excluded = [n.name for n in graph.graph.node if any(s in n.name for s in ("output", "predictor", "bias_encoder", "bias_decoder"))]
    onnx.checker.check_model(graph)
    del graph
    gc.collect()
    print(f"Quantizing {component} MatMul weights", flush=True)
    quant_temp = output / "model_quant.build.onnx"
    quantize_dynamic(str(destination), str(quant_temp), op_types_to_quantize=["MatMul"],
                     per_channel=True, reduce_range=False, weight_type=QuantType.QUInt8,
                     nodes_to_exclude=excluded)
    quant_temp.replace(output / "model_quant.onnx")
    if not keep_fp32 and component != "vad":
        destination.unlink()
    for name in ("config.yaml", "configuration.json", "tokens.json", "am.mvn", "seg_dict", "jieba_usr_dict"):
        if (source / name).is_file():
            shutil.copy2(source / name, output / name)
    print(f"Finished {component}", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--source-model-dir", type=Path, required=True)
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--manifest", type=Path, required=True)
    ap.add_argument("--punc-source-dir", type=Path)
    ap.add_argument("--trusted-source-manifest", type=Path, default=Path(__file__).with_name("models.tsv"))
    ap.add_argument("--download-sources-only", action="store_true")
    ap.add_argument("--keep-fp32", action="store_true", help="Keep ASR/punctuation FP32 reference graphs for validation")
    args = ap.parse_args()
    source, output = args.source_model_dir.resolve(), args.output_dir.resolve()
    if source == output or source in output.parents:
        ap.error("output-dir must be separate from the original model directory")
    punc_source = args.punc_source_dir or source.parent / "models-light-source" / "punc-small"
    sources = verify_original(source, args.trusted_source_manifest)
    sources += download_small_punc(punc_source)
    if args.download_sources_only:
        print(json.dumps({"sources": sources}, ensure_ascii=False, indent=2))
        return
    output.mkdir(parents=True, exist_ok=True)
    # An incomplete export must never retain a valid final manifest.
    args.manifest.unlink(missing_ok=True)
    import torch
    torch.set_num_threads(2)
    for component, path in (("asr", source / "asr"), ("vad", source / "vad"), ("punc", punc_source)):
        export_component(component, path, output / component, keep_fp32=args.keep_fp32)
    (output / "spk").mkdir(exist_ok=True)
    for name in ("config.yaml", "configuration.json", "campplus_cn_common.bin"):
        shutil.copy2(source / "spk" / name, output / "spk" / name)
    files = [{"path": str(p.relative_to(output)), "size": p.stat().st_size, "sha256": sha256(p)}
             for part in ("asr", "vad", "punc", "spk") for p in sorted((output / part).iterdir()) if p.is_file()]
    dependencies = {n: importlib.metadata.version(n) for n in (
        "funasr", "funasr-onnx", "torch", "torchaudio", "numpy", "onnx", "onnxruntime",
        "scipy", "scikit-learn", "kaldi-native-fbank", "jieba", "PyYAML")}
    manifest = {"schema": 1, "pipeline_version": PIPELINE_VERSION,
                "exporter_sha256": sha256(Path(__file__)), "dependencies": dependencies,
                "source_manifest_sha256": sha256(args.trusted_source_manifest),
                "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "sources": sources,
                "asr_outputs": ["logits", "token_num", "alphas", "peaks", "encoder_lengths"],
                "timestamp_upsample_rate": 1, "files": files}
    json_atomic(args.manifest, manifest)
    print(f"Verified light model manifest: {args.manifest}", flush=True)


if __name__ == "__main__":
    main()
