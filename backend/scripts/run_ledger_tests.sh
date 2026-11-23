#!/usr/bin/env sh
# 离线复验领退料更正单：仅依赖 Python 标准库，-I 忽略环境污染。
set -e
cd "$(dirname "$0")/.."
python3 -I tests/test_ledger_engine.py
python3 -I tests/test_projection_analysis.py
python3 -I tests/test_ledger_service.py
