# 补充依赖许可与源码 / Supplemental licenses and sources

此目录补齐部分上游 wheel 未附带的完整许可，并保存离线安装包内 LGPL 音频组件的对应源码材料。原文件保持原样；`来源.json` 记录下载地址、版本、文件大小与 SHA256，来自源码归档的文件还记录归档哈希和成员路径。

- `antlr4-python3-runtime`、`flatbuffers`、`jamo`、`jieba`、`sentencepiece`、`tokenizers`、`torch-complex`：补充官方许可证。sentencepiece 同时保留其内含 Darts-clone 与 libsais 的许可证。
- `tqdm`：保留官方 `LICENCE`（英国拼写）及其引用的 MPL 2.0 全文。
- `soxr`：保留 python-soxr 1.1.0 的官方 PyPI 源码归档，其中包含 libsoxr 源码、构建文件与组件许可。
- `libsndfile`、`libogg`、`libvorbis`、`flac`、`opus`、`mpg123`、`lame`：保留上游源码归档及版权材料；版本来自 SoundFile 0.14.0 所引用的 libsndfile-binaries 提交。`libsndfile/mac_build.sh` 与 `darwin.cmake` 是该上游提交的原始构建文件。`libsndfile/二进制来源.json` 记录该提交的二进制哈希，并已与本项目所用的原始 wheel 二进制核对一致。

torch-complex 的 PyPI 元数据把主页写作 `torch_complex`，实际官方仓库是 `kamo-naoyuki/pytorch_complex`。其维护者在 v0.4.4 标签之后补入 Apache 2.0 文本；此处记录补入许可证的固定提交。分发的四个 `torch_complex` Python 模块与该提交逐字节一致。版本元数据原本也声明 Apache Software License。

源码归档保留上游的完整内容，可能包含未用于本应用的示例、命令行工具或测试的额外许可。应以具体文件及组件声明为准。这里的上游构建脚本是溯源材料，不会在应用运行时执行。

This directory supplements licenses missing from some upstream wheels and includes corresponding source materials for bundled LGPL audio components. Original files are preserved. `来源.json` records source URLs, versions, byte sizes and SHA256 hashes, plus archive hashes and member paths where applicable.

The SoundFile sources and build files correspond to the `libsndfile-binaries` commit pinned by SoundFile 0.14.0. Its original arm64 library was verified byte-for-byte against the binary installed from the wheel. The python-soxr source archive includes libsoxr and its build and license files. Rebuild or replacement must follow the component's own instructions and license.

torch-complex's official repository is `kamo-naoyuki/pytorch_complex`, despite the stale homepage in its metadata. Its maintainer added the Apache-2.0 text after the v0.4.4 tag; the pinned license commit has the same four Python modules as the distributed v0.4.4 package. This provenance is recorded explicitly instead of inventing a license file for the older tag.

Upstream source archives may contain examples, tools or tests with additional licenses that are not part of the app's runtime. Refer to each component's original notices. The upstream build scripts here are provenance materials, not scripts executed by the app.
