#!/usr/bin/env bash
# 启动 Kronos 推理服务（只监听回环，不对外）。
#
# 环境变量：
#   KRONOS_VARIANT    mini | small        （默认 mini）
#   KRONOS_DEVICE     cpu | cuda          （默认 cpu；本机 GPU 只有 2GB，见 README）
#   KRONOS_PORT       端口                （默认 8710）
#   KRONOS_THREADS    torch 线程数        （默认 8，本机 20 核但我们内存吃紧）
#   KRONOS_TIMEOUT_S  单次推理超时秒      （默认 120）
set -euo pipefail
cd "$(dirname "$0")"

export HF_HOME="${HF_HOME:-$PWD/data/hf}"
export TOKENIZERS_PARALLELISM=false
mkdir -p "$HF_HOME" data

PORT="${KRONOS_PORT:-8710}"
HOST="${KRONOS_HOST:-127.0.0.1}"

exec .venv/bin/python -m uvicorn app.main:app \
  --host "$HOST" --port "$PORT" \
  --workers 1 \
  --timeout-keep-alive 75 \
  --log-level info
