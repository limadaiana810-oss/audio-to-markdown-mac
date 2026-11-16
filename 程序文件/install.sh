#!/bin/bash
# 录音转写工具 Mac 版 安装脚本（由「启动.command」自动调用）。可重复运行：已完成的步骤会跳过，下载中断会续传。
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

LOCK_DIR="$APP_DIR/.install.lock"
LOCK_OWNED=0

cleanup() {
  # 未拿到锁的窗口退出时，不能删掉另一个安装进程的锁。
  if [ "$LOCK_OWNED" = 1 ] && [ "$(cat "$LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then
    rm -rf "$LOCK_DIR" 2>/dev/null
  fi
  LOCK_OWNED=0
}
on_int() { echo; echo "已中断。下次双击「启动.command」可重试，已校验的下载会复用。"; cleanup; exit 130; }

check_platform() {
  step "1/6 检查电脑"
  local os ver major arch arm64 translated
  os="$(uname -s)"
  [ "$os" = "Darwin" ] || die "本工具只支持 macOS（检测到：${os}）。"
  ver="$(sw_vers -productVersion 2>/dev/null)"
  major="${ver%%.*}"
  arch="$(uname -m)"
  arm64="$(sysctl -n hw.optional.arm64 2>/dev/null)"
  translated="$(sysctl -n sysctl.proc_translated 2>/dev/null)"
  say "macOS 版本：${ver:-未知}　芯片架构：$arch$( [ "$translated" = 1 ] && echo '（终端正通过 Rosetta 运行）')"
  if [ "$arch" != "arm64" ] && [ "$arm64" != "1" ]; then
    die "这台是 Intel 芯片的 Mac，本安装包暂不支持。
原因：语音识别依赖的 PyTorch 新版只提供 Apple 芯片（M1/M2/M3/M4…）的 Mac 版本，
Intel Mac 只能用很旧的版本，没有经过测试。"
  fi
  case "$major" in ''|*[!0-9]*) die "无法识别 macOS 版本（${ver}）。" ;; esac
  if [ "$major" -lt "$MIN_MACOS_MAJOR" ]; then
    die "macOS $ver 版本太旧：需要 macOS 14（Sonoma）或更新。
可在「系统设置 → 通用 → 软件更新」升级后再安装。（原因：所用 PyTorch 2.15 只支持 macOS 14 以上）"
  fi
  say "✓ 支持：Apple 芯片 + macOS $ver"
}

# 还需要多少 MB 空间（已完成的部分不算）
need_mb() {
  local n=1500  # 余量：临时文件、日志
  [ -f "$PY_HOME/.ok" ] || n=$((n + 250))
  [ -f "$VENV/.ok" ] || n=$((n + 2200))
  local m
  for m in asr vad punc spk; do
    [ -f "$MODEL_DIR/$m/.ok" ] && continue
    case $m in asr) n=$((n + 900)) ;; punc) n=$((n + 1150)) ;; *) n=$((n + 40)) ;; esac
  done
  echo $n
}

check_disk() {
  step "2/6 检查磁盘空间"
  require_space_mb "$APP_DIR" "$(need_mb)" "原版安装"
  say "✓ 空间足够（提示：转写时每个 10 小时录音还会临时占用约 1.2 GB，做完自动删除）"
}

acquire_lock() {
  mkdir -p "$APP_DIR" || die "无法创建程序目录：$APP_DIR"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    local pid; pid="$(cat "$LOCK_DIR/pid" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      die "另一个安装窗口正在运行（进程 ${pid}），请等它结束或关掉它后再试。"
    fi
    rm -rf "$LOCK_DIR"; mkdir "$LOCK_DIR" || die "无法创建安装锁：$LOCK_DIR"
  fi
  echo $$ > "$LOCK_DIR/pid" || die "无法写入安装锁：$LOCK_DIR"
  LOCK_OWNED=1
}

copy_program() {
  # 把程序文件复制进程序目录：这样安装后把压缩包/解压出来的文件夹挪走也不影响使用
  rm -rf "$APP_COPY.tmp"
  cp -R "$PKG_DIR" "$APP_COPY.tmp" || die "复制程序文件失败"
  rm -rf "$APP_COPY"; mv "$APP_COPY.tmp" "$APP_COPY" || die "复制程序文件失败"
}

