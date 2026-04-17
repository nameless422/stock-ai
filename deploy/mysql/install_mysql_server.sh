#!/usr/bin/env bash
set -euo pipefail

MYSQL_ROOT_PASSWORD="${MYSQL_ROOT_PASSWORD:-ChangeMe_123456}"
MYSQL_DATABASE="${MYSQL_DATABASE:-stock_ai}"
MYSQL_APP_USER="${MYSQL_APP_USER:-stock_ai}"
MYSQL_APP_PASSWORD="${MYSQL_APP_PASSWORD:-StockAI_123456}"
MYSQL_BIND_ADDRESS="${MYSQL_BIND_ADDRESS:-127.0.0.1}"
SUDO="${SUDO:-}"

if command -v apt-get >/dev/null 2>&1; then
  $SUDO apt-get update
  DEBIAN_FRONTEND=noninteractive $SUDO apt-get install -y mysql-server
elif command -v yum >/dev/null 2>&1; then
  $SUDO yum install -y mysql-server
else
  echo "Unsupported package manager"
  exit 1
fi

$SUDO systemctl enable mysql || $SUDO systemctl enable mysqld
$SUDO systemctl restart mysql || $SUDO systemctl restart mysqld

MYSQL_CNF=""
for candidate in /etc/mysql/mysql.conf.d/mysqld.cnf /etc/my.cnf; do
  if [ -f "$candidate" ]; then
    MYSQL_CNF="$candidate"
    break
  fi
done

if [ -n "$MYSQL_CNF" ]; then
  $SUDO sed -i.bak -E "s/^bind-address\s*=.*/bind-address = ${MYSQL_BIND_ADDRESS}/" "$MYSQL_CNF" || true
fi

$SUDO systemctl restart mysql || $SUDO systemctl restart mysqld

$SUDO mysql -uroot <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_ROOT_PASSWORD}';
CREATE DATABASE IF NOT EXISTS \`${MYSQL_DATABASE}\` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS '${MYSQL_APP_USER}'@'%' IDENTIFIED BY '${MYSQL_APP_PASSWORD}';
GRANT ALL PRIVILEGES ON \`${MYSQL_DATABASE}\`.* TO '${MYSQL_APP_USER}'@'%';
FLUSH PRIVILEGES;
SQL

echo
echo "MySQL installed."
echo "Database: ${MYSQL_DATABASE}"
echo "App user: ${MYSQL_APP_USER}"
echo "Bind address: ${MYSQL_BIND_ADDRESS}"
