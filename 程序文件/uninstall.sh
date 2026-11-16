#!/bin/bash
# 卸载：只删除程序环境（Python、依赖、模型、程序副本），绝不删除你的录音、转写结果、声纹样本和日志。
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/common.sh"

echo "将删除程序环境：$APP_DIR"
echo "不会删除（请自行保留或手动删）：${USER_DIR}（录音、转写结果、声纹样本、日志）"
if [ ! -d "$APP_DIR" ]; then echo "程序环境不存在，无需卸载。"; exit 0; fi
# 安全检查：只允许删除名字正好是“录音转写工具”的目录，且不能是用户目录本身或其上级
case "$APP_DIR" in
  */录音转写工具) ;;
  *) echo "程序目录名称异常，拒绝删除：$APP_DIR"; exit 1 ;;
esac
case "$USER_DIR/" in "$APP_DIR"/*) echo "用户数据目录在程序目录里面，拒绝删除：$APP_DIR"; exit 1 ;; esac
if [ "$1" != "--yes" ]; then
  read -r -p "确认卸载请输入 y 后回车：" a
  [ "$a" = "y" ] || [ "$a" = "Y" ] || { echo "已取消。"; exit 0; }
fi
rm -rf "$APP_DIR" && echo "✓ 已卸载。你的文件仍在：$USER_DIR"
