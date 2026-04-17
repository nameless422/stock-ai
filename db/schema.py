import hashlib
import os

from db import compat as sqlite3
from strategy_engine import (
    DEFAULT_STRATEGY_CODE,
    DEFAULT_STRATEGY_DESCRIPTION,
    DEFAULT_STRATEGY_NAME,
)


def is_mysql(db_target):
    return sqlite3.is_mysql_target(db_target)


def ensure_column(cursor, db_target, table_name, column_name, column_sql):
    if is_mysql(db_target):
        row = cursor.execute(
            """
            SELECT COUNT(*)
            FROM information_schema.columns
            WHERE table_schema = DATABASE()
              AND table_name = %s
              AND column_name = %s
            """,
            (table_name, column_name),
        ).fetchone()
        if row and row[0] == 0:
            cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")
        return

    columns = [row[1] for row in cursor.execute(f"PRAGMA table_info({table_name})").fetchall()]
    if column_name not in columns:
        cursor.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_sql}")


def init_db(db_target):
    if not is_mysql(db_target):
        os.makedirs(os.path.dirname(db_target), exist_ok=True)

    conn = sqlite3.connect(db_target)
    c = conn.cursor()

    if is_mysql(db_target):
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS screening_results (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                run_date VARCHAR(32),
                run_time VARCHAR(32),
                stock_code VARCHAR(32),
                stock_name VARCHAR(255),
                daily_condition TEXT,
                weekly_condition TEXT,
                current_volume DOUBLE,
                max_volume_3m DOUBLE,
                dif DOUBLE,
                dea DOUBLE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                target_type VARCHAR(32),
                target_id BIGINT,
                target_name VARCHAR(255),
                matched_strategies TEXT,
                result_payload LONGTEXT,
                score DOUBLE DEFAULT 0
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS screening_runs (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                run_date VARCHAR(32),
                run_time VARCHAR(32),
                total_stocks INT,
                matched_count INT,
                status VARCHAR(32),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                target_type VARCHAR(32),
                target_id BIGINT,
                target_name VARCHAR(255),
                target_logic VARCHAR(32)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_definitions (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                name VARCHAR(255) NOT NULL UNIQUE,
                description TEXT,
                code LONGTEXT NOT NULL,
                enabled TINYINT DEFAULT 1,
                create_mode VARCHAR(32) DEFAULT 'direct',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_groups (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                name VARCHAR(255) NOT NULL UNIQUE,
                description TEXT,
                match_mode VARCHAR(16) DEFAULT 'AND',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS strategy_group_items (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                group_id BIGINT NOT NULL,
                strategy_id BIGINT NOT NULL,
                sort_order INT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_group_strategy (group_id, strategy_id),
                KEY idx_group_id (group_id),
                CONSTRAINT fk_strategy_group_items_group
                    FOREIGN KEY (group_id) REFERENCES strategy_groups(id)
                    ON DELETE CASCADE,
                CONSTRAINT fk_strategy_group_items_strategy
                    FOREIGN KEY (strategy_id) REFERENCES strategy_definitions(id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    else:
        c.execute(
            """CREATE TABLE IF NOT EXISTS screening_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                run_time TEXT,
                stock_code TEXT,
                stock_name TEXT,
                daily_condition TEXT,
                weekly_condition TEXT,
                current_volume REAL,
                max_volume_3m REAL,
                dif REAL,
                dea REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS screening_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_date TEXT,
                run_time TEXT,
                total_stocks INTEGER,
                matched_count INTEGER,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS strategy_definitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                code TEXT NOT NULL,
                enabled INTEGER DEFAULT 1,
                create_mode TEXT DEFAULT 'direct',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS strategy_groups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT DEFAULT '',
                match_mode TEXT DEFAULT 'AND',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS strategy_group_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id INTEGER NOT NULL,
                strategy_id INTEGER NOT NULL,
                sort_order INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(group_id, strategy_id),
                FOREIGN KEY (group_id) REFERENCES strategy_groups(id),
                FOREIGN KEY (strategy_id) REFERENCES strategy_definitions(id)
            )"""
        )

    ensure_column(c, db_target, "screening_results", "target_type", "VARCHAR(32)" if is_mysql(db_target) else "TEXT")
    ensure_column(c, db_target, "screening_results", "target_id", "BIGINT" if is_mysql(db_target) else "INTEGER")
    ensure_column(c, db_target, "screening_results", "target_name", "VARCHAR(255)" if is_mysql(db_target) else "TEXT")
    ensure_column(c, db_target, "screening_results", "matched_strategies", "TEXT")
    ensure_column(c, db_target, "screening_results", "result_payload", "LONGTEXT" if is_mysql(db_target) else "TEXT")
    ensure_column(c, db_target, "screening_results", "score", "DOUBLE DEFAULT 0" if is_mysql(db_target) else "REAL DEFAULT 0")
    ensure_column(c, db_target, "screening_runs", "target_type", "VARCHAR(32)" if is_mysql(db_target) else "TEXT")
    ensure_column(c, db_target, "screening_runs", "target_id", "BIGINT" if is_mysql(db_target) else "INTEGER")
    ensure_column(c, db_target, "screening_runs", "target_name", "VARCHAR(255)" if is_mysql(db_target) else "TEXT")
    ensure_column(c, db_target, "screening_runs", "target_logic", "VARCHAR(32)" if is_mysql(db_target) else "TEXT")

    if is_mysql(db_target):
        create_mysql_index(c, "screening_runs", "idx_screening_runs_target", "target_type, target_id, created_at")
        create_mysql_index(c, "screening_results", "idx_screening_results_target", "target_type, target_id, run_date, run_time")
        create_mysql_index(c, "strategy_group_items", "idx_strategy_group_items_group", "group_id, sort_order")
    else:
        c.execute("CREATE INDEX IF NOT EXISTS idx_screening_runs_target ON screening_runs(target_type, target_id, created_at)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_screening_results_target ON screening_results(target_type, target_id, run_date, run_time)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_strategy_group_items_group ON strategy_group_items(group_id, sort_order)")
    c.execute("SELECT id FROM strategy_definitions WHERE name = ?", (DEFAULT_STRATEGY_NAME,))
    if not c.fetchone():
        c.execute(
            """
            INSERT INTO strategy_definitions (name, description, code, enabled, create_mode)
            VALUES (?, ?, ?, 1, 'builtin')
            """,
            (DEFAULT_STRATEGY_NAME, DEFAULT_STRATEGY_DESCRIPTION, DEFAULT_STRATEGY_CODE),
        )
    conn.commit()
    conn.close()


def init_vt_db(db_target):
    conn = sqlite3.connect(db_target)
    c = conn.cursor()

    if is_mysql(db_target):
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS vt_users (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                username VARCHAR(255) UNIQUE NOT NULL,
                password VARCHAR(255) NOT NULL,
                is_admin TINYINT DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS vt_accounts (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                user_id BIGINT NOT NULL,
                account_name VARCHAR(255) NOT NULL,
                balance DOUBLE DEFAULT 500000.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_vt_accounts_user
                    FOREIGN KEY (user_id) REFERENCES vt_users(id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS vt_positions (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                account_id BIGINT NOT NULL,
                stock_code VARCHAR(32) NOT NULL,
                stock_name VARCHAR(255) NOT NULL,
                shares INT NOT NULL,
                avg_cost DOUBLE NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE KEY uniq_account_stock (account_id, stock_code),
                CONSTRAINT fk_vt_positions_account
                    FOREIGN KEY (account_id) REFERENCES vt_accounts(id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS vt_trades (
                id BIGINT PRIMARY KEY AUTO_INCREMENT,
                account_id BIGINT NOT NULL,
                stock_code VARCHAR(32) NOT NULL,
                stock_name VARCHAR(255) NOT NULL,
                trade_type VARCHAR(16) NOT NULL,
                shares INT NOT NULL,
                price DOUBLE NOT NULL,
                total_amount DOUBLE NOT NULL,
                commission DOUBLE DEFAULT 0,
                traded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_vt_trades_account
                    FOREIGN KEY (account_id) REFERENCES vt_accounts(id)
                    ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    else:
        c.execute(
            """CREATE TABLE IF NOT EXISTS vt_users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL,
                is_admin INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS vt_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                account_name TEXT NOT NULL,
                balance REAL DEFAULT 500000.0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES vt_users(id)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS vt_positions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                shares INTEGER NOT NULL,
                avg_cost REAL NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(account_id, stock_code),
                FOREIGN KEY (account_id) REFERENCES vt_accounts(id)
            )"""
        )
        c.execute(
            """CREATE TABLE IF NOT EXISTS vt_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_id INTEGER NOT NULL,
                stock_code TEXT NOT NULL,
                stock_name TEXT NOT NULL,
                trade_type TEXT NOT NULL,
                shares INTEGER NOT NULL,
                price REAL NOT NULL,
                total_amount REAL NOT NULL,
                commission REAL DEFAULT 0,
                traded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (account_id) REFERENCES vt_accounts(id)
            )"""
        )

    c.execute("SELECT id FROM vt_users WHERE username = ? AND is_admin = 1", ("admin",))
    if not c.fetchone():
        c.execute(
            "INSERT INTO vt_users (username, password, is_admin) VALUES (?, ?, 1)",
            ("admin", hashlib.sha256("admin".encode()).hexdigest()),
        )
    conn.commit()
    conn.close()


def create_mysql_index(cursor, table_name, index_name, columns_sql):
    row = cursor.execute(
        """
        SELECT COUNT(*)
        FROM information_schema.statistics
        WHERE table_schema = DATABASE()
          AND table_name = %s
          AND index_name = %s
        """,
        (table_name, index_name),
    ).fetchone()
    if row and row[0] == 0:
        cursor.execute(f"CREATE INDEX {index_name} ON {table_name} ({columns_sql})")
