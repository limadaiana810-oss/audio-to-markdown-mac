# -*- coding: utf-8 -*-
"""安装后自检：依赖能导入、ffmpeg 可用（含音量均衡滤镜）、4 个本地模型能加载，并完整跑一遍转写流程。"""
import argparse, csv, hashlib, importlib.metadata, json, os, platform, shutil, subprocess, sys, tempfile, time
from pathlib import Path


def fail(msg):
    print(f"✗ {msg}")
    sys.exit(1)


def file_sha256(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def verify_light_models(model_dir, trusted_manifest):
    """校验本机导出产物及其可信来源；不把 .ok 文件当作模型完整的证明。"""
    model_dir = Path(model_dir).resolve()
    local_manifest = model_dir / "models-light.manifest.json"
    try:
        actual = json.loads(local_manifest.read_text("utf-8"))
        trusted = json.loads(Path(trusted_manifest).read_text("utf-8"))
        if actual.get("schema") != 1 or trusted.get("schema") != 1:
            raise ValueError("不支持的轻量模型清单格式")
        if not trusted.get("pipeline_version") or actual.get("pipeline_version") != trusted["pipeline_version"]:
            raise ValueError("轻量模型流程版本与安装包不一致")
        exporter = Path(__file__).with_name("export_light_models.py")
        exporter_sha = file_sha256(exporter)
        if actual.get("exporter_sha256") != exporter_sha or trusted.get("exporter_sha256") != exporter_sha:
            raise ValueError("模型导出程序已变化，需要重新导出并更新可信清单")
        expected_deps = trusted.get("dependencies")
        if not isinstance(expected_deps, dict) or not expected_deps or actual.get("dependencies") != expected_deps:
            raise ValueError("模型导出依赖指纹与安装包不一致")
        for name, version in expected_deps.items():
            if importlib.metadata.version(name) != version:
                raise ValueError(f"轻量依赖版本不一致：{name}，需要 {version}")

        source_manifest = Path(__file__).with_name("models.tsv")
        source_manifest_sha = file_sha256(source_manifest)
        if (actual.get("source_manifest_sha256") != source_manifest_sha
                or trusted.get("source_manifest_sha256") != source_manifest_sha):
            raise ValueError("源模型清单指纹与安装包不一致，需要重新导出")
        allowed_sources = {}
        with source_manifest.open(encoding="utf-8") as stream:
            for row in csv.reader(stream, delimiter="\t"):
                if len(row) == 6 and row[0] in ("modelscope", "huggingface") and row[1] in ("asr", "vad", "spk"):
                    allowed_sources.setdefault((row[1], row[2]), set()).add((int(row[3]), row[4]))

        def source_fingerprint(data):
            sources = data.get("sources")
            if not isinstance(sources, list) or not sources:
                raise ValueError("缺少可信源权重指纹")
            records = {}
            for source in sources:
                key = source["component"], Path(source["path"]).name
                if key in records:
                    raise ValueError("源权重清单包含重复文件")
                records[key] = source["size"], source["sha256"]
            return records

        actual_sources, trusted_sources = source_fingerprint(actual), source_fingerprint(trusted)
        if actual_sources.keys() != trusted_sources.keys():
            raise ValueError("轻量模型的源权重文件列表与安装包不一致")
        for key, fingerprint in actual_sources.items():
            if key[0] in ("asr", "vad", "spk"):
                allowed = allowed_sources.get(key, set())
                if fingerprint not in allowed or trusted_sources[key] not in allowed:
                    raise ValueError(f"源权重不在固定校验清单中：{key[0]}/{key[1]}")
            elif key[0] != "punc" or fingerprint != trusted_sources[key]:
                raise ValueError(f"轻量模型的源权重指纹与安装包不一致：{key[0]}/{key[1]}")
        files = actual.get("files")
        if not isinstance(files, list) or not files:
            raise ValueError("轻量模型产物清单为空")
        paths = [item["path"] for item in files]
        if len(set(paths)) != len(paths) or set(paths) != {item["path"] for item in trusted["files"]}:
            raise ValueError("轻量模型产物列表与安装包不一致")
        for item in files:
            relative = Path(item["path"])
            path = (model_dir / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or not path.is_relative_to(model_dir):
                raise ValueError(f"无效模型路径：{relative}")
            if not path.is_file() or path.stat().st_size != item["size"] or file_sha256(path) != item["sha256"]:
                raise ValueError(f"轻量模型缺失或校验失败：{relative}")
    except (OSError, ValueError, KeyError, TypeError, importlib.metadata.PackageNotFoundError) as exc:
        fail(f"轻量模型校验未通过：{exc}。请重新运行安装脚本 --engine light。")
    fingerprint = hashlib.sha256(json.dumps(actual, sort_keys=True).encode()).hexdigest()
    print(f"· 轻量模型来源、依赖和 {len(files)} 个文件校验通过，指纹 {fingerprint[:16]}")
    return fingerprint


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--engine", choices=("original", "light"), default="original")
    ap.add_argument("--model-dir", required=True)
    ap.add_argument("--tx")
    ap.add_argument("--manifest", default=str(Path(__file__).with_name("models-light.manifest.json")))
    ap.add_argument("--verify-models-only", action="store_true")
    a = ap.parse_args()
    if a.engine == "light":
        verify_light_models(a.model_dir, a.manifest)
    if a.verify_models_only:
        if a.engine != "light":
            ap.error("--verify-models-only 当前用于轻量模型清单校验")
        return
    if not a.tx:
        ap.error("完整自检需要 --tx 程序路径")
    # 和日常启动保持一致：自检只使用已下载的本地模型。
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"

    print(f"· Python {platform.python_version()}，{platform.system()} {platform.machine()}")
    if platform.system() == "Darwin" and platform.machine() != "arm64":
        fail("Python 不是 arm64 版本")
    try:
        import numpy, scipy, soundfile, imageio_ffmpeg  # noqa
        if a.engine == "original":
            import sklearn, torch, funasr  # noqa
            print(f"· torch {torch.__version__}，funasr {funasr.__version__}，numpy {numpy.__version__}")
        else:
            import onnxruntime
            print(f"· 轻量试用：onnxruntime {onnxruntime.__version__}，numpy {numpy.__version__}")
    except Exception as e:
        fail(f"依赖导入失败：{e!r}")

    ff = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run([ff, "-hide_banner", "-filters"], capture_output=True, text=True)
    if r.returncode != 0 or "dynaudnorm" not in r.stdout:
        fail(f"ffmpeg 不可用或缺少 dynaudnorm 滤镜：{ff}")
    print(f"· ffmpeg 可用：{ff}")

    if a.engine == "original":
        for m in ("asr", "vad", "punc", "spk"):
            if not (Path(a.model_dir) / m / "config.yaml").exists():
                fail(f"模型 {m} 不完整")

    tmp = Path(tempfile.mkdtemp(prefix="tx自检 "))
    try:
        src = tmp / "自检 录音.m4a"
        made = False
        # Mac 自带 say：如装有中文语音就合成一句真实语音（只用于自检，不影响结果）
        if shutil.which("say"):
            aiff = tmp / "say.aiff"
            for v in ("Tingting", "Ting-Ting", "Meijia"):
                if subprocess.run(["say", "-v", v, "-o", str(aiff), "你好，这是一段安装自检的测试录音。"],
                                  capture_output=True).returncode == 0 and aiff.exists():
                    made = subprocess.run([ff, "-y", "-loglevel", "error", "-i", str(aiff), "-c:a", "aac", str(src)]).returncode == 0
                    break
        if not made:
            # 没有中文语音：用 6 秒测试音（识别结果为空是正常的，只验证整条流程能跑通）
            r = subprocess.run([ff, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=300:duration=6",
                                "-c:a", "aac", str(src)])
            if r.returncode != 0:
                fail("ffmpeg 生成测试音频失败")
        vp = tmp / "声纹"; vp.mkdir()
        out = tmp / "结果"
        t = time.time()
        r = subprocess.run([sys.executable, a.tx, "run", str(src), "-o", str(out), "--model-dir", a.model_dir,
                            "--engine", a.engine, "--voiceprints", str(vp), "--hotwords", str(tmp / "无热词.txt")])
        if r.returncode != 0:
            fail(f"试转写失败（退出码 {r.returncode}）")
        d = out / "轻量试用" / src.stem if a.engine == "light" else out / src.stem
        need = [d / "目录.md", d / f"{src.stem}.json", d / f"{src.stem}.srt", d / f"{src.stem}_全文.md"]
        miss = [str(p.name) for p in need if not p.exists()]
        if miss or not list((d / "分段").glob("*.md")):
            fail(f"输出文件不全：{miss}")
        sents = json.loads((d / f"{src.stem}.json").read_text("utf-8"))["句子"]
        text = "".join(s["text"] for s in sents)
        if made and not text.strip():
            fail("中文测试语音没有识别出文字；输出文件存在，但语音识别未通过")
        if any(s["start"] < 0 or s["end"] < s["start"] or not s.get("speaker") for s in sents):
            fail("试转写的时间戳或说话人字段不正确")
        print(f"· 试转写成功，用时 {time.time() - t:.0f} 秒；识别内容：{text or '（测试音，无文字，正常）'}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("✓ 自检通过")


if __name__ == "__main__":
    main()
