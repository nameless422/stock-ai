from db import compat as db
from app.core.strategy_engine import (
    DEFAULT_STRATEGY_CODE,
    DEFAULT_STRATEGY_DESCRIPTION,
    DEFAULT_STRATEGY_NAME,
)

def ensure_column(cursor, table_name, column_name, column_sql):
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


def init_db(db_target):
    conn = db.connect(db_target)
    c = conn.cursor()
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS screening_results (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            run_token VARCHAR(64),
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
            run_token VARCHAR(64),
            run_date VARCHAR(32),
            run_time VARCHAR(32),
            total_stocks INT,
            matched_count INT,
            status VARCHAR(32),
            failure_summary TEXT,
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
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS market_kline_cache (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            stock_code VARCHAR(32) NOT NULL,
            symbol VARCHAR(32) NOT NULL,
            period VARCHAR(16) NOT NULL,
            adjust_type VARCHAR(16) DEFAULT 'qfq',
            bars_count INT DEFAULT 0,
            payload LONGTEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS task_jobs (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            task_type VARCHAR(64) NOT NULL,
            queue_name VARCHAR(64) DEFAULT 'default',
            status VARCHAR(32) NOT NULL,
            priority INT DEFAULT 100,
            payload_text LONGTEXT,
            run_token VARCHAR(64) DEFAULT '',
            target_type VARCHAR(32) DEFAULT '',
            target_id BIGINT,
            target_name VARCHAR(255) DEFAULT '',
            progress_current INT DEFAULT 0,
            progress_total INT DEFAULT 0,
            progress_message TEXT,
            result_text TEXT,
            result_payload LONGTEXT,
            error_text TEXT,
            started_at VARCHAR(32),
            completed_at VARCHAR(32),
            created_at VARCHAR(32) NOT NULL,
            updated_at VARCHAR(32) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS task_logs (
            id BIGINT PRIMARY KEY AUTO_INCREMENT,
            task_id BIGINT NOT NULL,
            level VARCHAR(16) NOT NULL,
            message TEXT NOT NULL,
            created_at VARCHAR(32) NOT NULL,
            KEY idx_task_id (task_id),
            CONSTRAINT fk_task_logs_task
                FOREIGN KEY (task_id) REFERENCES task_jobs(id)
                ON DELETE CASCADE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        """
    )

    ensure_column(c, "screening_results", "target_type", "VARCHAR(32)")
    ensure_column(c, "screening_results", "target_id", "BIGINT")
    ensure_column(c, "screening_results", "target_name", "VARCHAR(255)")
    ensure_column(c, "screening_results", "matched_strategies", "TEXT")
    ensure_column(c, "screening_results", "result_payload", "LONGTEXT")
    ensure_column(c, "screening_results", "score", "DOUBLE DEFAULT 0")
    ensure_column(c, "screening_results", "run_token", "VARCHAR(64)")
    ensure_column(c, "screening_runs", "target_type", "VARCHAR(32)")
    ensure_column(c, "screening_runs", "target_id", "BIGINT")
    ensure_column(c, "screening_runs", "target_name", "VARCHAR(255)")
    ensure_column(c, "screening_runs", "target_logic", "VARCHAR(32)")
    ensure_column(c, "screening_runs", "run_token", "VARCHAR(64)")
    ensure_column(c, "screening_runs", "failure_summary", "TEXT")

    create_mysql_index(c, "screening_runs", "idx_screening_runs_target", "target_type, target_id, created_at")
    create_mysql_index(c, "screening_runs", "idx_screening_runs_run_token", "run_token")
    create_mysql_index(c, "screening_results", "idx_screening_results_target", "target_type, target_id, run_date, run_time")
    create_mysql_index(c, "screening_results", "idx_screening_results_run_token", "run_token")
    create_mysql_index(c, "strategy_group_items", "idx_strategy_group_items_group", "group_id, sort_order")
    create_mysql_index(c, "market_kline_cache", "idx_market_kline_cache_lookup", "stock_code, period, adjust_type")
    create_mysql_index(c, "task_jobs", "idx_task_jobs_status_priority", "status, priority, id")
    create_mysql_index(c, "task_logs", "idx_task_logs_task_id", "task_id, id")
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
