#!/usr/bin/env python3
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from db import compat
from db.schema import init_db, init_vt_db


TABLES = [
    "screening_results",
    "screening_runs",
    "strategy_definitions",
    "strategy_groups",
    "strategy_group_items",
    "vt_users",
    "vt_accounts",
    "vt_positions",
    "vt_trades",
]


def main():
    parser = argparse.ArgumentParser(description="Migrate stock-ai data from SQLite to MySQL.")
    parser.add_argument("--sqlite-path", required=True, help="Path to the source SQLite database.")
    parser.add_argument("--mysql-url", required=True, help="Target MySQL URL, e.g. mysql://user:pass@127.0.0.1:3306/stock_ai?charset=utf8mb4")
    parser.add_argument("--truncate", action="store_true", help="Truncate target tables before importing.")
    args = parser.parse_args()

    source = sqlite3.connect(args.sqlite_path)
    source.row_factory = sqlite3.Row

    init_db(args.mysql_url)
    init_vt_db(args.mysql_url)

    target = compat.connect(args.mysql_url)
    target.row_factory = compat.Row
    target_cursor = target.cursor()

    for table in reversed(TABLES):
        if args.truncate:
            target_cursor.execute(f"DELETE FROM {table}")
    target.commit()

    for table in TABLES:
        rows = source.execute(f"SELECT * FROM {table}").fetchall()
        if not rows:
            print(f"[skip] {table}: 0 rows")
            continue

        columns = [item[1] for item in source.execute(f"PRAGMA table_info({table})").fetchall()]
        placeholders = ", ".join(["?"] * len(columns))
        column_sql = ", ".join(columns)

        for row in rows:
            target_cursor.execute(
                f"INSERT INTO {table} ({column_sql}) VALUES ({placeholders})",
                tuple(row[column] for column in columns),
            )

        target.commit()
        print(f"[ok] {table}: {len(rows)} rows")

    source.close()
    target.close()
    print("Migration completed.")


if __name__ == "__main__":
    main()
