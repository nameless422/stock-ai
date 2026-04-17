#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

cd "$BASE_DIR"

if [ -z "${STOCK_AI_DB_URL:-}" ]; then
  echo "Missing STOCK_AI_DB_URL"
  echo "Example:"
  echo "export STOCK_AI_DB_URL='mysql://user:password@127.0.0.1:3306/stock_ai?charset=utf8mb4'"
  exit 1
fi

echo "Starting stock-ai on http://127.0.0.1:8000"
python3 worker_main.py &
worker_pid=$!
python3 main.py &
web_pid=$!

cleanup() {
  kill "$web_pid" >/dev/null 2>&1 || true
  kill "$worker_pid" >/dev/null 2>&1 || true
}

trap cleanup EXIT INT TERM

wait "$web_pid"
