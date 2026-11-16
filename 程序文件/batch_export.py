"""Export chronological, source-preserving batch Markdown without loading ASR models."""
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import quote


_FILENAME_TIME = re.compile(
    r"(?<!\d)(\d{4})[-_.]?(\d{2})[-_.]?(\d{2})[T _-]?"
    r"(\d{2})[:_.-]?(\d{2})[:_.-]?(\d{2})(?!\d)"
)


def recording_time_from_name(source):
    """Return an unambiguous filename time; never infer recording time from mtime."""
    found = set()
    for match in _FILENAME_TIME.finditer(Path(source).stem):
        try:
            found.add(datetime(*(int(part) for part in match.groups())))
        except ValueError:
            continue
    return next(iter(found)) if len(found) == 1 else None


def _escape(text):
    text = str(text).replace("\r", "\\r").replace("\n", "\\n")
    return re.sub(r"([\\`*_{}\[\]()#+.!|<>])", r"\\\1", text)


def _link(label, path):
    return f"[{_escape(label)}]({quote(str(Path(path).resolve()), safe='/')})"


def _identity(source):
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError("来源不是录音文件")
    stat = source.stat()
    return {"path": str(source), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns,
            "ctime_ns": stat.st_ctime_ns, "device": stat.st_dev, "inode": stat.st_ino}


def source_snapshot(source):
    """Capture content before decoding; retry a brief concurrent metadata update."""
    source = Path(source)
    for _ in range(3):
        before = _identity(source)
        digest = hashlib.sha256()
        with source.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        after = _identity(source)
        if before == after:
            return after, digest.hexdigest()
    raise ValueError("录音仍在变化，请等待文件写入完成后重试")


