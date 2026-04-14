#!/usr/bin/env bash
set -euo pipefail

APP_NAME="stock-ai"
APP_USER="${APP_USER:-root}"
APP_DIR="${APP_DIR:-/root/.openclaw/workspace/stock-ai}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
PORT="${PORT:-8000}"

echo "[1/5] Preparing directories"
mkdir -p "$APP_DIR"

echo "[2/5] Installing Python venv tooling if needed"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update
  apt-get install -y python3 python3-venv python3-pip
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip
fi

echo "[3/5] Creating virtual environment"
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "[4/5] Installing dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "[5/5] Writing systemd unit"
cat >/etc/systemd/system/${APP_NAME}.service <<EOF
[Unit]
Description=Stock AI FastAPI service
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=STOCK_AI_DB_PATH=${APP_DIR}/screening.db
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "${APP_NAME}"
systemctl restart "${APP_NAME}"
systemctl --no-pager --full status "${APP_NAME}"

echo
echo "Service is running on port ${PORT}. If needed, open the port in Tencent Cloud security group."
