#!/bin/bash
# 轻量试用安装：只写 venv-light/models-light，不重建原版 Python 环境和源模型。
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"
select_engine light || exit 2

LOCK_DIR="$APP_DIR/.install.lock"
LOCK_OWNED=0
LOCAL_MANIFEST="$LIGHT_MODEL_DIR/models-light.manifest.json"

cleanup_light() {
  if [ "$LOCK_OWNED" = 1 ] && [ "$(cat "$LOCK_DIR/pid" 2>/dev/null)" = "$$" ]; then
    rm -rf "$LOCK_DIR" 2>/dev/null
  fi
  LOCK_OWNED=0
}

acquire_light_lock() {
  mkdir -p "$APP_DIR" || die "无法创建程序目录：$APP_DIR"
  if ! mkdir "$LOCK_DIR" 2>/dev/null; then
    local pid; pid="$(cat "$LOCK_DIR/pid" 2>/dev/null)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      die "另一个安装窗口正在运行（进程 ${pid}），请等它结束后再试。"
    fi
    rm -rf "$LOCK_DIR"
    mkdir "$LOCK_DIR" || die "无法创建安装锁：$LOCK_DIR"
  fi
  echo $$ > "$LOCK_DIR/pid" || die "无法写入安装锁：$LOCK_DIR"
  LOCK_OWNED=1
}

copy_light_program() {
  rm -rf "$APP_COPY.tmp"
  cp -R "$PKG_DIR" "$APP_COPY.tmp" || die "复制程序文件失败"
  rm -rf "$APP_COPY"
  mv "$APP_COPY.tmp" "$APP_COPY" || die "复制程序文件失败"
}

install_light_deps() {
  local fingerprint
  fingerprint="$(light_deps_fingerprint)"
  if [ "$(cat "$LIGHT_VENV/.ok" 2>/dev/null)" = "$fingerprint" ] \
     && "$VPY" -c 'import numpy, scipy, soundfile, torch, funasr, funasr_onnx, onnx, onnxruntime, imageio_ffmpeg' 2>/dev/null; then
    say "✓ 轻量依赖清单一致，已就绪"
    return
  fi
  # 环境原地按固定清单安装；无论成功或失败都不删除原版 venv/models。
  if [ ! -x "$VPY" ]; then
    "$PY_HOME/bin/python3" -m venv "$LIGHT_VENV" || die "创建轻量独立环境失败"
  fi
  local pip_args=()
  [ -n "$TX_PIP_INDEX_URL" ] && pip_args+=(--index-url "$TX_PIP_INDEX_URL")
  # Python 3.12 新建环境不自带 setuptools/wheel；先安装已锁定的构建工具。
  "$VPY" -m pip install --cache-dir "$APP_DIR/cache/light-pip" --require-hashes --no-deps \
    "${pip_args[@]}" -r "$BUILD_LOCK_FILE" || die "轻量环境构建工具安装失败"
  "$VPY" -m pip install --cache-dir "$APP_DIR/cache/light-pip" --require-hashes --no-deps \
    --no-build-isolation "${pip_args[@]}" -r "$LIGHT_LOCK_FILE" \
    || die "轻量依赖安装失败。原版仍可使用，再运行安装脚本 --engine light 可重试。"
  "$VPY" -c 'import numpy, scipy, soundfile, torch, funasr, funasr_onnx, onnx, onnxruntime, imageio_ffmpeg' \
    || die "轻量依赖装完但无法导入"
  echo "$fingerprint" > "$LIGHT_VENV/.ok" || die "无法保存轻量依赖状态，请检查磁盘空间。"
}

verify_light_install() {
  "$VPY" "$PKG_DIR/selftest.py" --engine light --model-dir "$LIGHT_MODEL_DIR" \
    --manifest "$LIGHT_MANIFEST" --verify-models-only
}