install_python() {
  step "3/6 准备独立的 Python 3.12（只放在程序目录，不影响系统）"
  if [ -f "$PY_HOME/.ok" ] && [ "$(cat "$PY_HOME/.ok")" = "$PY_SHA256" ] && "$PY_HOME/bin/python3" -c "pass" 2>/dev/null; then
    say "✓ 已就绪，跳过"; return
  fi
  local dl="$APP_DIR/downloads/$PY_FILE" ok=0 u rc
  say "下载约 25 MB（来源：Astral 官方 python-build-standalone，下载后校验 sha256）"
  for u in $PY_URLS; do
    download_verified "$u" "$dl" "" "$PY_SHA256"; rc=$?
    if [ $rc -eq 0 ]; then ok=1; break; fi
    say "  换一个下载地址重试……"
  done
  [ $ok = 1 ] || die "Python 运行环境下载失败。需要能访问 github.com 或 releases.astral.sh；
如果在国内，可以打开代理/VPN 后再双击「启动.command」重试。"
  rm -rf "$APP_DIR/python.tmp" "$PY_HOME"
  mkdir -p "$APP_DIR/python.tmp"
  tar -xzf "$dl" -C "$APP_DIR/python.tmp" || die "解压 Python 失败（文件可能损坏，已校验过 sha256，请检查磁盘空间）"
  mv "$APP_DIR/python.tmp/python" "$PY_HOME" && rm -rf "$APP_DIR/python.tmp"
  "$PY_HOME/bin/python3" -c "import sys; print('Python', sys.version.split()[0])" || die "Python 无法运行（可能不是 Apple 芯片，或系统太旧）"
  echo "$PY_SHA256" > "$PY_HOME/.ok" || die "无法保存 Python 安装状态，请检查磁盘空间。"
  rm -f "$dl"
  say "✓ Python 就绪"
}

install_deps() {
  step "4/6 安装识别程序依赖（PyTorch、FunASR、ffmpeg 等，下载约 320 MB，来自 PyPI，逐个校验哈希）"
  local fp; fp="$(cat "$LOCK_FILE" "$BUILD_LOCK_FILE" | shasum -a 256 | awk '{print $1}')-$PY_SHA256"
  if [ -f "$VENV/.ok" ] && [ "$(cat "$VENV/.ok")" = "$fp" ] && "$VPY" -c "pass" 2>/dev/null; then
    say "✓ 已就绪，跳过"; return
  fi
  rm -rf "$VENV"
  "$PY_HOME/bin/python3" -m venv "$VENV" || die "创建运行环境失败"
  local idx=""
  [ -n "$TX_PIP_INDEX_URL" ] && idx="--index-url $TX_PIP_INDEX_URL" && say "使用自定义镜像：${TX_PIP_INDEX_URL}（仍然逐个校验哈希）"
  local cache="$APP_DIR/cache/pip"
  # shellcheck disable=SC2086
  "$VPY" -m pip install --cache-dir "$cache" --require-hashes --no-deps $idx -r "$BUILD_LOCK_FILE" \
    || die "安装构建工具失败（多半是网络问题：需要能访问 pypi.org / files.pythonhosted.org）。再双击「启动.command」可重试。"
  # shellcheck disable=SC2086
  "$VPY" -m pip install --cache-dir "$cache" --require-hashes --no-deps --no-build-isolation $idx -r "$LOCK_FILE" \
    || die "安装依赖失败（多半是网络问题：需要能访问 pypi.org / files.pythonhosted.org；已下载的会缓存）。
再双击「启动.command」可重试。"
  "$VPY" -c "import torch, funasr, soundfile, scipy, sklearn, imageio_ffmpeg" \
    || die "依赖装完但无法导入，请把日志发给提供者"
  echo "$fp" > "$VENV/.ok" || die "无法保存依赖安装状态，请检查磁盘空间。"
  rm -rf "$APP_DIR/cache"
  say "✓ 依赖就绪"
}

install_models() {
  step "5/6 下载语音模型（约 2.05 GB：识别、静音检测、标点、声纹 4 个模型）"
  say "先从魔搭 ModelScope 下载，不行自动换 Hugging Face；每个文件都会校验 sha256，断了再运行会续传。"
  local m src ok rc line s name file size sha url nfiles manifest_hash installed_source
  manifest_hash="$(sha256_of "$MODELS_TSV")"
  [ -n "$manifest_hash" ] || die "读取原版模型清单失败：$MODELS_TSV"
  for m in asr vad punc spk; do
    local d="$MODEL_DIR/$m"
    installed_source="$(cat "$d/.source" 2>/dev/null)"
    if [ -n "$installed_source" ] && [ "$(cat "$d/.ok" 2>/dev/null)" = "$installed_source:$manifest_hash" ] \
       && original_model_verified "$m" "$installed_source"; then
      say "✓ $m 模型清单及文件校验通过"; continue
    fi
    ok=0
    for src in $MODEL_SOURCES; do
      grep -q "^$src	$m	" "$MODELS_TSV" || continue
      if [ -f "$d/.source" ] && [ "$(cat "$d/.source")" != "$src" ]; then rm -rf "$d"; fi  # 不混用不同来源的文件
      mkdir -p "$d" && echo "$src" > "$d/.source" || die "无法准备模型目录：$d"
      say "· $m ← $src"
      rc=0; nfiles=0
      while IFS='	' read -r s name file size sha url <&3; do
        [ "$s" = "$src" ] && [ "$name" = "$m" ] || continue
        nfiles=$((nfiles + 1))
        say "  ${file}（$((size / 1048576)) MB）"
        download_verified "$url" "$d/$file" "$size" "$sha" || { rc=1; break; }
      done 3< "$MODELS_TSV"
      if [ $rc -eq 0 ] && [ $nfiles -gt 0 ]; then
        echo "$src:$manifest_hash" > "$d/.ok" || die "无法保存模型状态：$d"
        ok=1; break
      fi
      say "  从 $src 下载 $m 没成功，换下一个来源"
    done
    [ $ok = 1 ] || die "模型 $m 下载失败（魔搭和 Hugging Face 都没成功）。请检查网络后再双击「启动.command」重试。"
  done
  say "✓ 模型就绪：$MODEL_DIR"
}

