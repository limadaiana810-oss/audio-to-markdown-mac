#!/bin/bash
# 双击运行：打开「录音转写工具」菜单。还没安装时会先自动安装。
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "$#" -eq 0 ] && [ -d "$HERE/录音转写.app" ]; then
  open "$HERE/录音转写.app"
  exit $?
fi
. "$HERE/程序文件/common.sh"
rc=0
if ! is_installed; then
  echo "还没有安装（或这个安装包比已装的新），先自动安装……"
  "${BASH:-/bin/bash}" "$HERE/程序文件/install.sh"; rc=$?
fi
if [ $rc -eq 0 ]; then
  "${BASH:-/bin/bash}" "$APP_COPY/launcher.sh" "$@"; rc=$?
fi
pause_if_tty
exit $rc