check_light_disk() {
  local need=512
  [ -x "$VPY" ] && [ "$(cat "$LIGHT_VENV/.ok" 2>/dev/null)" = "$(light_deps_fingerprint)" ] \
    || need=$((need + 2000))
  # 包括小标点源权重、FP32 导出中间图、量化文件和导出余量。
  verify_light_install >/dev/null 2>&1 || need=$((need + 3500))
  require_space_mb "$APP_DIR" "$need" "轻量安装和模型导出"
}

main_light() {
  trap cleanup_light EXIT
  trap 'echo; echo "轻量安装已中断，原版仍可使用。"; exit 130' INT TERM
  start_log "安装_轻量"
  echo "录音转写工具 v$APP_VERSION —— 轻量版安装"
  echo "独立环境：$LIGHT_VENV"
  echo "独立模型：$LIGHT_MODEL_DIR"
  echo "原版保留在：$ORIGINAL_VENV"

  [ "$(uname -s)" = Darwin ] || die "轻量安装包仅支持 macOS。"
  [ "$(uname -m)" = arm64 ] || [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = 1 ] \
    || die "轻量安装包需要 Apple 芯片的 Mac。"
  [ -x "$PY_HOME/bin/python3" ] || die "缺少公共 Python 环境，请先安装原版，再选择轻量试用。"
  [ -s "$LIGHT_LOCK_FILE" ] || die "安装包缺少固定版本的轻量依赖清单：$LIGHT_LOCK_FILE"
  [ -s "$BUILD_LOCK_FILE" ] || die "安装包缺少固定版本的构建工具清单：$BUILD_LOCK_FILE"
  [ -s "$LIGHT_MANIFEST" ] || die "安装包缺少可信轻量模型清单：$LIGHT_MANIFEST"
  [ -f "$PKG_DIR/export_light_models.py" ] || die "安装包缺少轻量模型导出程序"
  check_light_disk
  acquire_light_lock
  rm -f "$INSTALLED_MARK"
  mkdir -p "$LIGHT_MODEL_DIR" "$INBOX_DIR" "$OUT_DIR" "$VP_DIR" "$LOG_DIR" \
    || die "无法创建轻量模型或用户目录"

  step "1/3 准备轻量独立依赖"
  install_light_deps
  step "2/3 检查或导出轻量模型"
  if verify_light_install >/dev/null 2>&1; then
    say "✓ 本地模型与来源指纹校验通过，跳过导出"
  else
    for m in asr vad spk; do
      [ -f "$ORIGINAL_MODEL_DIR/$m/config.yaml" ] \
        || die "缺少可用的原版 $m 源权重，请先完成原版安装。"
    done
    say "从已有源权重导出到 models-light；首次可能需要下载独立的小标点源模型。"
    "$VPY" "$PKG_DIR/export_light_models.py" --source-model-dir "$ORIGINAL_MODEL_DIR" \
      --output-dir "$LIGHT_MODEL_DIR" --manifest "$LOCAL_MANIFEST" \
      || die "轻量模型导出失败。原版仍可使用，可重试轻量安装。"
    verify_light_install || die "导出产物的来源或文件校验未通过"
  fi
  copy_light_program

  step "3/3 轻量转写自检"
  "$VPY" "$APP_COPY/selftest.py" --engine light --model-dir "$LIGHT_MODEL_DIR" \
    --manifest "$LIGHT_MANIFEST" --tx "$APP_COPY/tx.py" \
    || die "轻量自检未通过，原版仍可使用。"
  printf '%s:%s\n' "$APP_VERSION" "$(sha256_of "$LOCAL_MANIFEST")" > "$SELFTEST_MARK" \
    || die "轻量自检完成，但无法保存自检状态，请检查磁盘空间后重试。"
  echo "$APP_VERSION" > "$INSTALLED_MARK" || die "无法保存轻量安装状态，请检查磁盘空间后重试。"
  echo
  echo "轻量版安装完成。直接启动默认轻量版；菜单按 9 可切回原版。"
  echo "轻量结果：$OUT_DIR/轻量试用"
  echo "本次日志：$LOG_FILE"
}

main_light "$@"
