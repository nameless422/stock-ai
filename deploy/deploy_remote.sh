#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PROJECT_NAME="$(basename "$BASE_DIR")"
ENV_FILE="${ENV_FILE:-$BASE_DIR/.env.remote-secrets}"

REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_USER="${REMOTE_USER:-}"
REMOTE_PORT="${REMOTE_PORT:-22}"
REMOTE_APP_DIR="${REMOTE_APP_DIR:-}"
APP_NAME="${APP_NAME:-stock-ai}"
APP_USER="${APP_USER:-}"
INSTALL_MYSQL="${INSTALL_MYSQL:-1}"

REMOTE_PASSWORD="${REMOTE_PASSWORD:-}"

log() {
  echo
  echo "==> $1"
}

load_env_file() {
  if [ -f "$ENV_FILE" ]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
  fi
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1" >&2
    exit 1
  fi
}

prompt_password_if_needed() {
  if [ -n "$REMOTE_PASSWORD" ]; then
    return
  fi

  if ssh -o BatchMode=yes -o ConnectTimeout=5 -p "$REMOTE_PORT" "${REMOTE_USER}@${REMOTE_HOST}" "exit 0" >/dev/null 2>&1; then
    return
  fi

  printf "Remote password for %s@%s: " "$REMOTE_USER" "$REMOTE_HOST" >&2
  stty -echo
  IFS= read -r REMOTE_PASSWORD
  stty echo
  printf '\n' >&2
}

run_expect() {
  local mode="$1"
  shift

  if [ -z "$REMOTE_PASSWORD" ]; then
    "$mode" "$@"
    return
  fi

  if ! command -v expect >/dev/null 2>&1; then
    echo "Password deployment requires 'expect', or configure SSH key login." >&2
    exit 1
  fi

  local expect_script
  local expect_status
  expect_script="$(mktemp "/tmp/${PROJECT_NAME}.expect.XXXXXX")"

  cat >"$expect_script" <<'EOF'
set mode [lindex $argv 0]
set password $env(REMOTE_PASSWORD)
set args [lrange $argv 1 end]

if {$mode eq "ssh"} {
  spawn ssh {*}$args
} elseif {$mode eq "scp"} {
  spawn scp {*}$args
} else {
  puts stderr "Unsupported mode: $mode"
  exit 1
}

expect {
  -re "(?i)are you sure you want to continue connecting" {
    send "yes\r"
    exp_continue
  }
  -re "(?i)password:" {
    send "$password\r"
    exp_continue
  }
  eof {
    catch wait result
    exit [lindex $result 3]
  }
}
EOF

  set +e
  REMOTE_PASSWORD="$REMOTE_PASSWORD" expect "$expect_script" "$mode" "$@"
  expect_status=$?
  set -e
  rm -f "$expect_script"
  return "$expect_status"
}

run_ssh() {
  local command="$1"
  local remote_shell
  remote_shell="bash -lc $(printf '%q' "$command")"

  run_expect ssh \
    -o StrictHostKeyChecking=no \
    -p "$REMOTE_PORT" \
    "${REMOTE_USER}@${REMOTE_HOST}" \
    "$remote_shell"
}

run_scp() {
  local src="$1"
  local dst="$2"

  run_expect scp \
    -o StrictHostKeyChecking=no \
    -P "$REMOTE_PORT" \
    "$src" \
    "${REMOTE_USER}@${REMOTE_HOST}:$dst"
}

build_archive() {
  local archive_path="$1"

  tar \
    --exclude='.git' \
    --exclude='.venv' \
    --exclude='.mypy_cache' \
    --exclude='__pycache__' \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='.env.remote-secrets' \
    --exclude='.DS_Store' \
    --exclude='._*' \
    -czf "$archive_path" \
    -C "$(dirname "$BASE_DIR")" \
    "$PROJECT_NAME"
}

