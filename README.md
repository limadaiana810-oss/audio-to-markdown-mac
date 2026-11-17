# Audio to Markdown for Mac · 录音转写

**中文** | [English](README.en.md)

[![版本](https://img.shields.io/github/v/release/limadaiana810-oss/audio-to-markdown-mac)](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/latest)
[![系统](https://img.shields.io/badge/macOS-14%2B%20%C2%B7%20Apple%20Silicon-555)](#下载安装)
[![许可](https://img.shields.io/badge/自有代码-MIT-555)](LICENSE)

**录音转写 mac 应用， UI 窗口，下载的 dmg 中带转写模型**
自用大疆 mic3 ，16g 内存 macair 可用，3 小时音频 5 分钟解决，优化了本地转写的速度。
使用的轻量转写模型，最高 4 个并发转写，最后会把内容合并成 带时间轴和区分说话人的 markdown 文件，可以直接喂给 ai 形成自己的人生上下文。
让 Mac 接过这份工作：选中录音，本地转写，拿走一份带时间戳、说话人和来源的 Markdown。再把它交给你喜欢的 AI，整理总结、会议纪要或待办。

一个小窗口，做完一件事。适合录音笔、领夹麦克风和手机录音的日常整理。

**[下载 v2.5.1 · Apple Silicon DMG](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/download/v2.5.1/Audio-to-Markdown-2.5.1-AppleSilicon.dmg)** · [查看最新版本](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/latest) · [反馈问题 / 提建议](https://github.com/limadaiana810-oss/audio-to-markdown-mac/issues/new/choose)

## 它能做什么

| 你要做的事 | 工具怎么处理 |
|---|---|
| 一次整理多份录音 | 选择多个文件或一个文件夹，最高四并发；一份完成，下一份自动补位 |
| 转写长录音 | 默认约 15 分钟分段，分段并行处理，按原始时间顺序还原 |
| 看清每份录音的状态 | 单文件进度、灰色完成状态、底部总进度；整批结束后恢复列表原顺序 |
| 把结果交给 AI | 自动合并为一份 `.md`，保留日期时间段、来源、时间戳、说话人和正文 |
| 中途停一下 | 保留已校验的分段结果，重新开始时复用健康缓存 |
| 在本机处理录音 | 安装包自带轻量模型与运行环境；转写无需账号、无需联网、不上传音频 |

界面使用紧凑的 macOS 原生窗口和图标按钮，悬停可查看用途。当前应用界面及输出标签为中文；本页提供中英文文档切换。

## 下载安装

**系统要求：Apple Silicon Mac，macOS 14 或更新版本。** 本版不支持 Intel Mac。

1. [下载 DMG](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/download/v2.5.1/Audio-to-Markdown-2.5.1-AppleSilicon.dmg)，打开后把「录音转写」拖进 `Applications`。
2. 打开应用，用波形加号选多个录音，或用文件夹加号选一个录音文件夹。
3. 点击 ▶。显示「转写完成」后，用文档图标打开 Markdown，或用另存为按钮取走文稿。

文件夹只导入当前层，不递归子文件夹。支持 WAV、MP3、M4A、AAC、FLAC 等音频，以及可解码的 MP4、MOV 音轨。一般用户无需安装 Python、Homebrew 或开发工具。

安装包约 1 GB：窗口很小，但离线模型和运行环境也要一起带走。下载页提供校验文件及对应源码。当前应用采用本地签名，**尚未经过 Apple Developer ID 公证**；macOS 可能要求到「系统设置 → 隐私与安全性」确认打开。请从本仓库的 Releases 下载，并核对该版本的 SHA-256 校验值。

## 你会得到什么

单文件、多文件都输出一份完整 Markdown。下面是虚构示例，省略了来源文件的本地链接：

```markdown
# 录音转写

## 2026-11-12 10:00:00–10:30:00

来源：会议_20261112_100000.wav

[00:00:12] 说话人1：今天先讨论项目进度。

[00:00:25] 说话人2：我先介绍目前的情况。
```

- 多份录音根据**文件名中的日期时间**排序；无法确定时间的录音会明确标注，并放在已知时间之后。
- 标题结束时间由开始时间加实际录音时长得到。段落时间戳相对于**各自录音开头**。
- 说话人编号在每份录音内独立，不能据此认定不同文件的“说话人1”是同一个人。
- 识别到了文字但不能可靠定位时间的内容会保留为待确认项，空录音也保留章节说明。

默认文稿位置是 `~/录音转写/转写结果/合并全文.md`，每批历史全文保存在同目录的 `合并全文/` 中。应用里的文稿按钮指向**本次**结果；建议直接从按钮打开或另存为。

本工具负责转写，AI 总结由你自行选择平台完成。它不会调用总结模型补造原话。姓名、金额、否定词、专业词和说话人归属，在重要场景仍需回听核对。

## 到底有多快

下面是已有的长任务测量，**来自 v2.3.0，不能当作当前版本在所有电脑上的成绩**：

| 测试素材 | 机器 | 并发 | 端到端耗时 |
|---|---|---:|---:|
| 单份 10 小时中文合成录音 | Apple M5 / 16 GB | 4 | 8 分 26 秒 |
| 40 份各 15 分钟中文合成录音 | Apple M5 / 16 GB | 4 | 8 分 39 秒 |

两轮均为空缓存，包含模型校验、转码、加载、识别、标点、说话人处理及导出，不含首次安装。素材是带编号检查点的合成语音，不能证明真实多人会议的识别准确率。v2.5.0 已用六份合成录音验证队列补位、四进程处理和合并顺序；v2.5.1 未改变识别算法。

**“10 小时音频，5 分钟完成”是优化目标，目前未通过实测验收。** M5 Pro / 48 GB 的表现、当前版本的 10 小时性能和真实多人准确率，都需要继续测量。轻量引擎目前走 CPU 推理；耗时也受录音内容、格式和缓存状态影响。

如果你愿意帮忙，欢迎在 [Issue](https://github.com/limadaiana810-oss/audio-to-markdown-mac/issues/new/choose) 中留下机器型号、内存、版本、音频时长、缓存情况和总耗时。别把第二次命中缓存的速度当成首次转写速度——这次让进度条公平参赛。

## 代码与开发

原生界面采用 **SwiftUI / AppKit**，调度与导出使用 **Python**。轻量链路组合 **FSMN VAD、Paraformer ONNX INT8、CT-Transformer 标点与 CAM++ 说话人特征**，处理流程和模块职责见[技术说明](docs/ARCHITECTURE.md)。

```text
程序文件/
├── TranscriberApp.swift       原生窗口、队列状态、文稿操作
├── gui_backend.py             JSON 事件、进程管理、完成校验
├── tx.py                      音频准备、最多四进程、分段与恢复缓存
├── light_engine.py            轻量模型推理与时间对齐
├── light_postprocess.py       标点、说话人聚类、待确认内容
├── batch_export.py            按时间合并 Markdown、保留来源
├── build_app.sh               编译开发应用
├── build_release.sh           制作离线应用与 DMG
└── tests/                     调度、恢复、导出与原生界面状态回归
```

开发需要 Apple Silicon Mac 和 Xcode Command Line Tools。完整命令与构建依赖见[开发与架构](docs/ARCHITECTURE.md#本地开发)，欢迎先从输出格式、测试、安装体验或文档入手。

## 一起把它做好

第一次来，可以直接[报告问题或提建议](https://github.com/limadaiana810-oss/audio-to-markdown-mac/issues/new/choose)。想改代码，欢迎 Fork 后提交 [Pull Request](https://github.com/limadaiana810-oss/audio-to-markdown-mac/pulls)。提交前可看[贡献指南](CONTRIBUTING.md)：复现步骤、预期与实际行为，比一句“不太好用”更容易变成下一次修复。

反馈中请附应用版本、macOS 版本、芯片与内存。录音和文稿可能包含隐私；提供经授权的短样本或脱敏描述即可，不必公开整场会议。

版本使用 `v主版本.次版本.修订版本`。每次发布都有独立的 Git tag、Release、DMG 和校验文件；旧版保留自己的下载地址，不用新安装包覆盖旧版。[更新记录](CHANGELOG.md)解释每次变化，[Releases](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases)保留可下载版本，维护者可查看[发布流程](docs/RELEASING.md)。

## 开源许可

本项目自有代码采用 [MIT License](LICENSE)。Python、FFmpeg、第三方依赖和模型权重分别沿用各自许可，不能把根目录的 MIT 许可直接套用于整个安装包。来源与分发材料见[第三方说明](THIRD_PARTY_NOTICES.md)及 DMG 内的 `第三方许可/`。

相关主题：Mac 本地录音转文字、离线中文语音识别、Apple Silicon 音频转写、录音笔批量转写、带时间戳的说话人转写、音频转 Markdown、AI 会议纪要前处理。
