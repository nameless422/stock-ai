#!/usr/bin/env bash
set -euo pipefail

APP_NAME="stock-ai"
APP_USER="${APP_USER:-$(id -un)}"
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
PORT="${PORT:-8000}"
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_DATABASE="${MYSQL_DATABASE:-stock_ai}"
MYSQL_APP_USER="${MYSQL_APP_USER:-stock_ai}"
MYSQL_APP_PASSWORD="${MYSQL_APP_PASSWORD:-StockAI_123456}"
INSTALL_MYSQL="${INSTALL_MYSQL:-1}"
SUDO=""

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

echo "[1/5] Preparing directories"
mkdir -p "$APP_DIR"

echo "[2/5] Installing Python venv tooling if needed"
if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update
  $SUDO apt-get install -y python3 python3-venv python3-pip
elif command -v yum >/dev/null 2>&1; then
  $SUDO yum install -y python3 python3-pip
fi

echo "[3/5] Creating virtual environment"
"$PYTHON_BIN" -m venv "$VENV_DIR"

echo "[4/5] Installing dependencies"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"

if [ "$INSTALL_MYSQL" = "1" ]; then
  echo "[5/6] Installing MySQL"
  chmod +x "$APP_DIR/deploy/mysql/install_mysql_server.sh"
  MYSQL_DATABASE="$MYSQL_DATABASE" \
  MYSQL_APP_USER="$MYSQL_APP_USER" \
  MYSQL_APP_PASSWORD="$MYSQL_APP_PASSWORD" \
  MYSQL_BIND_ADDRESS="$MYSQL_HOST" \
  SUDO="$SUDO" \
  "$APP_DIR/deploy/mysql/install_mysql_server.sh"
fi

echo "[6/6] Writing systemd unit"
$SUDO tee /etc/systemd/system/${APP_NAME}.service >/dev/null <<EOF
[Unit]
Description=Stock AI FastAPI service
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=STOCK_AI_DB_URL=mysql://${MYSQL_APP_USER}:${MYSQL_APP_PASSWORD}@${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DATABASE}?charset=utf8mb4
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

$SUDO systemctl daemon-reload
$SUDO systemctl enable "${APP_NAME}"
$SUDO systemctl restart "${APP_NAME}"
$SUDO systemctl --no-pager --full status "${APP_NAME}"

echo
echo "Service is running on port ${PORT}. If needed, open the port in Tencent Cloud security group."
echo "Database URL: mysql://${MYSQL_APP_USER}:******@${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DATABASE}?charset=utf8mb4"