original_model_verified() { # $1=模型名，$2=清单来源
  local s name file size sha url nfiles=0
  while IFS='	' read -r s name file size sha url; do
    [ "$s" = "$2" ] && [ "$name" = "$1" ] || continue
    nfiles=$((nfiles + 1))
    [ "$(size_of "$MODEL_DIR/$1/$file")" = "$size" ] || return 1
    [ "$(sha256_of "$MODEL_DIR/$1/$file")" = "$sha" ] || return 1
  done < "$MODELS_TSV"
  [ "$nfiles" -gt 0 ]
}

prepare_user_dir() {
  mkdir -p "$INBOX_DIR" "$OUT_DIR" "$VP_DIR" "$LOG_DIR" || die "无法创建 $USER_DIR"
  [ -f "$VP_DIR/说明.txt" ] || cat > "$VP_DIR/说明.txt" <<'TXT'
这里放「声纹样本」，转写稿里就会直接写名字，而不是“说话人1、说话人2”。

· 每人一段 15–60 秒的录音，只有他一个人在说话，尽量安静。
· 文件名就是名字，例如：我.m4a、张三.wav
· 一个人也可以建一个文件夹放多段：李四/1.wav、李四/2.m4a
· 最方便：用 QuickTime 或剪映从以前的录音里截一段他单独说话的部分。
· 放好后重新转写即可（已转写过的文件，在启动菜单里选“重新出结果”或删掉对应结果文件夹）。
TXT
  [ -f "$HW_FILE" ] || printf '%s\n' "# 热词：一行一个（公司名、产品名、同事名）。实验功能，目前测试未见明显效果。" > "$HW_FILE"
}

selftest() {
  step "6/6 自检（加载模型并试转一段测试音频，约 1–3 分钟）"
  local fingerprint
  fingerprint="$APP_VERSION:$(cat "$MODELS_TSV" "$LOCK_FILE" "$BUILD_LOCK_FILE" | shasum -a 256 | awk '{print $1}')"
  if [ -f "$SELFTEST_MARK" ] && [ "$(cat "$SELFTEST_MARK")" = "$fingerprint" ] && [ "$1" != "force" ]; then
    say "✓ 之前已通过，跳过"; return
  fi
  TX_MODEL_DIR="$MODEL_DIR" "$VPY" "$APP_COPY/selftest.py" --engine original --model-dir "$MODEL_DIR" --tx "$APP_COPY/tx.py" \
    || die "自检没通过，请把日志发给提供者。"
  echo "$fingerprint" > "$SELFTEST_MARK" || die "无法保存自检状态，请检查磁盘空间。"
}

main() {
  if [ "$ENGINE" = light ]; then
    if ! light_prerequisites_ready; then
      echo "轻量首次准备需要公共 Python 和固定源模型，先准备原版资源，再继续轻量安装。"
      "${BASH:-/bin/bash}" "$PKG_DIR/install.sh" --engine original || return $?
    fi
    "${BASH:-/bin/bash}" "$PKG_DIR/install-light.sh" "$@"
    return $?
  fi
  local recheck="" arg
  for arg in "$@"; do [ "$arg" = --recheck ] && recheck=force; done
  trap cleanup EXIT
  trap on_int INT TERM
  start_log "安装"
  echo "录音转写工具 v$APP_VERSION —— 安装"
  echo "程序会装到：$APP_DIR"
  echo "你的录音/结果在：$USER_DIR"
  echo "不需要管理员密码，不安装 Homebrew，不改动系统。需要联网下载约 2.4 GB（之后转写不联网）。"
  check_platform
  check_disk
  acquire_lock
  rm -f "$APP_DIR/installed.ok"
  copy_program
  prepare_user_dir
  install_python
  install_deps
  install_models
  selftest "$recheck"
  echo "$APP_VERSION" > "$APP_DIR/installed.ok" || die "无法保存安装状态，请检查磁盘空间。"
  echo
  echo "================ 安装完成 ================"
  echo "以后使用：双击「启动.command」"
  echo "放录音：${INBOX_DIR}（自动模式会盯这里）"
  echo "看结果：$OUT_DIR"
  echo "放声纹：${VP_DIR}（每人 15–60 秒单人录音，文件名=名字）"
  echo "日志：$LOG_DIR"
  echo "本次安装日志：$LOG_FILE"
}

light_prerequisites_ready() {
  [ -x "$PY_HOME/bin/python3" ] || return 1
  local file
  for file in asr/model.pt vad/model.pt spk/campplus_cn_common.bin asr/config.yaml vad/config.yaml spk/config.yaml; do
    [ -s "$ORIGINAL_MODEL_DIR/$file" ] || return 1
  done
}

main "$@"
