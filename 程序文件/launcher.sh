#!/bin/bash
# 录音转写工具 Mac 版 启动菜单（由「启动.command」调用）。
# 也可以带参数直接用：launcher.sh files 文件1 文件2… | folder 文件夹 | watch | redo 文件…
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

export TX_MODEL_DIR="$MODEL_DIR" HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 TQDM_DISABLE=1

open_dir() { mkdir -p "$1"; if command -v open >/dev/null 2>&1; then open "$1"; else echo "位置：$1"; fi; }

# 用 caffeinate 防止长时间转写时 Mac 睡眠（系统自带，结束后自动恢复）
keep_awake() { if command -v caffeinate >/dev/null 2>&1; then caffeinate -i "$@"; else "$@"; fi; }

show_batch_export() { # 只读本次日志，不能把上一批总稿当成本次结果。
  local line merged="" partial=""
  while IFS= read -r line; do
    case "$line" in
      *"合并全文："*) merged="${line#*合并全文：}" ;;
      *"部分结果："*) partial="${line#*部分结果：}" ;;
    esac
  done < "$1"
  if [ -n "$merged" ] && [ -f "$merged" ]; then
    echo "本次合并全文：$merged"
  fi
  if [ -n "$partial" ] && [ -f "$partial" ]; then
    echo "本次部分结果（仍有录音未完成）：$partial"
  fi
}

run_tx() { # $1=run|watch，其余=传给 tx.py 的参数
  local log rc log_rc statuses=()
  if [ "$ENGINE" = light ]; then
    "$VPY" "$APP_COPY/selftest.py" --engine light --model-dir "$MODEL_DIR" \
      --manifest "$LIGHT_MANIFEST" --verify-models-only || return 3
  fi
  mkdir -p "$LOG_DIR" "$OUT_DIR" || { echo "【日志保存失败】无法创建日志或结果目录。"; return 74; }
  # 同秒重跑或多个窗口也使用独立日志，避免带出上一批合并文件的位置。
  log="$(mktemp "$LOG_DIR/转写_${ENGINE}_$(date +%Y%m%d-%H%M%S).log.XXXXXX")" \
    || { echo "【日志保存失败】无法创建本次日志：$LOG_DIR"; return 74; }
  echo "日志：$log"
  echo "结果：$OUT_DIR"
  echo "引擎：$ENGINE_LABEL"
  trap 'echo' INT   # Ctrl+C 只停止转写，回到菜单
  keep_awake "$VPY" "$APP_COPY/tx.py" "$@" -o "$OUT_DIR" --voiceprints "$VP_DIR" --hotwords "$HW_FILE" \
    --engine "$ENGINE" --model-dir "$MODEL_DIR" 2>&1 | tee -a "$log"
  statuses=("${PIPESTATUS[@]}")
  rc=${statuses[0]}; log_rc=${statuses[1]}
  trap - INT
  echo
  if [ "$log_rc" -ne 0 ]; then
    echo "【日志保存失败】日志可能不完整，请检查磁盘空间和目录权限：$log"
    if [ "$rc" -eq 0 ]; then
      echo "转写处理已结束，结果保留在：$OUT_DIR"
      return 74
    fi
  fi
  if [ "$rc" -eq 0 ]; then
    if [ "$ENGINE" = light ]; then
      echo "✓ 完成。轻量结果在「$OUT_DIR/轻量试用/录音名/」，先看里面的 目录.md"
    else
      echo "✓ 完成。每个录音的结果在「$OUT_DIR/录音名/」，先看里面的 目录.md"
    fi
  elif [ "$rc" -eq 130 ]; then
    echo "已停止。下次再转同一个文件会从断点继续。"
  else
    echo "【有文件没转成功】退出码 ${rc}。再转一次会从断点继续；仍失败请把日志发给提供者：$log"
  fi
  show_batch_export "$log"
  return "$rc"
}

# 弹出访达选择窗口；结果每行一个路径，写到标准输出。返回 0=选好 1=取消 2=弹窗不可用
choose() { # $1 = file|folder
  local script err out
  if [ "$1" = file ]; then
    script='set fs to choose file with prompt "选择要转写的录音（可按住 ⌘ 多选）" with multiple selections allowed
set out to ""
repeat with f in fs
set out to out & POSIX path of f & linefeed
end repeat
return out'
  else
    script='return POSIX path of (choose folder with prompt "选择放录音的文件夹（会转写里面所有录音）")'
  fi
  command -v osascript >/dev/null 2>&1 || return 2
  err="$(mktemp)"
  out="$(osascript -e "$script" 2>"$err")"
  if [ $? -ne 0 ]; then
    if grep -q -- "-128" "$err"; then rm -f "$err"; return 1; fi
    rm -f "$err"; return 2
  fi
  rm -f "$err"
  printf '%s\n' "$out"
  return 0
}

