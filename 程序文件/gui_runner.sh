#!/bin/bash
# 原生窗口入口：所有安装日志留在 stderr，stdout 只传 JSON 状态。
export TX_ENGINE=light
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
select_engine light || exit 2

# 分发包直接使用包内运行环境与模型，不下载、不依赖开发者机器的安装目录。
PORTABLE_ROOT="$(dirname "$PKG_DIR")/runtime"
if [ -f "$PORTABLE_ROOT/portable.json" ]; then
  PORTABLE_PYTHON="$PORTABLE_ROOT/python/bin/python3"
  if [ ! -x "$PORTABLE_PYTHON" ] || [ ! -x "$PORTABLE_ROOT/bin/ffmpeg" ] || [ ! -s "$PORTABLE_ROOT/models-light/models-light.manifest.json" ]; then
    printf '%s\n' '{"type":"error","message":"应用文件不完整，请重新复制安装包中的应用。"}'
    exit 2
  fi
  export TX_MODEL_DIR="$PORTABLE_ROOT/models-light"
  export TX_FFMPEG="$PORTABLE_ROOT/bin/ffmpeg" IMAGEIO_FFMPEG_EXE="$PORTABLE_ROOT/bin/ffmpeg"
  export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONDONTWRITEBYTECODE=1
  export NUMBA_CACHE_DIR="$APP_DIR/cache/numba"
  mkdir -p "$APP_DIR" "$NUMBA_CACHE_DIR" || exit 2
  exec "$PORTABLE_PYTHON" -B "$PKG_DIR/gui_backend.py" -- "$@"
fi

installer_pid=""
cancel_install() {
  trap '' INT TERM
  if [ -n "$installer_pid" ]; then
    # 安装进程单独成组，停止时连同下载、自检子进程一起回收。
    kill -INT -- "-$installer_pid" 2>/dev/null || true
    local checks=0
    while kill -0 -- "-$installer_pid" 2>/dev/null && [ "$checks" -lt 30 ]; do
      sleep .1
      checks=$((checks + 1))
    done
    kill -TERM -- "-$installer_pid" 2>/dev/null || true
    sleep .2
    kill -KILL -- "-$installer_pid" 2>/dev/null || true
    wait "$installer_pid" 2>/dev/null || true
  fi
  printf '%s\n' '{"type":"cancelled","message":"已停止准备，下次打开可以继续。"}'
  exit 130
}

if ! is_installed; then
  printf '%s\n' '{"type":"status","message":"正在准备转写工具，首次使用可能需要几分钟…"}'
  trap cancel_install INT TERM
  set -m  # Bash 3.2 为安装子任务分配独立进程组，即使没有终端窗口。
  /bin/bash "$PKG_DIR/install.sh" --engine light >&2 &
  installer_pid=$!
  wait "$installer_pid"
  rc=$?
  installer_pid=""
  set +m
  trap - INT TERM
  if [ "$rc" -ne 0 ]; then
    printf '%s\n' '{"type":"error","message":"转写工具准备未完成，请重新打开应用后重试。"}'
    exit 2
  fi
fi
if [ ! -x "$VPY" ] || [ ! -f "$APP_COPY/gui_backend.py" ]; then
  printf '%s\n' '{"type":"error","message":"应用文件不完整，请重新安装录音转写工具。"}'
  exit 2
fi
exec "$VPY" "$APP_COPY/gui_backend.py" -- "$@"
