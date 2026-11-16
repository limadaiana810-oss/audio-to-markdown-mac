# 更新记录 / Changelog

[中文首页](README.md) · [English README](README.en.md) · [所有公开版本 / Public releases](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases)

每个公开版本对应独立 tag、安装包和校验值。首次公开发布前的版本仅为本地开发历史，不代表存在可下载的 GitHub Release。

Each public version has a distinct tag, installer, and checksum. Versions before the first public release refer to local development history, not previously published GitHub Releases.

## [2.5.1](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/tag/v2.5.1) — 2026-11-16

### 中文

首次公开开源发布，基于本地 v2.5.0 的转写功能。

- 发布自有源码并采用 MIT 许可；保留第三方组件与模型各自的许可材料。
- 提供中文默认、可切换英文的项目介绍、开发与贡献说明。
- 提供 Apple Silicon DMG、对应源码与 SHA-256 校验文件。
- 建立问题反馈、Pull Request 和按版本保留安装包的发布方式。
- 保留紧凑原生界面、灰色完成状态、最多四并发、自动补位、按时间合并 Markdown。
- 本次没有更换识别模型或算法；没有新增“10 小时 5 分钟”的性能保证。

### English

First public open-source release, based on the transcription features in local v2.5.0.

- Publish project-owned source under MIT while retaining separate third-party and model licenses.
- Add Chinese-first documentation with English counterparts, development notes, and contribution guidance.
- Provide an Apple Silicon DMG, corresponding source, and SHA-256 checksums.
- Establish issue feedback, Pull Requests, and distinct downloads for each release.
- Retain the compact native UI, gray completion state, up to four workers, automatic queue refill, and chronological merged Markdown.
- No recognition-model or algorithm change, and no new claim that 10 hours can be transcribed in 5 minutes.

## 2.5.0 — 公开发布前的本地版本 / Pre-public local version

- 原生小窗口，单文件进度、完成后下移、整批成功后恢复列表原序。
- 最多四进程处理，合并文稿保留来源、日期时间段、时间戳及说话人标签。
- 完成状态改为灰色；完善离线分发、取消清理与源文件校验。

- Compact native window with per-file progress, completed rows moved down, and original order restored after batch success.
- Up to four workers and merged transcripts retaining sources, date/time ranges, timestamps, and speaker labels.
- Gray completion state; improvements to offline packaging, cancellation cleanup, and source verification.
