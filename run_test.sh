#!/usr/bin/env bash
# 从任意工作目录运行统一检查。
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
python_bin="${TEST_PYTHON:-$PWD/.venv/bin/python}"
if [[ ! -x "$python_bin" ]]; then
  printf 'FAIL [Python 环境] 未找到 %s\n请先按 README 创建 .venv 并安装依赖。\n' "$python_bin" >&2
  exit 2
fi
exec "$python_bin" -u tests/run_suite.py "$@"
