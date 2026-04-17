#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-stock-ai}"
WORKER_NAME="${WORKER_NAME:-${APP_NAME}-worker}"
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
INSTALL_NGINX="${INSTALL_NGINX:-1}"
NGINX_SERVER_NAME="${NGINX_SERVER_NAME:-_}"

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
    run_as_root apt-get install -y python3 python3-venv python3-pip rsync curl
    if [ "$INSTALL_MYSQL" = "1" ]; then
      run_as_root apt-get install -y mysql-server
    fi
    if [ "$INSTALL_NGINX" = "1" ]; then
      run_as_root apt-get install -y nginx
    fi
    return
  fi

  run_as_root yum install -y python3 python3-pip rsync curl
  if [ "$INSTALL_MYSQL" = "1" ]; then
    run_as_root yum install -y mysql-server
  fi
  if [ "$INSTALL_NGINX" = "1" ]; then
    run_as_root yum install -y nginx
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

  run_as_root mysql -uroot <<SQL
CREATE DATABASE IF NOT EXISTS \`${MYSQL_DATABASE}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${MYSQL_APP_USER}'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
ALTER USER '${MYSQL_APP_USER}'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_APP_USER}'@'%';
FLUSH PRIVILEGES;
SQL

  if [ -n "$MYSQL_ROOT_PASSWORD" ]; then
    run_as_root mysql -uroot <<SQL
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

  for optional_var in \
    MINIMAX_API_KEY \
    MINIMAX_API_BASE \
    MINIMAX_MODEL \
    LLM_API_KEY \
    LLM_API_BASE \
    LLM_MODEL \
    OPENAI_API_KEY \
    OPENAI_BASE_URL \
    OPENAI_API_BASE \
    OPENAI_MODEL \
    SCREENING_MAX_WORKERS \
    SCREENING_SUBMIT_BATCH \
    SCREENING_SAVE_INTERVAL \
    WEB_CONCURRENCY \
    NGINX_SERVER_NAME \
    STOCK_INFO_TTL \
    KLINE_TTL \
    SEARCH_TTL; do
    optional_value="${!optional_var:-}"
    if [ -n "$optional_value" ]; then
      printf '%s=%s\n' "$optional_var" "$optional_value" | run_as_root tee -a "$ENV_FILE" >/dev/null
    fi
  done

  run_as_root chmod 600 "$ENV_FILE"
}

write_systemd_units() {
  local mysql_service
  mysql_service="$(detect_mysql_service)"

  run_as_root tee "/etc/systemd/system/${APP_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Stock AI web service
After=network.target ${mysql_service}.service

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

  run_as_root tee "/etc/systemd/system/${WORKER_NAME}.service" >/dev/null <<EOF
[Unit]
Description=Stock AI screening worker
After=network.target ${mysql_service}.service

[Service]
Type=simple
User=${APP_USER}
WorkingDirectory=${APP_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${VENV_DIR}/bin/python ${APP_DIR}/worker_main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
}

write_nginx_config() {
  local pkg_manager
  local nginx_conf
  pkg_manager="$(detect_package_manager)"

  run_as_root mkdir -p "/var/cache/nginx/${APP_NAME}"

  if [ "$pkg_manager" = "apt" ]; then
    nginx_conf="/etc/nginx/sites-available/${APP_NAME}.conf"
  else
    nginx_conf="/etc/nginx/conf.d/${APP_NAME}.conf"
  fi

  run_as_root tee "$nginx_conf" >/dev/null <<EOF
proxy_cache_path /var/cache/nginx/${APP_NAME} levels=1:2 keys_zone=${APP_NAME}_cache:20m max_size=256m inactive=30m use_temp_path=off;
limit_req_zone \$binary_remote_addr zone=${APP_NAME}_rate:10m rate=20r/s;

server {
    listen 80 default_server;
    server_name ${NGINX_SERVER_NAME};

    server_tokens off;
    client_max_body_size 10m;
    keepalive_timeout 30s;
    send_timeout 30s;

    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;
    gzip_min_length 1024;

    add_header X-Frame-Options SAMEORIGIN;
    add_header X-Content-Type-Options nosniff;
    add_header Referrer-Policy strict-origin-when-cross-origin;

    limit_req zone=${APP_NAME}_rate burst=40 nodelay;

    location ~ /\.(?!well-known) {
        deny all;
        access_log off;
        log_not_found off;
    }

    location /static/ {
        alias ${APP_DIR}/static/;
        access_log off;
        expires 7d;
        add_header Cache-Control "public, max-age=604800, immutable";
    }

    location = / {
        proxy_cache ${APP_NAME}_cache;
        proxy_cache_valid 200 10s;
        proxy_cache_methods GET HEAD;
        proxy_cache_use_stale error timeout invalid_header updating http_500 http_502 http_503 http_504;
        proxy_cache_background_update on;
        proxy_cache_lock on;
        add_header X-Proxy-Cache \$upstream_cache_status always;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_connect_timeout 5s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
        proxy_pass http://127.0.0.1:8000;
    }

    location = /strategies {
        proxy_cache ${APP_NAME}_cache;
        proxy_cache_valid 200 10s;
        proxy_cache_methods GET HEAD;
        proxy_cache_use_stale error timeout invalid_header updating http_500 http_502 http_503 http_504;
        proxy_cache_background_update on;
        proxy_cache_lock on;
        add_header X-Proxy-Cache \$upstream_cache_status always;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_connect_timeout 5s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
        proxy_pass http://127.0.0.1:8000;
    }

    location ~ ^/api/(stock|search|quote)/ {
        proxy_cache ${APP_NAME}_cache;
        proxy_cache_valid 200 5s;
        proxy_cache_methods GET HEAD;
        proxy_cache_use_stale error timeout invalid_header updating http_500 http_502 http_503 http_504;
        proxy_cache_background_update on;
        proxy_cache_lock on;
        add_header X-Proxy-Cache \$upstream_cache_status always;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_connect_timeout 5s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
        proxy_pass http://127.0.0.1:8000;
    }

    location / {
        add_header X-Proxy-Cache BYPASS always;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_http_version 1.1;
        proxy_connect_timeout 5s;
        proxy_send_timeout 30s;
        proxy_read_timeout 30s;
        proxy_pass http://127.0.0.1:8000;
    }
}
EOF

  if [ "$pkg_manager" = "apt" ]; then
    run_as_root rm -f /etc/nginx/sites-enabled/default
    run_as_root ln -sfn "$nginx_conf" "/etc/nginx/sites-enabled/${APP_NAME}.conf"
  fi
}

start_services() {
  run_as_root systemctl daemon-reload
  run_as_root systemctl enable "${APP_NAME}"
  run_as_root systemctl enable "${WORKER_NAME}"
  run_as_root systemctl restart "${APP_NAME}"
  run_as_root systemctl restart "${WORKER_NAME}"
  run_as_root systemctl --no-pager --full status "${APP_NAME}"
  run_as_root systemctl --no-pager --full status "${WORKER_NAME}"
}

start_nginx() {
  run_as_root nginx -t
  if command -v ufw >/dev/null 2>&1; then
    run_as_root ufw allow 80/tcp >/dev/null 2>&1 || true
  fi
  run_as_root systemctl enable nginx
  run_as_root systemctl restart nginx
  run_as_root systemctl --no-pager --full status nginx
}

ensure_nginx_static_access() {
  local app_parent
  app_parent="$(dirname "$APP_DIR")"

  run_as_root chmod 755 "$app_parent" || true
  run_as_root chmod 755 "$APP_DIR" || true
  if [ -d "$APP_DIR/static" ]; then
    run_as_root find "$APP_DIR/static" -type d -exec chmod 755 {} +
    run_as_root find "$APP_DIR/static" -type f -exec chmod 644 {} +
  fi
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

  log "Writing systemd units"
  write_systemd_units

  log "Starting services"
  start_services

  if [ "$INSTALL_NGINX" = "1" ]; then
    log "Writing Nginx config"
    write_nginx_config

    log "Preparing static file access"
    ensure_nginx_static_access

    log "Starting Nginx"
    start_nginx
  fi

  echo
  echo "Web service: ${APP_NAME}"
  echo "Worker service: ${WORKER_NAME}"
  if [ "$INSTALL_NGINX" = "1" ]; then
    echo "Nginx: enabled on port 80"
  fi
  echo "App dir: ${APP_DIR}"
  echo "Env file: ${ENV_FILE}"
  echo "Database URL: mysql://${MYSQL_APP_USER}:******@${MYSQL_HOST}:${MYSQL_PORT}/${MYSQL_DATABASE}?charset=utf8mb4"
}

main "$@"
