from __future__ import annotations

import json
from typing import Optional

from db import compat as db


class AssistantRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def create_message(
        self,
        *,
        session_id: str,
        intent: str,
        user_text: str,
        assistant_text: str,
        action_type: str = "",
        strategy_id: Optional[int] = None,
        strategy_name: str = "",
        task_id: Optional[int] = None,
        run_token: str = "",
        stock_code: str = "",
        stock_name: str = "",
        metadata: Optional[dict] = None,
    ) -> dict:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO stock_assistant_messages (
                session_id, intent, user_text, assistant_text, action_type,
                strategy_id, strategy_name, task_id, run_token,
                stock_code, stock_name, metadata_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                intent,
                user_text,
                assistant_text,
                action_type,
                strategy_id,
                strategy_name,
                task_id,
                run_token,
                stock_code,
                stock_name,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        message_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return self.get_message(message_id) or {}

    def get_message(self, message_id: int) -> Optional[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT * FROM stock_assistant_messages WHERE id = ?",
            (message_id,),
        ).fetchone()
        conn.close()
        return self._decode_row(dict(row)) if row else None

    def list_sessions(self, limit: int = 50) -> list[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        rows = cursor.execute(
            """
            SELECT
                session_id,
                MIN(id) AS first_id,
                MAX(id) AS latest_id,
                COUNT(*) AS message_count,
                MIN(created_at) AS started_at,
                MAX(created_at) AS updated_at
            FROM stock_assistant_messages
            GROUP BY session_id
            ORDER BY latest_id DESC
            LIMIT ?
            """,
            (max(1, min(limit, 200)),),
        ).fetchall()
        session_rows = [dict(row) for row in rows]
        message_ids = []
        for row in session_rows:
            if row.get("first_id"):
                message_ids.append(int(row["first_id"]))
            if row.get("latest_id"):
                message_ids.append(int(row["latest_id"]))

        messages_by_id = {}
        if message_ids:
            placeholders = ",".join("?" for _ in message_ids)
            message_rows = cursor.execute(
                f"SELECT * FROM stock_assistant_messages WHERE id IN ({placeholders})",
                tuple(message_ids),
            ).fetchall()
            messages_by_id = {
                int(row["id"]): self._decode_row(dict(row))
                for row in message_rows
            }

        conn.close()
        sessions = []
        for row in session_rows:
            first = messages_by_id.get(int(row["first_id"]), {}) if row.get("first_id") else {}
            latest = messages_by_id.get(int(row["latest_id"]), {}) if row.get("latest_id") else {}
            title = (first.get("user_text") or latest.get("user_text") or "新会话").strip()
            sessions.append(
                {
                    "id": row.get("latest_id"),
                    "session_id": row.get("session_id") or "",
                    "title": title,
                    "user_text": latest.get("user_text") or title,
                    "assistant_text": latest.get("assistant_text") or "",
                    "intent": latest.get("intent") or "",
                    "action_type": latest.get("action_type") or "",
                    "strategy_id": latest.get("strategy_id"),
                    "strategy_name": latest.get("strategy_name") or "",
                    "task_id": latest.get("task_id"),
                    "run_token": latest.get("run_token") or "",
                    "stock_code": latest.get("stock_code") or "",
                    "stock_name": latest.get("stock_name") or "",
                    "metadata": latest.get("metadata") or {},
                    "message_count": int(row.get("message_count") or 0),
                    "created_at": row.get("started_at"),
                    "updated_at": row.get("updated_at"),
                }
            )
        return sessions

    def list_messages(self, session_id: Optional[str] = None, limit: int = 50, ascending: bool = False) -> list[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        sql = "SELECT * FROM stock_assistant_messages WHERE 1 = 1"
        params = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += f" ORDER BY id {'ASC' if ascending else 'DESC'} LIMIT ?"
        params.append(max(1, min(limit, 200)))
        rows = cursor.execute(sql, params).fetchall()
        conn.close()
        return [self._decode_row(dict(row)) for row in rows]

    def clear_messages(self, session_id: Optional[str] = None) -> int:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        if session_id:
            cursor.execute("DELETE FROM stock_assistant_messages WHERE session_id = ?", (session_id,))
        else:
            cursor.execute("DELETE FROM stock_assistant_messages")
        deleted = getattr(cursor, "rowcount", 0)
        conn.commit()
        conn.close()
        return int(deleted or 0)

    def _decode_row(self, row: dict) -> dict:
        try:
            metadata = json.loads(row.get("metadata_json") or "{}")
        except Exception:
            metadata = {}
        row["metadata"] = metadata if isinstance(metadata, dict) else {}
        return row
