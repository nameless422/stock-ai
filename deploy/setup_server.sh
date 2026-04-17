#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-stock-ai}"
APP_USER="${APP_USER:-$(id -un)}"
APP_DIR="${APP_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
VENV_DIR="${VENV_DIR:-$APP_DIR/.venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
ENV_DIR="${ENV_DIR:-/etc/${APP_NAME}}"
ENV_FILE="${ENV_FILE:-${ENV_DIR}/${APP_NAME}.env}"

MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-3306}"
MYSQL_DATABASE="${MYSQL_DATABASE:-stock_ai}"
MYSQL_APP_USER="${MYSQL_APP_USER:-stock_ai}"
MYSQL_APP_PASSWORD="${MYSQL_APP_PASSWORD:-StockAI_123456}"
MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-}"
INSTALL_MYSQL="${INSTALL_MYSQL:-1}"

SUDO=""

if [ "$(id -u)" -ne 0 ]; then
  SUDO="sudo"
fi

log() {
  echo
  echo "==> $1"
}

run_as_root() {
  if [ -n "$SUDO" ]; then
    "$SUDO" "$@"
  else
    "$@"
  fi
}

detect_package_manager() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "apt"
    return
  fi

  if command -v yum >/dev/null 2>&1; then
    echo "yum"
    return
  fi

  echo "Unsupported package manager"
  exit 1
}

install_system_packages() {
  local pkg_manager
  pkg_manager="$(detect_package_manager)"

  if [ "$pkg_manager" = "apt" ]; then
    run_as_root apt-get update
    run_as_root apt-get install -y python3 python3-venv python3-pip
    if [ "$INSTALL_MYSQL" = "1" ]; then
      run_as_root apt-get install -y mysql-server
    fi
    return
  fi

  run_as_root yum install -y python3 python3-pip
  if [ "$INSTALL_MYSQL" = "1" ]; then
    run_as_root yum install -y mysql-server
  fi
}

detect_mysql_service() {
  if run_as_root systemctl list-unit-files mysql.service >/dev/null 2>&1; then
    echo "mysql"
    return
  fi

  if run_as_root systemctl list-unit-files mysqld.service >/dev/null 2>&1; then
    echo "mysqld"
    return
  fi

  echo "mysql"
}

restart_mysql_service() {
  local mysql_service
  mysql_service="$(detect_mysql_service)"

  run_as_root systemctl enable "$mysql_service"
  run_as_root systemctl restart "$mysql_service"
}

configure_mysql_bind_address() {
  local mysql_cnf=""

  for candidate in /etc/mysql/mysql.conf.d/mysqld.cnf /etc/my.cnf; do
    if [ -f "$candidate" ]; then
      mysql_cnf="$candidate"
      break
    fi
  done

  if [ -z "$mysql_cnf" ]; then
    return
  fi

  if grep -Eq '^[[:space:]]*bind-address[[:space:]]*=' "$mysql_cnf"; then
    run_as_root sed -i.bak -E "s/^[[:space:]]*bind-address[[:space:]]*=.*/bind-address = ${MYSQL_HOST}/" "$mysql_cnf"
  else
    printf '\nbind-address = %s\n' "$MYSQL_HOST" | run_as_root tee -a "$mysql_cnf" >/dev/null
  fi
}

initialize_mysql() {
  restart_mysql_service
  configure_mysql_bind_address
  restart_mysql_service

  run_as_root mysql <<SQL
CREATE DATABASE IF NOT EXISTS \`${MYSQL_DATABASE}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${MYSQL_APP_USER}'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
ALTER USER '${MYSQL_APP_USER}'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_APP_USER}'@'%';
FLUSH PRIVILEGES;
SQL

  if [ -n "$MYSQL_ROOT_PASSWORD" ]; then
    run_as_root mysql <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}';
SQL
  fi
}

ensure_virtualenv() {
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
}

install_dependencies() {
  "$VENV_DIR/bin/pip" install --upgrade pip
  "$VENV_DIR/bin/pip" install -r "$APP_DIR/requirements.txt"
}

write_env_file() {
  run_as_root mkdir -p "$ENV_DIR"
  run_as_root tee "$ENV_FILE" >/dev/null <<EOF
PYTHONUNBUFFERED=1
STOCK_AI_DB_URL=mysql://${MYSQL_APP_USER}:${MYSQL_APP_PASSWORD}@${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DATABASE}?charset=utf8mb4
EOF
  run_as_root chmod 600 "$ENV_FILE"
}

write_systemd_unit() {
  run_as_root tee "/etc/systemd/system/${APP_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Stock AI FastAPI service
After=network.target

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

start_service() {
  run_as_root systemctl daemon-reload
  run_as_root systemctl enable "${APP_NAME}"
  run_as_root systemctl restart "${APP_NAME}"
  run_as_root systemctl --no-pager --full status "${APP_NAME}"
}

main() {
  log "Preparing application directory"
  mkdir -p "$APP_DIR"

  log "Installing system packages"
  install_system_packages

  log "Preparing virtual environment"
  ensure_virtualenv

  log "Installing Python dependencies"
  install_dependencies

  if [ "$INSTALL_MYSQL" = "1" ]; then
    log "Installing and initializing MySQL"
    initialize_mysql
  else
    log "Skipping MySQL installation"
  fi

  log "Writing environment file"
  write_env_file

  log "Writing systemd unit"
  write_systemd_unit

  log "Starting service"
  start_service

  echo
  echo "Service name: ${APP_NAME}"
  echo "App dir: ${APP_DIR}"
  echo "Env file: ${ENV_FILE}"
  echo "Database URL: mysql://${MYSQL_APP_USER}:******@${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DATABASE}?charset=utf8mb4"
}

main "$@"
