# Contributing

[中文](CONTRIBUTING.md) | **English**

Thanks for helping make this small tool useful. Code, documentation, reproducible bugs, and benchmarks with clear conditions are all welcome.

## Report a problem

Choose a template in [Issues](https://github.com/limadaiana810-oss/audio-to-markdown-mac/issues/new/choose) and include, when possible:

- App version, macOS version, chip, and memory.
- Steps to reproduce, expected behavior, and actual behavior.
- File count, formats, total audio duration, and whether existing caches were used.
- The error message or a short reproducing sample.

Logs are normally in `~/录音转写/日志/`. Review filenames, local paths, names, meeting content, and other sensitive information before posting them. Do not publish recordings, transcripts, hotwords, or voiceprints without permission. A description or synthetic sample is useful when the original recording cannot be shared.

For performance feedback, report **end-to-end elapsed time** and state whether installation, model loading, decoding, and export are included. Compare fresh transcription and cache reuse separately. For accuracy reports, a short manually transcribed reference alongside the recognition result is helpful, with notes on noise, overlapping speech, and accents.

## Send a Pull Request

1. Fork the repository and create a branch from `main`. Open an Issue first for a large feature or model change.
2. Keep the change focused on one problem. Describe the user action and the resulting behavior.
3. Add a test that reproduces the problem or verifies the behavior where appropriate. Documentation-only edits do not need artificial tests.
4. Run relevant checks from the [development guide](docs/ARCHITECTURE.en.md#local-development). UI and offline-packaging changes also need an actual build and inspection.
5. Update affected Chinese and English documentation. Open a PR with the validation steps and results.

Do not commit model weights, audio, transcripts, inference caches, virtual environments, personal logs, or DMGs to Git history. Installers are distributed through GitHub Releases.

## Behavior to preserve

- The GUI defaults to the light pipeline, with at most four workers globally—not four per input file.
- Task completion order must not change the order of speech inside a recording or the merged document's sorting rules.
- Failure, partial completion, or a completion event without valid output must not appear as batch success.
- Stopping must clean up child processes; verified chunk results should remain recoverable.
- Transcripts must retain sources. Do not invent missing speech to make a passage sound smoother.
- Model or inference-parameter changes must account for cache fingerprints to avoid reusing incompatible results.

## Versions and releases

The version source is `程序文件/VERSION`, using `MAJOR.MINOR.PATCH`.

| Change | Typical version update |
|---|---|
| Bug fix, compatibility improvement, or packaging revision | Increase PATCH, e.g. `2.5.1 → 2.5.2` |
| Backward-compatible feature | Increase MINOR, e.g. `2.5.1 → 2.6.0` |
| Incompatible behavior, interface, or data-format change | Increase MAJOR and document migration |

Regular contribution PRs do not need to bump the version. The maintainer updates `VERSION`, `CHANGELOG.md`, download links, and release notes together. Public tags use names such as `v2.5.1`. Publish each release under a new tag with its matching installer; retain old assets and checksums. Fix an already published package through a new version, rather than silently replacing old files. See the [release guide](docs/RELEASING.en.md) for the full process.

## Licensing

Contributions to project-owned code use the repository's [MIT License](LICENSE). When introducing third-party code, models, or other materials, identify the source and original license and retain required notices. They do not automatically become MIT-licensed. See [third-party notices](THIRD_PARTY_NOTICES.md).
