#!/bin/bash
# 双击运行：卸载程序环境（约 3.5 GB）。不会删除你的录音、转写结果、声纹样本和日志（在「录音转写」文件夹）。
HERE="$(cd "$(dirname "$0")" && pwd)"
"${BASH:-/bin/bash}" "$HERE/程序文件/uninstall.sh" "$@"
rc=$?
. "$HERE/程序文件/common.sh" && pause_if_tty
exit $rc
