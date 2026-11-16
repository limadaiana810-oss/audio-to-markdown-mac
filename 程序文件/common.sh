# shellcheck shell=bash
# 录音转写工具 Mac 版 —— 安装/启动/卸载共用的设置和函数。
# 注意：macOS 自带的 /bin/bash 是 3.2，这里只用 3.2 支持的语法（不用关联数组、mapfile、${x,,} 等）。

PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_VERSION="$(cat "$PKG_DIR/VERSION" 2>/dev/null || echo 未知)"
APP_NAME="录音转写工具"

# 程序环境（Python、依赖、模型）——只放在你自己的用户目录里，卸载时只删这里
APP_DIR="${TX_APP_DIR:-$HOME/Library/Application Support/录音转写工具}"
# 你的录音、结果、声纹样本、日志——卸载也不会删
USER_DIR="${TX_USER_DIR:-$HOME/录音转写}"
INBOX_DIR="$USER_DIR/待转写录音"
OUT_DIR="$USER_DIR/转写结果"
VP_DIR="$USER_DIR/声纹样本"
HW_FILE="$USER_DIR/热词.txt"
LOG_DIR="$USER_DIR/日志"

PY_HOME="$APP_DIR/python"
ORIGINAL_VENV="$APP_DIR/venv"
ORIGINAL_MODEL_DIR="$APP_DIR/models"
LIGHT_VENV="$APP_DIR/venv-light"
LIGHT_MODEL_DIR="$APP_DIR/models-light"
APP_COPY="$APP_DIR/app"

# Python 运行环境：Astral 官方 python-build-standalone（MIT/PSF 许可），sha256 取自该 Release 的 SHA256SUMS
PY_FILE="${TX_PY_FILE:-cpython-3.12.16+20261026-aarch64-apple-darwin-install_only_stripped.tar.gz}"
PY_SHA256="${TX_PY_SHA256:-353d5f1b4fd2715e7053f12f068435d5bbad16b91e057006b5d01e0925709a7e}"
PY_URLS="${TX_PY_URLS:-https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz https://releases.astral.sh/github/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-aarch64-apple-darwin-install_only_stripped.tar.gz}"
LOCK_FILE="${TX_LOCK_FILE:-$PKG_DIR/requirements-macos-arm64.txt}"
BUILD_LOCK_FILE="${TX_BUILD_LOCK_FILE:-$PKG_DIR/build-constraints.txt}"
MODELS_TSV="${TX_MODELS_TSV:-$PKG_DIR/models.tsv}"
LIGHT_LOCK_FILE="${TX_LIGHT_LOCK_FILE:-$PKG_DIR/requirements-light-macos-arm64.txt}"
LIGHT_MANIFEST="${TX_LIGHT_MANIFEST:-$PKG_DIR/models-light.manifest.json}"
MODEL_SOURCES="${TX_MODEL_SOURCES:-modelscope huggingface}"
MIN_MACOS_MAJOR=14

# 不让用户自己的 Python 环境变量/pip 配置干扰
unset PYTHONPATH PYTHONHOME VIRTUAL_ENV CONDA_PREFIX
export PYTHONNOUSERSITE=1 PIP_CONFIG_FILE=/dev/null PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1

LOG_FILE=""

say() { echo "$*"; }
step() { echo; echo "==== $* ===="; }
warn() { echo "【注意】$*"; }

die() {
  echo
  echo "【失败】$*"
  if [ -n "$LOG_FILE" ]; then
    echo "完整日志：$LOG_FILE"
    echo "（可以把这个日志文件发给给你安装包的人帮忙看）"
  fi
  exit 1
}

start_log() { # $1 = 日志名前缀
  mkdir -p "$LOG_DIR" 2>/dev/null || { echo "无法创建日志目录：$LOG_DIR"; exit 1; }
  LOG_FILE="$LOG_DIR/$1_$(date +%Y%m%d-%H%M%S).log"
  : > "$LOG_FILE" || die "无法写入安装日志：$LOG_FILE"
  # 屏幕和日志同时输出
  exec > >(tee -a "$LOG_FILE") 2>&1
}

sha256_of() { shasum -a 256 "$1" 2>/dev/null | awk '{print $1}'; }
size_of() { wc -c < "$1" 2>/dev/null | tr -d ' '; }

disk_available_mb() { # 使用目标目录所在卷，不假定与 HOME 同卷。
  local target="$1" available
  while [ ! -d "$target" ]; do
    [ "$target" != / ] || return 1
    target="$(dirname "$target")"
  done
  available="$(df -Pk "$target" 2>/dev/null | awk 'NR==2{print $4}')"
  case "$available" in ''|*[!0-9]*) return 1 ;; esac
  echo $((available / 1024))
}

require_space_mb() { # $1=目标路径，$2=所需 MB，$3=用途
  local available
  available="$(disk_available_mb "$1")" || die "无法检查磁盘余量：$1"
  say "$3：剩余约 $available MB，需要预留约 $2 MB（$1）"
  [ "$available" -ge "$2" ] || die "磁盘空间不够：$3 需要约 $2 MB，现在只剩 $available MB。请清理该目录所在磁盘后重试。"
}

