#!/usr/bin/env python3
"""Check release identity before tagging or uploading an installer (standard library only)."""
import argparse
import hashlib
import os
from pathlib import Path
import plistlib
import re


def check_release(root, *, tag=None, app=None, dmg=None):
    root = Path(root)
    version = (root / "程序文件/VERSION").read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", version):
        raise ValueError("VERSION must be a stable major.minor.patch version")
    if tag is not None and tag != "v" + version:
        raise ValueError(f"Tag {tag!r} does not match VERSION {version}")
    changelog = (root / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(r"^## \[" + re.escape(version) + r"\](?:\([^\r\n)]+\))?(?:\s|$)", changelog, re.M):
        raise ValueError("CHANGELOG.md needs an entry for " + version)
    asset = f"Audio-to-Markdown-{version}-AppleSilicon.dmg"
    download = f"/releases/download/v{version}/{asset}"
    for filename in ("README.md", "README.en.md"):
        text = (root / filename).read_text(encoding="utf-8")
        if download not in text:
            raise ValueError(f"{filename} needs the current version's direct DMG link")
    if app is not None:
        app = Path(app)
        with (app / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        for key in ("CFBundleShortVersionString", "CFBundleVersion"):
            if info.get(key) != version:
                raise ValueError(f"Application {key} does not match VERSION {version}")
        bundled = (app / "Contents/Resources/程序文件/VERSION").read_text(encoding="utf-8").strip()
        if bundled != version:
            raise ValueError("Bundled source VERSION does not match the application")
    if dmg is not None:
        dmg = Path(dmg)
        if dmg.name != asset:
            raise ValueError("Installer name must be " + asset)
        checksum = Path(str(dmg) + ".sha256").read_text(encoding="utf-8").strip()
        digest = hashlib.sha256()
        with dmg.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if checksum != digest.hexdigest() + "  " + asset:
            raise ValueError("Installer checksum or checksum filename does not match")
    return version


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag", default=os.environ.get("GITHUB_REF_NAME") if os.environ.get("GITHUB_REF_TYPE") == "tag" else None)
    parser.add_argument("--app", type=Path)
    parser.add_argument("--dmg", type=Path)
    args = parser.parse_args()
    try:
        version = check_release(args.root, tag=args.tag, app=args.app, dmg=args.dmg)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"Release check failed: {exc}\n")
    print(f"Release identity verified: v{version}")


if __name__ == "__main__":
    main()