# 弹窗不可用时：让用户把文件拖进终端窗口（终端会自动加反斜杠转义），用 Python 的 shlex 正确拆分
ask_drag() {
  local line
  echo "请把录音文件（或文件夹）拖进这个窗口，然后按回车：" >&2
  read -r line || return 1
  [ -n "$line" ] || return 1
  "$VPY" -c 'import shlex,sys; print("\n".join(shlex.split(sys.argv[1])))' "$line"
}

collect_and_run() { # $1 = file|folder，其余为 run 参数（如 --force）
  local picked rc paths=() p
  picked="$(choose "$1")"; rc=$?
  shift
  if [ $rc -eq 1 ]; then echo "已取消。"; return 0; fi
  if [ $rc -eq 2 ]; then picked="$(ask_drag)" || { echo "没有收到文件。"; return 0; }; fi
  while IFS= read -r p; do [ -n "$p" ] && paths+=("$p"); done <<LIST
$picked
LIST
  if [ ${#paths[@]} -eq 0 ]; then echo "没有选中文件。"; return 0; fi
  echo "将转写："; for p in "${paths[@]}"; do echo "  $p"; done
  run_tx run "${paths[@]}" "$@"
}

switch_engine() {
  local wanted=light previous="$ENGINE" rc
  [ "$ENGINE" = light ] && wanted=original
  select_engine "$wanted" || return $?
  if ! is_installed; then
    echo "正在准备${ENGINE_LABEL}环境……"
    "${BASH:-/bin/bash}" "$PKG_DIR/install.sh" --engine "$ENGINE"; rc=$?
    if [ "$rc" -ne 0 ]; then
      select_engine "$previous"
      echo "未切换成功，继续使用${ENGINE_LABEL}。"
      return "$rc"
    fi
  fi
  echo "已切换为${ENGINE_LABEL}，仅当前窗口生效；下次直接启动默认轻量版。"
}

menu() {
  local c
  while true; do
    echo
    echo "========== 录音转写工具 v$APP_VERSION =========="
    echo " 当前引擎：${ENGINE_LABEL}（原版保留，可随时切换）"
    echo " 默认每段约 15 分钟，多文件共用最多 4 个转写进程，每份结果按原时间顺序合并"
    echo " 1) 选择录音文件开始转写（可多选）"
    echo " 2) 选择一个文件夹，转写里面所有录音"
    echo " 3) 自动模式：盯住「待转写录音」文件夹，放进去就自动转（关窗口即停止）"
    echo " 4) 打开「转写结果」文件夹"
    echo " 5) 打开「声纹样本」文件夹（每人 15–60 秒单人录音，文件名=名字）"
    echo " 6) 打开「待转写录音」文件夹"
    echo " 7) 打开日志文件夹"
    echo " 8) 换了声纹样本后，重新出某些录音的结果（不用重新识别，很快）"
    if [ "$ENGINE" = light ]; then
      echo " 9) 切回原版转写"
    else
      echo " 9) 切换到轻量版（首次自动准备独立环境和模型）"
    fi
    echo " 0) 退出"
    read -r -p "请输入数字后按回车：" c || return 0
    case "$c" in
      1) collect_and_run file ;;
      2) collect_and_run folder ;;
      3) echo "正在盯：${INBOX_DIR}（每分钟检查一次；把录音拷进去，拷完才会开始）"
         open_dir "$INBOX_DIR"
         run_tx watch "$INBOX_DIR" ;;
      4) open_dir "$OUT_DIR" ;;
      5) open_dir "$VP_DIR" ;;
      6) open_dir "$INBOX_DIR" ;;
      7) open_dir "$LOG_DIR" ;;
      8) collect_and_run file --force ;;
      9) switch_engine ;;
      0|q|Q) return 0 ;;
      *) echo "没看懂，请输入 0–9 的数字。" ;;
    esac
  done
}

if ! is_installed; then
  echo "${ENGINE_LABEL}还没有安装完成（或安装包版本已更新）。请先运行安装脚本 --engine ${ENGINE}。"
  exit 3
fi
# 全局 --engine 可放在命令前后；保留其余参数的引号与空格。
_tx_args=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --engine) shift 2 ;;
    --engine=*) shift ;;
    *) _tx_args+=("$1"); shift ;;
  esac
done
set -- "${_tx_args[@]}"
unset _tx_args
mkdir -p "$INBOX_DIR" "$OUT_DIR" "$VP_DIR" "$LOG_DIR"
case "$1" in
  files) shift; run_tx run "$@" ;;
  folder) shift; run_tx run "$@" ;;
  redo) shift; run_tx run "$@" --force ;;
  watch) shift; run_tx watch "${@:-$INBOX_DIR}" ;;
  "") menu ;;
  *) echo "未知参数：$1"; exit 2 ;;
esac
