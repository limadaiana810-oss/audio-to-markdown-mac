# Audio to Markdown for Mac

[中文](README.md) | **English**

[![Release](https://img.shields.io/github/v/release/limadaiana810-oss/audio-to-markdown-mac)](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/latest)
[![Platform](https://img.shields.io/badge/macOS-14%2B%20%C2%B7%20Apple%20Silicon-555)](#download-and-use)
[![License](https://img.shields.io/badge/project%20code-MIT-555)](LICENSE)

**While recording: “This is important.” While replaying: “Why did I record ten hours?”**

Let your Mac take the next shift. Select recordings, transcribe locally, and get one Markdown document with timestamps, speaker labels, and sources. Hand that file to your preferred AI for summaries, meeting notes, or action items.

A small native window with one job. Built for recordings from voice recorders, wireless microphones, and phones, with a focus on Chinese speech.

**[Download v2.5.1 · Apple Silicon DMG](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/download/v2.5.1/Audio-to-Markdown-2.5.1-AppleSilicon.dmg)** · [Latest release](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/latest) · [Report a bug / suggest a feature](https://github.com/limadaiana810-oss/audio-to-markdown-mac/issues/new/choose)

## What it does

| Your task | How the app handles it |
|---|---|
| Process a batch of recordings | Select multiple files or a folder; up to four workers, with the next file admitted when one finishes |
| Transcribe long recordings | Split into roughly 15-minute chunks, process in parallel, then restore chronological order |
| See each file's status | Per-file progress, gray completed rows, and overall progress; the original list order returns when the batch succeeds |
| Prepare a document for AI | Merge everything into one `.md` with date/time ranges, sources, timestamps, speaker labels, and speech |
| Stop and resume | Keep verified chunk results and reuse valid caches on the next run |
| Keep transcription local | Bundled models and runtime; no account, connection, or audio upload required for transcription |

The compact macOS interface uses icon buttons with tooltips. **The app interface and output labels are currently in Chinese.** The language links above switch the documentation, not the app or its recognition model.

## Download and use

**Requirements: an Apple Silicon Mac running macOS 14 or later.** Intel Macs are not supported by this release.

1. [Download the DMG](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/download/v2.5.1/Audio-to-Markdown-2.5.1-AppleSilicon.dmg), open it, and drag 「录音转写」 into `Applications`.
2. Open the app. Use the waveform-plus button to select files, or the folder-plus button to select a recording folder.
3. Press ▶. When 「转写完成」 appears, open the Markdown with the document button or save a copy.

Folder import reads the current folder only, without recursing into subfolders. Supported inputs include WAV, MP3, M4A, AAC, FLAC, and decodable audio tracks in MP4 and MOV. End users do not need to install Python, Homebrew, or developer tools.

The download is about 1 GB: the window is small, but the offline models and runtime travel with it. Each release provides checksums and the corresponding source. The app is ad-hoc signed and **is not Apple Developer ID notarized**. macOS may require you to approve opening it in System Settings → Privacy & Security. Download from this repository's Releases and compare the file's SHA-256 with that release's checksum.

## The output

Both a single file and a batch produce one complete Markdown document. This fictional example omits the local link behind the source filename:

```markdown
# 录音转写

## 2026-11-12 10:00:00–10:30:00

来源：会议_20261112_100000.wav

[00:00:12] 说话人1：今天先讨论项目进度。

[00:00:25] 说话人2：我先介绍目前的情况。
```

- Recordings are ordered by the **date and time in their filenames**. Unknown recording times are marked explicitly and sorted after known times.
- The heading's end time is the start time plus the actual recording duration. Paragraph timestamps are relative to the **start of each source recording**.
- Speaker numbers are independent within each recording. “Speaker 1” in two files does not establish that they are the same person.
- Recognized text without a reliable time alignment is retained for review. Empty recordings keep a section explaining their result.

The latest merged transcript is saved to `~/录音转写/转写结果/合并全文.md`; historical batch documents are in the adjacent `合并全文/` folder. The app's document button points to **this batch's** output, so opening or saving from the app is the easiest route.

AI summarization is a separate step you choose and initiate. The app does not ask a summarization model to invent missing speech. For important material, check names, amounts, negations, technical terms, and speaker assignments against the recording.

## How fast is it?

These existing long-task measurements are from **v2.3.0, not a benchmark of the current release on every Mac**:

| Input | Machine | Workers | End-to-end time |
|---|---|---:|---:|
| One 10-hour synthetic Chinese recording | Apple M5 / 16 GB | 4 | 8 min 26 sec |
| Forty 15-minute synthetic Chinese recordings | Apple M5 / 16 GB | 4 | 8 min 39 sec |

Both runs started with empty caches and included model verification, decoding, loading, recognition, punctuation, speaker processing, and export. Initial installation was excluded. The input was synthetic speech with numbered checkpoints, so these timings do not establish accuracy on real meetings. Six synthetic recordings were used to verify v2.5.0's four-worker queue, slot refill, and merged ordering. v2.5.1 does not change the recognition algorithm.

**“10 hours of audio in 5 minutes” is an optimization target, not a verified result.** Performance on an M5 Pro / 48 GB, current-release 10-hour timing, and real multi-speaker accuracy still need measurement. The light engine currently uses CPU inference; content, format, and cache state also affect runtime.

Want to help? [Share a benchmark](https://github.com/limadaiana810-oss/audio-to-markdown-mac/issues/new/choose) with your Mac model, memory, app version, audio duration, cache state, and total elapsed time. A cached second run belongs in its own category—give the progress bar a fair race.

## Code and development

The native interface uses **SwiftUI / AppKit**; **Python** handles scheduling and export. The light pipeline combines **FSMN VAD, Paraformer ONNX INT8, CT-Transformer punctuation, and CAM++ speaker features**. See the [architecture guide](docs/ARCHITECTURE.en.md) for processing flow and responsibilities.

```text
程序文件/
├── TranscriberApp.swift       Native window, queue state, transcript actions
├── gui_backend.py             JSON events, process control, completion checks
├── tx.py                      Audio preparation, up to four workers, chunk caches
├── light_engine.py            Light-model inference and time alignment
├── light_postprocess.py       Punctuation, speaker clustering, review items
├── batch_export.py            Chronological Markdown merge and source retention
├── build_app.sh               Build the development app
├── build_release.sh           Package the offline app and DMG
└── tests/                     Scheduling, recovery, export, and native UI state
```

Development requires an Apple Silicon Mac and Xcode Command Line Tools. See [local development](docs/ARCHITECTURE.en.md#local-development) for setup and build commands. Output formatting, tests, installation experience, and documentation are good places to start.

## Help shape the next release

[Report a bug or propose a feature](https://github.com/limadaiana810-oss/audio-to-markdown-mac/issues/new/choose), or fork the repository and send a [Pull Request](https://github.com/limadaiana810-oss/audio-to-markdown-mac/pulls). The [contribution guide](CONTRIBUTING.en.md) explains what helps: a reproducible example and expected versus actual behavior make a fix much easier.

Include the app version, macOS version, chip, and memory. Recordings and transcripts may contain private information; a short sample you have permission to share or a redacted description is enough. Please do not upload a whole private meeting.

Versions follow `vMAJOR.MINOR.PATCH`. Each published version has its own Git tag, Release, DMG, and checksum. Old download addresses stay tied to their original files. Read the [changelog](CHANGELOG.md) for changes, [Releases](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases) for downloads, and the [release guide](docs/RELEASING.en.md) for maintenance.

## License

Project-owned code is available under the [MIT License](LICENSE). Python, FFmpeg, third-party dependencies, and model weights retain their respective licenses; the root MIT license does not relicense the whole installer. See [third-party notices](THIRD_PARTY_NOTICES.md) and `第三方许可/` inside the DMG for sources and distribution materials.

Related topics: local Mac audio transcription, offline Chinese speech recognition, Apple Silicon transcription, batch voice-recorder transcription, speaker labels and timestamps, audio to Markdown, and AI meeting-note preparation.