main() {
  load_env_file

  REMOTE_HOST="${REMOTE_HOST:-}"
  REMOTE_USER="${REMOTE_USER:-}"
  REMOTE_APP_DIR="${REMOTE_APP_DIR:-/home/${REMOTE_USER}/stock-ai}"
  APP_USER="${APP_USER:-$REMOTE_USER}"

  if [ -z "$REMOTE_HOST" ] || [ -z "$REMOTE_USER" ]; then
    echo "REMOTE_HOST and REMOTE_USER are required. You can put them in $ENV_FILE." >&2
    exit 1
  fi

  require_cmd tar
  require_cmd ssh
  require_cmd scp

  prompt_password_if_needed

  local archive_path
  local deploy_env_path
  local remote_env_path
  archive_path="$(mktemp "/tmp/${PROJECT_NAME}.deploy.XXXXXX").tar.gz"
  deploy_env_path="$(mktemp "/tmp/${PROJECT_NAME}.deploy.env.XXXXXX")"
  remote_env_path="/tmp/${PROJECT_NAME}.deploy.env"
  trap 'rm -f "${archive_path:-}" "${deploy_env_path:-}"' EXIT

  log "Packing project"
  build_archive "$archive_path"

  log "Uploading project archive"
  run_scp "$archive_path" "/tmp/${PROJECT_NAME}.deploy.tar.gz"

  local env_names=(
    APP_NAME
    APP_USER
    MYSQL_HOST
    MYSQL_PORT
    MYSQL_DATABASE
    MYSQL_APP_USER
    MYSQL_APP_PASSWORD
    MYSQL_ROOT_PASSWORD
    INSTALL_MYSQL
    INSTALL_NGINX
    NGINX_SERVER_NAME
    DEEPSEEK_API_KEY
    DEEPSEEK_API_BASE
    DEEPSEEK_BASE_URL
    DEEPSEEK_MODEL
    MINIMAX_API_KEY
    MINIMAX_API_BASE
    MINIMAX_MODEL
    LLM_API_KEY
    LLM_API_BASE
    LLM_MODEL
    OPENAI_API_KEY
    OPENAI_BASE_URL
    OPENAI_API_BASE
    OPENAI_MODEL
    SCREENING_MAX_WORKERS
    SCREENING_SUBMIT_BATCH
    SCREENING_SAVE_INTERVAL
    STOCK_INFO_TTL
    KLINE_TTL
    SEARCH_TTL
  )
  local env_name
  : >"$deploy_env_path"
  for env_name in "${env_names[@]}"; do
    if [ -n "${!env_name:-}" ]; then
      printf "%s=%q\n" "$env_name" "${!env_name}" >>"$deploy_env_path"
    fi
  done
  printf "APP_DIR=%q\n" "$REMOTE_APP_DIR" >>"$deploy_env_path"
  chmod 600 "$deploy_env_path"

  log "Uploading deploy environment"
  run_scp "$deploy_env_path" "$remote_env_path"

  local preserve_env_names
  preserve_env_names="$(IFS=,; echo "${env_names[*]},APP_DIR")"

  local remote_cmd
remote_cmd=$(cat <<EOF
set -euo pipefail
DEPLOY_ENV_PATH=$(printf '%q' "$remote_env_path")
ARCHIVE_PATH=/tmp/${PROJECT_NAME}.deploy.tar.gz
TMP_DIR=\$(mktemp -d /tmp/${PROJECT_NAME}.XXXXXX)
cleanup() {
  rm -rf "\$TMP_DIR"
  rm -f "\$DEPLOY_ENV_PATH"
}
trap cleanup EXIT

set -a
. "\$DEPLOY_ENV_PATH"
set +a

APP_DIR="\${APP_DIR:-$(printf '%q' "$REMOTE_APP_DIR")}"
REMOTE_INSTALL_MYSQL="\${INSTALL_MYSQL:-1}"

if command -v mysql >/dev/null 2>&1; then
  if mysql --protocol=TCP -h "\${MYSQL_HOST:-127.0.0.1}" -P "\${MYSQL_PORT:-3306}" -u"\${MYSQL_APP_USER:-stock_ai}" -p"\${MYSQL_APP_PASSWORD:-}" -e "USE \\\`\${MYSQL_DATABASE:-stock_ai}\\\`;" >/dev/null 2>&1; then
    REMOTE_INSTALL_MYSQL=0
  fi
fi

mkdir -p "\$APP_DIR"
tar -xzf "\$ARCHIVE_PATH" -C "\$TMP_DIR"
rsync -a --delete --exclude '.git/' --exclude '.venv/' --exclude '.mypy_cache/' --exclude '__pycache__/' --exclude '._*' "\$TMP_DIR/${PROJECT_NAME}/" "\$APP_DIR/"
chmod +x "\$APP_DIR/deploy/setup_server.sh" "\$APP_DIR/run.sh"
export INSTALL_MYSQL="\$REMOTE_INSTALL_MYSQL"
sudo --preserve-env=${preserve_env_names} /bin/bash "\$APP_DIR/deploy/setup_server.sh"
sudo systemctl is-active "\${APP_NAME:-stock-ai}" >/dev/null
for _ in \$(seq 1 15); do
  if curl -fsS --max-time 5 http://127.0.0.1:8000/ >/dev/null 2>/dev/null; then
    break
  fi
  sleep 1
done
curl -fsS --max-time 5 http://127.0.0.1:8000/ >/dev/null
echo
echo "Deploy complete: http://${REMOTE_HOST}/"
EOF
)

  log "Deploying on remote host"
  run_ssh "$remote_cmd"

  log "Done"
  echo "Remote URL: http://${REMOTE_HOST}/"
}

main "$@"
