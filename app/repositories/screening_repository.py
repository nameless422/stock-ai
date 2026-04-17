from __future__ import annotations

import json
from typing import Optional

from db import compat as db


class ScreeningRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def save_run(
        self,
        run_token: str,
        run_date: str,
        run_time: str,
        total_stocks: int,
        matched_count: int,
        status: str,
        results: list[dict],
        target_info: Optional[dict] = None,
        failure_summary: str = "",
        miss_log_text: str = "",
        miss_log_payload: Optional[dict] = None,
    ) -> None:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        target_info = target_info or {}

        cursor.execute("DELETE FROM screening_results WHERE run_token = ?", (run_token,))
        cursor.execute("DELETE FROM screening_runs WHERE run_token = ?", (run_token,))
        cursor.execute(
            """
            INSERT INTO screening_runs (
                run_token, run_date, run_time, total_stocks, matched_count, status,
                failure_summary, miss_log_text, miss_log_payload,
                target_type, target_id, target_name, target_logic
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_token,
                run_date,
                run_time,
                total_stocks,
                matched_count,
                status,
                failure_summary,
                miss_log_text,
                json.dumps(miss_log_payload or {}, ensure_ascii=False),
                target_info.get("target_type"),
                target_info.get("target_id"),
                target_info.get("target_name"),
                target_info.get("target_logic"),
            ),
        )

        seen = set()
        for item in results:
            code = item.get("code", "")
            if code in seen:
                continue
            seen.add(code)
            cursor.execute(
                """
                INSERT INTO screening_results (
                    run_token, run_date, run_time, stock_code, stock_name,
                    daily_condition, weekly_condition, current_volume, max_volume_3m, dif, dea,
                    target_type, target_id, target_name, matched_strategies, result_payload, score
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_token,
                    run_date,
                    run_time,
                    code,
                    item.get("name", ""),
                    item.get("daily", ""),
                    item.get("weekly", ""),
                    item.get("current_vol", 0),
                    item.get("max_vol_3m", 0),
                    item.get("dif", 0),
                    item.get("dea", 0),
                    target_info.get("target_type"),
                    target_info.get("target_id"),
                    target_info.get("target_name"),
                    ", ".join(item.get("matched_strategies", [])),
                    json.dumps(item.get("payload", {}), ensure_ascii=False),
                    item.get("score", 0),
                ),
            )

        conn.commit()
        conn.close()

    def query_latest_run(
        self,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        completed_only: bool = False,
    ) -> Optional[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        sql = "SELECT * FROM screening_runs WHERE 1 = 1"
        params = []
        if completed_only:
            sql += " AND status = 'completed'"
        if target_type:
            sql += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = cursor.execute(sql, params).fetchone()
        conn.close()
        return dict(row) if row else None

    def get_run(
        self,
        *,
        run_token: Optional[str] = None,
        run_date: Optional[str] = None,
        run_time: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
    ) -> Optional[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        sql = "SELECT * FROM screening_runs WHERE 1 = 1"
        params = []
        if run_token:
            sql += " AND run_token = ?"
            params.append(run_token)
        else:
            sql += " AND run_date = ? AND run_time = ?"
            params.extend([run_date, run_time])
        if target_type:
            sql += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        sql += " ORDER BY created_at DESC LIMIT 1"
        row = cursor.execute(sql, params).fetchone()
        conn.close()
        return dict(row) if row else None

    def query_runs(
        self,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        limit: int = 30,
        completed_only: bool = True,
    ) -> list[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        sql = "SELECT * FROM screening_runs WHERE 1 = 1"
        params = []
        if completed_only:
            sql += " AND status = 'completed'"
        if target_type:
            sql += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = cursor.execute(sql, params).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def query_results(
        self,
        *,
        run_token: Optional[str] = None,
        run_date: Optional[str] = None,
        run_time: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
    ) -> list[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        sql = "SELECT * FROM screening_results WHERE 1 = 1"
        params = []
        if run_token:
            sql += " AND run_token = ?"
            params.append(run_token)
        else:
            sql += " AND run_date = ? AND run_time = ?"
            params.extend([run_date, run_time])
        if target_type:
            sql += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        sql += " ORDER BY score DESC, current_volume DESC"
        rows = cursor.execute(sql, params).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def load_cached_klines(
        self,
        stock_code: str,
        period: str,
        adjust: str = "qfq",
        limit: Optional[int] = None,
    ) -> list:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        row = cursor.execute(
            """
            SELECT payload
            FROM market_kline_cache
            WHERE stock_code = ? AND period = ? AND adjust_type = ?
            """,
            (stock_code, period, adjust),
        ).fetchone()
        conn.close()
        if not row:
            return []
        try:
            payload = json.loads(row["payload"])
        except Exception:
            return []
        if not isinstance(payload, list):
            return []
        return payload[-limit:] if limit else payload

    def save_cached_klines(
        self,
        stock_code: str,
        symbol: str,
        period: str,
        klines: list,
        adjust: str = "qfq",
    ) -> None:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM market_kline_cache WHERE stock_code = ? AND period = ? AND adjust_type = ?",
            (stock_code, period, adjust),
        )
        cursor.execute(
            """
            INSERT INTO market_kline_cache
                (stock_code, symbol, period, adjust_type, bars_count, payload, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (stock_code, symbol, period, adjust, len(klines), json.dumps(klines, ensure_ascii=False)),
        )
        conn.commit()
        conn.close()

    def cleanup_old_data(self, db_target: str) -> int:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "DELETE FROM screening_results WHERE STR_TO_DATE(run_date, '%Y-%m-%d') < DATE_SUB(CURDATE(), INTERVAL 7 DAY)"
        )
        deleted = getattr(cursor, "rowcount", 0)
        cursor.execute(
            "DELETE FROM screening_runs WHERE STR_TO_DATE(run_date, '%Y-%m-%d') < DATE_SUB(CURDATE(), INTERVAL 7 DAY)"
        )
        cursor.execute(
            "DELETE FROM task_logs WHERE task_id IN (SELECT id FROM task_jobs WHERE created_at < DATE_SUB(NOW(), INTERVAL 7 DAY))"
        )
        cursor.execute("DELETE FROM task_jobs WHERE created_at < DATE_SUB(NOW(), INTERVAL 7 DAY)")
        conn.commit()
        conn.close()
        return deleted
