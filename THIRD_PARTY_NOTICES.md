# 第三方组件 / Third-party notices

[中文](#中文) · [English](#english)

## 中文

根目录 [LICENSE](LICENSE) 中的 MIT 许可适用于本项目自有代码和文档。第三方源代码、模型权重、运行环境及其说明文件分别适用原作者的许可；本项目不重新授权这些内容。

DMG 在根目录及应用内 `Contents/Resources/第三方许可/` 提供第三方许可材料。`Python依赖清单.json` 记录随包 Python 依赖的实际版本和项目链接；`Python及依赖许可/` 保留安装包中的许可证、版权与 NOTICE 文件。部分上游 wheel 未带独立许可证，已从对应官方源码补到 `补充依赖许可/`，来源、版本、归档成员和 SHA256 见该目录的 `来源.json`。

| 组件 | 用途 | 许可与材料 |
|---|---|---|
| FunASR、funasr-onnx、ONNX Runtime、PyTorch 及相关依赖 | 本地推理、模型导出、音频与数值计算 | 各上游许可；实际版本见依赖清单，完整文本见随包许可 |
| Paraformer、FSMN VAD、CT-Transformer、CAM++ | 识别、静音检测、标点、说话人特征 | 锁定模型卡声明 Apache-2.0；[模型许可及来源](分发资源/模型许可/来源.json) |
| Python standalone 及其底层组件 | 独立 Python 运行环境 | Python 和各底层组件原许可；[版本及来源](分发资源/Python底层组件许可/来源.json) |
| FFmpeg 9.0.2 | 通过独立进程解码与转换音频 | LGPL-2.1-or-later；完整原始源码、构建脚本及许可随 DMG 提供 |
| libsndfile 1.2.2 / SoundFile 0.14.0 | 读写音频 | libsndfile 使用 LGPL，SoundFile 使用 BSD；补充目录含上游构建来源及源码 |
| libogg 1.3.5、libvorbis 1.3.7、FLAC 1.4.3、Opus 1.4、mpg123 1.32.3、LAME 3.100 | 上游 libsndfile 二进制内含的音频组件 | 各自 BSD/LGPL 等许可；保留官方源码和版权文件 |
| python-soxr 1.1.0 / libsoxr | 依赖使用的重采样库 | LGPL-2.1-or-later 及内含组件许可；补充目录含对应源码归档 |

FFmpeg 构建未启用 GPL、nonfree 或网络协议，未修改其原始源码。构建参数见 [`build_audio_runtime.sh`](程序文件/build_audio_runtime.sh)。原始归档亦可从 [本版本 Release](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/tag/v2.5.1) 下载；许可全文见 [LGPL 2.1](分发资源/FFmpeg-LGPL-2.1.txt) 和 [FFmpeg 许可说明](分发资源/FFmpeg-LICENSE.md)。这一程序使用 FFmpeg 项目代码，保留其原作者版权。[FFmpeg 官方说明](https://ffmpeg.org/legal.html)

SoundFile 使用的二进制来源追溯至其 0.14.0 版本所引用的 `libsndfile-binaries` 提交 `a3e6f9769d0c7e91d2d036cf0fdbe5b4bbf18b87`。相关源码和上游构建文件收录于 `补充依赖许可/`；构建文件用于说明对应上游过程，不代表本项目逐一重编了这些 wheel。

可以修改、重编并替换适用许可允许的第三方组件。FFmpeg 位于应用资源目录 `runtime/bin/ffmpeg`；Python 扩展和动态库位于 `runtime/python/lib/python3.12/site-packages/`。修改后需重新进行本地应用签名。分发构建调整了动态库搜索路径并去除本机编译调试路径，具体处理见 [`package_release.py`](程序文件/package_release.py)。

补充许可证中的原作者姓名、联系地址及上游构建路径属于原始许可材料，不是本项目用户的录音或个人资料。请保留完整版权声明。再次分发时应同时保留适用许可、对应源码材料和所做修改说明。

## English

The root [MIT license](LICENSE) covers this project's own code and documentation. Third-party code, model weights, runtimes and accompanying documents retain their original licenses; this project does not relicense them.

The DMG and the app's `Contents/Resources/第三方许可/` directory include third-party notices. `Python依赖清单.json` lists the installed dependency versions and upstream URLs. `Python及依赖许可/` preserves license, copyright and NOTICE files from the distributions. Licenses omitted from some upstream wheels are supplemented from official sources in `补充依赖许可/`; `来源.json` records versions, URLs, archive members and SHA256 hashes.

The pinned Paraformer, FSMN VAD, CT-Transformer and CAM++ model cards declare Apache-2.0. Model cards, provenance and the license text are retained in [`分发资源/模型许可/`](分发资源/模型许可/来源.json). Python and its underlying libraries keep their individual licenses, with [runtime provenance](分发资源/Python底层组件许可/来源.json) included.

This software uses FFmpeg 9.0.2 under LGPL-2.1-or-later through a separate process. The audio-only build disables GPL, nonfree and network support. The unmodified FFmpeg source archive, the [exact build script](程序文件/build_audio_runtime.sh) and license texts are included in the DMG. Source is also available from [this version's Release](https://github.com/limadaiana810-oss/audio-to-markdown-mac/releases/tag/v2.5.1). See the [FFmpeg license information](https://ffmpeg.org/legal.html).

SoundFile 0.14.0 is BSD licensed and includes libsndfile 1.2.2 under LGPL. Its upstream binary also contains Ogg, Vorbis, FLAC, Opus, mpg123 and LAME components under their respective BSD/LGPL and other applicable licenses. The source versions and build files are traced to SoundFile's pinned `libsndfile-binaries` commit `a3e6f9769d0c7e91d2d036cf0fdbe5b4bbf18b87`. Corresponding source archives and notices are included in the supplemental directory. python-soxr 1.1.0 and its embedded libsoxr use LGPL-2.1-or-later and the included component licenses; their source archive is also provided.

These upstream build files document how the dependencies were built; this project does not claim to have rebuilt every wheel. The release packaging removes local debug paths and adjusts library search paths as shown in [`package_release.py`](程序文件/package_release.py).

Components may be modified and replaced as permitted by their licenses. FFmpeg is at `runtime/bin/ffmpeg`; Python extensions and shared libraries are under `runtime/python/lib/python3.12/site-packages/` inside the app's resources. Modified apps need to be signed locally again. Preserve applicable copyrights, license texts, corresponding source materials and change notices when redistributing. Upstream author names and addresses in license files are part of the original notices.
