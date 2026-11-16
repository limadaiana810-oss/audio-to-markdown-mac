# Releases and versioning

[简体中文](RELEASING.md) · **English**

v2.5.1 is the first public open-source release. v2.5.0 and earlier numbers refer to local development before public release; they do not imply that installers with those versions exist on GitHub.

## One version, one traceable set of artifacts

`程序文件/VERSION` is the single source of the application version. `build_app.sh` writes it into both application version fields; `package_release.py` uses it to name the installer. Do not change the cache version constants in `tx.py` just to display a new app version: those constants control transcription cache compatibility.

| Item | Convention |
|---|---|
| Development | `main`, accepting reviewed Pull Requests |
| Stable version | `MAJOR.MINOR.PATCH`, with Git tag `vMAJOR.MINOR.PATCH` |
| Breaking change | Increment major and document migration |
| Compatible feature | Increment minor |
| Fix, documentation, packaging | Increment patch |
| Installer | `Audio-to-Markdown-<version>-AppleSilicon.dmg` |
| Checksum | Matching `.dmg.sha256`, checking both content and filename |
| Source | Code at the Git tag; optionally the build's `Source-<version>.zip` |
| Current stable version | GitHub Releases' Latest marker; README download links name a specific version |

Never replace a published tag, DMG, or checksum with different content. Publish a new patch release to fix a problem, and add an upgrade notice to the affected release. Keep older release pages and downloads. Mark preview releases as Pre-release without replacing the stable Latest release.

This repository has GitHub release immutability enabled. It takes effect when the official Release is published, locking its version tag and assets. An unpublished draft candidate may still have its source, candidate tag, and assets corrected during review; verify their consistency again before publication. After publication, do not replace the tag, DMG, or checksum: ship changes in the next version. See [GitHub’s documentation](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes).

## What CI verifies

[CI](../.github/workflows/ci.yml) runs on `main`, Pull Requests, and `v*` tags:

1. Verify VERSION, CHANGELOG, and both README download links; tag builds also verify the tag matches VERSION.
2. Install hash-locked runtime dependencies on macOS 15 ARM64 / Python 3.12 without downloading recognition models.
3. Run the regression suite for scheduling, cache recovery, chronological merging, backend events, cancellation, native UI state, and release identity.
4. Compile the development application and check signature integrity, app version, and bundled source version.

A passing CI run does not validate a complete DMG against real audio. Maintainers separately verify model inference, offline portability, UI operation, and long-recording performance on release hardware. CI does not automatically download models, package a DMG, or upload installers.

## Release procedure

Build from a clean release commit on an Apple Silicon Mac. First update `VERSION`, [CHANGELOG](../CHANGELOG.md), and the current download links in both READMEs through a Pull Request. Document observable changes, completed validation, and known limits. Any performance claim must identify the hardware, audio type, cache state, and software version.

```bash
# Use the prepared light-model runtime
PYTHON="$HOME/Library/Application Support/录音转写工具/venv-light/bin/python"
VERSION="$(cat 程序文件/VERSION)"

"$PYTHON" 程序文件/check_release.py --tag "v$VERSION"
"$PYTHON" -m unittest discover -s 程序文件/tests -v
bash 程序文件/build_release.sh
```

For a first build, run `bash 程序文件/install.sh --engine light` to prepare the runtime and models, or point `TX_BUILD_APP_DIR` at an existing build environment. The FFmpeg source archive is excluded from Git history. If absent, the packager uses `fetch_ffmpeg_source.py` to download it from the pinned official URL and verify its SHA-256. The DMG still includes the corresponding source archive, build script, and third-party licenses.

Check the freshly built application and installer, not an older installed copy:

```bash
"$PYTHON" 程序文件/check_release.py --tag "v$VERSION" \
  --app ".package-cache/release-stage/录音转写.app" \
  --dmg "发布/Audio-to-Markdown-$VERSION-AppleSilicon.dmg"
```

Mount the DMG and inspect both language READMEs, the source archive, licenses, and application. Test real Chinese audio for single-file and multi-file export, admission of later files in a queue longer than four, stop/resume, chronological order, source attribution, and speaker output. Use `test_portable_release.py` to check transcription and cancellation in a fresh location with network and old installation dependencies denied. This script expects a synthetic sample containing its specified acceptance phrase; do not publish private meeting recordings as test fixtures.

After build and runtime validation, create a tag and draft release. Prepare bilingual `release-notes.md` beforehand and review it before upload. Do not upload raw test reports containing personal paths or real recordings.

```bash
git tag -a "v$VERSION" -m "Release v$VERSION"
git push origin "v$VERSION"
gh release create "v$VERSION" --verify-tag --draft \
  --title "Audio to Markdown for Mac v$VERSION" \
  --notes-file release-notes.md \
  "发布/Audio-to-Markdown-$VERSION-AppleSilicon.dmg" \
  "发布/Audio-to-Markdown-$VERSION-AppleSilicon.dmg.sha256"
```

Wait for tag CI to pass, then review draft asset names, sizes, checksums, tag commit, and notes before publication:

```bash
gh release edit "v$VERSION" --draft=false --latest
```

Finally verify the public repository, language switch, issue entry points, and DMG download without authentication. Compare the downloaded file's SHA-256 with the published checksum. Repeat this process for each update while retaining older releases.

Workflow syntax follows the [GitHub Actions documentation](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax); current macOS runner availability is listed in the official [runner-images repository](https://github.com/actions/runner-images).
