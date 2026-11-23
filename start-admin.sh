#!/usr/bin/env bash
# 启动本机应用：桌面窗口、审核工具 http://127.0.0.1:8765 、管理控制台 http://127.0.0.1:8780 。
# 本目录没有 .venv，解释器在旁边的主仓库里。
set -euo pipefail

cd "$(dirname "$0")"
ROOT="$(pwd)"
PYTHON="${ROOT}/../ecommerce-video-compliance/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "未找到 ${PYTHON}" >&2
  exit 1
fi

exec "$PYTHON" -u "${ROOT}/run.py"
