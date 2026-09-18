#!/usr/bin/env bash
# 情绪晴雨表 Demo 启动脚本。在本目录执行: ./run.sh
# 页面入口: http://127.0.0.1:8321  （K3 本机 Chromium 打开）
set -euo pipefail
cd "$(dirname "$0")"
exec .venv/bin/python -u server/app.py --host 127.0.0.1 --port 8321