curl_reason() { # 把 curl 退出码翻成中文
  case "$1" in
    6) echo "找不到服务器（DNS 解析失败），请检查网络" ;;
    7) echo "连不上服务器，请检查网络或代理" ;;
    22) echo "服务器返回错误（文件不存在或被拒绝）" ;;
    18|56|92) echo "下载中途断开" ;;
    28) echo "网络超时" ;;
    35|51|58|60|77|83) echo "HTTPS 证书/加密连接失败（公司网络或代理可能在拦截）" ;;
    23) echo "写入磁盘失败（磁盘满了？）" ;;
    *) echo "curl 错误码 $1" ;;
  esac
}

# 下载并校验：download_verified URL 目标文件 期望字节数(可空) 期望sha256
# 返回 0 成功；1 网络失败；2 校验失败
download_verified() {
  local url="$1" dest="$2" size="$3" sha="$4" part rc got
  if [ -f "$dest" ] && [ "$(sha256_of "$dest")" = "$sha" ]; then
    return 0
  fi
  part="$dest.part"
  mkdir -p "$(dirname "$dest")"
  if [ -n "$size" ] && [ -f "$part" ] && [ "$(size_of "$part")" -gt "$size" ] 2>/dev/null; then
    rm -f "$part"
  fi
  curl -fL --retry 3 --retry-delay 3 --connect-timeout 20 --speed-limit 1024 --speed-time 120 \
       -C - --progress-bar -o "$part" "$url" </dev/null
  rc=$?
  if [ $rc -eq 33 ] || [ $rc -eq 36 ]; then # 服务器不支持续传：从头下
    rm -f "$part"
    curl -fL --retry 3 --retry-delay 3 --connect-timeout 20 --speed-limit 1024 --speed-time 120 \
         --progress-bar -o "$part" "$url" </dev/null
    rc=$?
  fi
  if [ $rc -ne 0 ]; then
    echo "  下载失败：$(curl_reason $rc)  [$url]"
    # 416 = 已下载部分比服务器文件还大，删掉重来
    [ $rc -eq 22 ] && rm -f "$part"
    return 1
  fi
  if [ -n "$size" ] && [ "$(size_of "$part")" != "$size" ]; then
    echo "  文件大小不对（期望 $size 字节，实际 $(size_of "$part")），已删除，下次重下"
    rm -f "$part"; return 2
  fi
  got="$(sha256_of "$part")"
  if [ "$got" != "$sha" ]; then
    echo "  校验失败（sha256 不一致），文件可能被篡改或损坏，已删除：$(basename "$dest")"
    echo "  期望 $sha"
    echo "  实际 $got"
    rm -f "$part"; return 2
  fi
  mv -f "$part" "$dest" || { echo "  保存下载文件失败，保留已校验的临时文件供重试：$part"; return 1; }
}

select_engine() {
  case "$1" in
    original)
      ENGINE=original; ENGINE_LABEL="原版"
      VENV="$ORIGINAL_VENV"; MODEL_DIR="$ORIGINAL_MODEL_DIR"
      INSTALLED_MARK="$APP_DIR/installed.ok"; SELFTEST_MARK="$APP_DIR/selftest.ok" ;;
    light)
      ENGINE=light; ENGINE_LABEL="轻量版"
      VENV="$LIGHT_VENV"; MODEL_DIR="$LIGHT_MODEL_DIR"
      INSTALLED_MARK="$APP_DIR/installed-light.ok"; SELFTEST_MARK="$APP_DIR/selftest-light.ok" ;;
    *) echo "未知转写引擎：$1（请选择 original 或 light）" >&2; return 2 ;;
  esac
  VPY="$VENV/bin/python"
  export TX_ENGINE="$ENGINE" TX_MODEL_DIR="$MODEL_DIR"
}

light_deps_fingerprint() {
  printf '%s:%s:%s\n' "$(sha256_of "$LIGHT_LOCK_FILE")" "$(sha256_of "$BUILD_LOCK_FILE")" "$PY_SHA256"
}

is_installed() {
  [ -f "$INSTALLED_MARK" ] && [ "$(cat "$INSTALLED_MARK" 2>/dev/null)" = "$APP_VERSION" ] && [ -x "$VPY" ] || return 1
  if [ "$ENGINE" = light ]; then
    [ -s "$LIGHT_LOCK_FILE" ] && [ -s "$BUILD_LOCK_FILE" ] && [ -s "$LIGHT_MANIFEST" ] || return 1
    [ "$(cat "$VENV/.ok" 2>/dev/null)" = "$(light_deps_fingerprint)" ] || return 1
    [ "$(cat "$SELFTEST_MARK" 2>/dev/null)" = "$APP_VERSION:$(sha256_of "$MODEL_DIR/models-light.manifest.json")" ] || return 1
  fi
  return 0
}

pause_if_tty() {
  if [ -t 0 ]; then echo; read -r -p "按回车键关闭本窗口……" _x; fi
}

# .command 会先检查安装状态再交给 launcher。只读扫描参数，让显式选择也用于自动安装。
# 不保存用户偏好；没有参数或环境覆盖的新窗口使用轻量版。
_tx_selected="${TX_ENGINE:-light}"
_tx_next_engine=0
for _tx_arg in "$@"; do
  if [ "$_tx_next_engine" = 1 ]; then _tx_selected="$_tx_arg"; _tx_next_engine=0; continue; fi
  case "$_tx_arg" in
    --engine) _tx_next_engine=1 ;;
    --engine=*) _tx_selected="${_tx_arg#--engine=}" ;;
  esac
done
[ "$_tx_next_engine" = 0 ] || { echo "--engine 后面需要 original 或 light" >&2; exit 2; }
select_engine "$_tx_selected" || exit 2
unset _tx_selected _tx_next_engine _tx_arg