def verified_source_identity(source, expected, content_digest=None):
    """Allow only metadata-only ctime changes proven against a prior content SHA.

    Size, mtime, inode, device and path remain strict. Old markers without a
    content snapshot retain their original conservative identity checks.
    """
    current = _identity(Path(source))
    if current == expected:
        return current
    def payload_identity(value):
        return {key: item for key, item in value.items() if key != "ctime_ns"}
    if (not isinstance(expected, dict) or payload_identity(current) != payload_identity(expected)
            or not isinstance(content_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", content_digest)):
        raise ValueError("原录音发生变化，请重新运行")
    checked, actual_digest = source_snapshot(source)
    if payload_identity(checked) != payload_identity(expected) or actual_digest != content_digest:
        raise ValueError("原录音内容发生变化，请重新运行")
    return checked


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _load_entry(entry):
    """Use the same verified bytes for hashing and export, including cached/skipped files."""
    if entry.get("status") not in ("completed", "skipped"):
        raise ValueError(entry.get("error") or f"文件尚未完成（{entry.get('status', '未知状态')}）")
    source = Path(entry["source"]).resolve()
    before = _identity(source)
    if not entry.get("result"):
        raise ValueError("缺少结果目录")
    result = Path(entry["result"]).resolve()
    marker = json.loads((result / ".work" / "completed.json").read_bytes())
    if not isinstance(marker, dict):
        raise ValueError("完成标记与原录音不匹配")
    source_digest = marker.get("source_sha256")
    before = verified_source_identity(source, marker.get("source"), source_digest)
    hashes = marker.get("output_sha256")
    listed = marker.get("outputs")
    if not isinstance(hashes, dict) or not isinstance(listed, list):
        raise ValueError("完成标记缺少输出校验信息")
    names = (f"{source.stem}_全文.md", f"{source.stem}.json", "待复核.md", "待复核.json")
    payloads = {}
    for name in names:
        content = (result / name).read_bytes()
        if (name not in listed or not content
                or hashes.get(name) != hashlib.sha256(content).hexdigest()):
            raise ValueError(f"输出文件校验失败：{name}")
        payloads[name] = content
    data = json.loads(payloads[names[1]])
    review = json.loads(payloads["待复核.json"])
    if (not isinstance(data, dict) or not isinstance(data.get("文件"), str)
            or Path(data["文件"]).resolve() != source):
        raise ValueError("转写结果的录音来源不匹配")
    total, sentences, issues = data.get("时长秒"), data.get("句子"), data.get("待复核")
    if (not _finite(total) or total <= 0 or not isinstance(sentences, list)
            or not isinstance(issues, list) or not isinstance(review, dict)
            or review.get("items") != issues):
        raise ValueError("转写结果或待复核记录结构不完整")
    previous = -1
    for sentence in sentences:
        if (not isinstance(sentence, dict) or not isinstance(sentence.get("text"), str)
                or not isinstance(sentence.get("speaker"), str)
                or not all(_finite(sentence.get(key)) for key in ("start", "end"))
                or not 0 <= sentence["start"] < sentence["end"] <= total + .051
                or sentence["start"] < previous):
            raise ValueError("转写句子或时间顺序无效")
        previous = sentence["start"]
    markdown = payloads[names[0]].decode("utf-8")
    title, separator, body = markdown.partition("\n")
    if title != f"# {source.stem} 全文" or not separator:
        raise ValueError("独立全文的标题格式无效")
    if sentences and not body.strip():
        raise ValueError("有转写句子，但独立全文没有正文")
    if not sentences and body.strip():
        raise ValueError("独立全文与空句子列表不一致")
    before = verified_source_identity(source, before, source_digest)
    return {"source": source, "identity": before, "source_sha256": source_digest, "result": result, "body": body, "duration": total,
            "sentence_count": len(sentences), "review_count": len(issues), "review": issues,
            "full_path": result / names[0], "review_path": result / "待复核.md"}


def _duration(seconds):
    tenths = round(seconds * 10)
    hours, rest = divmod(tenths, 36000)
    minutes, rest = divmod(rest, 600)
    whole, fraction = divmod(rest, 10)
    suffix = f".{fraction}" if fraction else ""
    return f"{hours:02d}:{minutes:02d}:{whole:02d}{suffix}"


def _clock(value, with_date=False):
    text = value.strftime("%Y-%m-%d %H:%M:%S" if with_date else "%H:%M:%S")
    return text + (f".{value.microsecond:06d}".rstrip("0") if value.microsecond else "")


def _section_title(when, duration):
    if when is None:
        return f"录音时间未知｜00:00:00–{_duration(duration)}" if duration is not None else "录音时间未知"
    if duration is None:
        return _clock(when, with_date=True)
    end = when + timedelta(seconds=duration)
    return f"{_clock(when, with_date=True)}–{_clock(end, with_date=end.date() != when.date())}"


def _plain_body(body):
    # Only change the generated timestamp/speaker label, never the spoken text.
    return re.sub(r"^\*\*(\[\d{2,}:\d{2}:\d{2}\] [^\r\n]*?)\*\*：", r"\1：", body,
                  flags=re.MULTILINE)


def _unplaced_text(issues):
    """These two review forms are produced only for text excluded from the body."""
    return [item["text"] for item in issues if isinstance(item, dict)
            and isinstance(item.get("text"), str) and item["text"].strip()
            and (item.get("code") == "invalid_timestamp"
                 or ("raw_start" in item and "raw_end" in item))]


def _write_atomic(path, text):
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def export_batch_markdown(batch, out_root):
    """Write an archived batch transcript and, only when complete, the latest full copy.

    Each file retains its own relative timestamps and speaker labels. Files with no
    reliable filename time follow dated files in input order. A failed/invalid entry
    remains visible in a partial document and never replaces the last full document.
    Filesystem write errors propagate to the caller; ASR/cache state is never changed.
    """
    entries = batch.get("files", [])
    if not isinstance(entries, list):
        raise ValueError("批次文件列表无效")
    result = {"status": "not_applicable", "path": None, "file_count": len(entries),
              "included_count": 0, "missing": [], "ordered_sources": [], "sha256": None}
    if not entries:
        return result
    if not isinstance(batch.get("index_path"), str) or not batch["index_path"]:
        raise ValueError("缺少批次历史索引路径")
    ordered = []
    for position, entry in enumerate(entries):
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), str):
            raise ValueError("批次文件来源无效")
        when = recording_time_from_name(entry["source"])
        ordered.append((when, position, entry))
    ordered.sort(key=lambda item: (item[0] is None, item[0] or datetime.max, item[1]))
    rows, missing = [], []
    for when, position, entry in ordered:
        source = str(Path(entry["source"]).resolve())
        row = {"when": when, "position": position, "source": source}
        try:
            row["data"] = _load_entry(entry)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            issue = {"source": source, "status": entry.get("status"), "reason": str(exc)}
            missing.append(issue)
            row["error"] = str(exc)
        rows.append(row)
    # Earlier files may change while later files are being loaded; check the whole
    # snapshot again before publishing, including entries skipped by the ASR loop.
    for row in rows:
        if "data" not in row:
            continue
        try:
            data = row["data"]
            data["identity"] = verified_source_identity(data["source"], data["identity"],
                                                         data.get("source_sha256"))
        except (OSError, ValueError) as exc:
            row.pop("data")
            row["error"] = str(exc)
            missing.append({"source": row["source"], "status": "changed", "reason": str(exc)})
    complete = not missing
    root = Path(out_root).resolve()
    archive = root / "合并全文"
    archive.mkdir(parents=True, exist_ok=True)
    batch_id = re.sub(r"[^\w-]", "_", Path(batch["index_path"]).stem)
    engine = re.sub(r"[^\w-]", "_", str(batch.get("engine", "unknown")))
    suffix = "合并全文" if complete else "部分结果"
    path = archive / f"{batch_id}-{engine}_{suffix}.md"
    latest = root / "合并全文.md"
    title = "# 录音转写" if complete else "# 录音转写（部分结果）"
    parts = [title, "", "录音日期时间来自文件名；时间戳相对于各自录音开头，说话人编号按录音独立。", ""]
    if missing:
        parts += [f"本批有 {len(missing)} 份录音未完成，下文标明缺失位置；上一份完整总稿未更新。", ""]
    for row in rows:
        source = row["source"]
        duration = row["data"]["duration"] if "data" in row else None
        parts += [f"## {_section_title(row['when'], duration)}", "",
                  f"来源：{_link(Path(source).name, source)}", ""]
        if "error" in row:
            parts += ["（本份录音未完成，未纳入正文。）", ""]
            continue
        data = row["data"]
        if not data["sentence_count"]:
            parts += ["（没有识别出的正文）", ""]
        parts.append(_plain_body(data["body"]))
        unplaced = _unplaced_text(data["review"])
        if unplaced:
            parts += ["", "### 待确认内容", ""]
            for original in unplaced:
                parts += [f"[时间待确认] 说话人未确定：{original}", ""]
        parts.append("")
    text = "\n".join(parts)
    _write_atomic(path, text)
    result.update(status="completed" if complete else "partial", path=str(path),
                  included_count=len(entries) - len(missing), missing=missing,
                  ordered_sources=[row["source"] for row in rows],
                  sha256=hashlib.sha256(text.encode("utf-8")).hexdigest())
    if complete:
        _write_atomic(latest, text)
        result["latest_path"] = str(latest)
    return result
