# 发布与版本管理

**简体中文** · [English](RELEASING.en.md)

v2.5.1 是首次公开开源版本。v2.5.0 及更早的编号属于公开发布前的本地开发记录，不代表 GitHub 上存在对应安装包。

## 一个版本，对应一份可复查的交付物

`程序文件/VERSION` 是应用版本的唯一来源。`build_app.sh` 将它写入应用的两个版本字段；`package_release.py` 据此命名安装包。不要为了显示新版本而更改 `tx.py` 的缓存版本常量：它们控制识别缓存兼容性，职责不同。

| 内容 | 规则 |
|---|---|
| 开发主线 | `main`；接受经过检查的 Pull Request |
| 正式版本 | `MAJOR.MINOR.PATCH`，Git tag 使用 `vMAJOR.MINOR.PATCH` |
| 不兼容变化 | 增加主版本号，并写明迁移办法 |
| 向后兼容功能 | 增加次版本号 |
| 修复、文档或分发调整 | 增加修订号 |
| 安装包 | `Audio-to-Markdown-<版本>-AppleSilicon.dmg` |
| 校验文件 | 同名 `.dmg.sha256`，同时校验内容和文件名 |
| 源码 | Git tag 对应代码；可另附该次构建生成的 `Source-<版本>.zip` |
| 最新版本 | GitHub Releases 的 Latest 标记；README 直链指向明确版本 |

已公开的 tag、DMG 和校验文件不覆盖、不换包。发现问题发布下一个修订版本，并在旧 Release 说明问题及升级入口。旧版本保留自己的页面和下载链接；“最新”入口随正式发布更新。预览版标为 Pre-release，不替换稳定版 Latest。

本仓库已开启 GitHub Release 不可变保护，约束在正式 Release 公开发布后生效，届时版本 tag 和附件将被锁定。尚未公开的草稿候选可在核对中修正源码、候选 tag 和附件；修正后重新验证它们的一致性，再公开发布。正式发布后不覆盖 tag、DMG 或校验文件，修改通过下一版本交付。见 [GitHub 官方说明](https://docs.github.com/en/code-security/how-tos/secure-your-supply-chain/establish-provenance-and-integrity/prevent-release-changes)。

## 自动检查负责什么

[CI](../.github/workflows/ci.yml) 在 `main`、Pull Request 和 `v*` tag 上运行：

1. 校验版本号、中英文下载链接和 CHANGELOG；tag 构建还校验 tag 与版本一致。
2. 在 macOS 15 ARM64 / Python 3.12 安装带哈希的固定版本运行依赖，不下载识别模型。
3. 执行全部单元回归，覆盖调度、缓存恢复、时间合并、后台事件、取消、原生界面状态和发布身份。
4. 编译开发应用，核对签名完整性、应用版本和内置源码版本。

CI 通过不等于完整 DMG 已通过真实音频验收。模型推理、离线可移植性、实际界面操作和长录音性能由维护者在发布机器上另行验证；本流程不会在 CI 自动下载模型、制作或上传安装包。

## 发布步骤

在 Apple Silicon Mac 上，从干净的发布提交构建。先在 Pull Request 更新 `VERSION`、[CHANGELOG](../CHANGELOG.md) 和两份 README 中当前版本的下载链接。说明实际行为变化、已做验证和已知限制；性能数字必须带机器、音频性质、缓存状态与版本。

```bash
# 使用已准备好的轻量运行环境
PYTHON="$HOME/Library/Application Support/录音转写工具/venv-light/bin/python"
VERSION="$(cat 程序文件/VERSION)"

"$PYTHON" 程序文件/check_release.py --tag "v$VERSION"
"$PYTHON" -m unittest discover -s 程序文件/tests -v
bash 程序文件/build_release.sh
```

首次构建需先执行 `bash 程序文件/install.sh --engine light` 准备本地运行环境和模型；也可以设置 `TX_BUILD_APP_DIR` 指向已经准备好的构建环境。FFmpeg 对应源包不进入 Git 历史；缺失时构建器通过 `fetch_ffmpeg_source.py` 从固定官方地址下载并核对 SHA-256。DMG 仍附上完整对应源码、构建脚本和第三方许可。

构建后，核对本次 `.app` 与 `.dmg`（下面的应用路径应指向本次产物，不能使用旧的安装副本）：

```bash
"$PYTHON" 程序文件/check_release.py --tag "v$VERSION" \
  --app ".package-cache/release-stage/录音转写.app" \
  --dmg "发布/Audio-to-Markdown-$VERSION-AppleSilicon.dmg"
```

挂载 DMG 并检查 README 中英文、源码压缩包、许可文件和可拖入 Applications 的应用。运行真实中文测试音频：单份、多份、超过四份的队列补位、停止后恢复、时间排序、来源与说话人输出。使用 `test_portable_release.py` 在新目录中验证拒绝联网及旧安装依赖后的转写与取消；该脚本需要包含指定验收句的自制音频，不能拿真实会议录音充当公开测试数据。

确认构建产物、回归结果和运行验证后，再创建 tag 和草稿 Release。`release-notes.md` 应是准备好的中英文说明；发布前回读内容，不上传包含本机路径和真实录音信息的原始测试报告。

```bash
git tag -a "v$VERSION" -m "Release v$VERSION"
git push origin "v$VERSION"
gh release create "v$VERSION" --verify-tag --draft \
  --title "Audio to Markdown for Mac v$VERSION" \
  --notes-file release-notes.md \
  "发布/Audio-to-Markdown-$VERSION-AppleSilicon.dmg" \
  "发布/Audio-to-Markdown-$VERSION-AppleSilicon.dmg.sha256"
```

等待 tag 的 CI 通过，核对草稿附件名称、大小、校验值、tag 指向的提交及说明，再公开发布：

```bash
gh release edit "v$VERSION" --draft=false --latest
```

最后以未登录访问方式核对仓库、中英文切换、Issue 入口和 DMG 下载；将实际下载文件的 SHA-256 与发布值比对。每次更新沿用这个流程，保留旧 Release 的可追溯性。

流程语法参考 [GitHub Actions 工作流文档](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax)；当前 macOS 运行器可用性见 [官方 runner-images](https://github.com/actions/runner-images)。
