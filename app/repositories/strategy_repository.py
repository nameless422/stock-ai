from __future__ import annotations

from typing import Optional

from db import compat as db


class StrategyRepository:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def list_strategies(self, enabled_only: bool = False) -> list[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        sql = "SELECT * FROM strategy_definitions"
        if enabled_only:
            sql += " WHERE enabled = 1"
        sql += " ORDER BY id DESC"
        rows = cursor.execute(sql).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_strategy(self, strategy_id: int) -> Optional[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT * FROM strategy_definitions WHERE id = ?",
            (strategy_id,),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    def create_strategy(
        self,
        name: str,
        description: str,
        code: str,
        create_mode: str = "direct",
        enabled: int = 1,
    ) -> dict:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO strategy_definitions (name, description, code, enabled, create_mode, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (name.strip(), description.strip(), code, 1 if enabled else 0, create_mode),
        )
        strategy_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return self.get_strategy(strategy_id)

    def update_strategy(
        self,
        strategy_id: int,
        name: str,
        description: str,
        code: str,
        enabled: int = 1,
    ) -> bool:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE strategy_definitions
            SET name = ?, description = ?, code = ?, enabled = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name.strip(), description.strip(), code, 1 if enabled else 0, strategy_id),
        )
        changed = cursor.rowcount
        conn.commit()
        conn.close()
        return changed > 0

    def delete_strategy(self, strategy_id: int) -> bool:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM strategy_group_items WHERE strategy_id = ?", (strategy_id,))
        cursor.execute("DELETE FROM strategy_definitions WHERE id = ?", (strategy_id,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted > 0

    def list_strategy_groups(self) -> list[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        cursor = conn.cursor()
        groups = cursor.execute("SELECT * FROM strategy_groups ORDER BY id DESC").fetchall()
        result = []
        for group in groups:
            items = cursor.execute(
                """
                SELECT s.id, s.name
                FROM strategy_group_items gi
                JOIN strategy_definitions s ON s.id = gi.strategy_id
                WHERE gi.group_id = ?
                ORDER BY gi.sort_order ASC, gi.id ASC
                """,
                (group["id"],),
            ).fetchall()
            payload = dict(group)
            payload["strategies"] = [dict(item) for item in items]
            payload["strategy_ids"] = [item["id"] for item in items]
            result.append(payload)
        conn.close()
        return result

    def get_strategy_group(self, group_id: int) -> Optional[dict]:
        for group in self.list_strategy_groups():
            if group["id"] == group_id:
                return group
        return None

    def create_strategy_group(
        self,
        name: str,
        description: str,
        match_mode: str,
        strategy_ids: list[int],
    ) -> dict:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO strategy_groups (name, description, match_mode, updated_at)
            VALUES (?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (name.strip(), description.strip(), (match_mode or "AND").upper()),
        )
        group_id = cursor.lastrowid
        for index, strategy_id in enumerate(strategy_ids):
            cursor.execute(
                "INSERT OR IGNORE INTO strategy_group_items (group_id, strategy_id, sort_order) VALUES (?, ?, ?)",
                (group_id, strategy_id, index),
            )
        conn.commit()
        conn.close()
        return self.get_strategy_group(group_id)

    def update_strategy_group(
        self,
        group_id: int,
        name: str,
        description: str,
        match_mode: str,
        strategy_ids: list[int],
    ) -> bool:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE strategy_groups
            SET name = ?, description = ?, match_mode = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name.strip(), description.strip(), (match_mode or "AND").upper(), group_id),
        )
        cursor.execute("DELETE FROM strategy_group_items WHERE group_id = ?", (group_id,))
        for index, strategy_id in enumerate(strategy_ids):
            cursor.execute(
                "INSERT OR IGNORE INTO strategy_group_items (group_id, strategy_id, sort_order) VALUES (?, ?, ?)",
                (group_id, strategy_id, index),
            )
        changed = cursor.rowcount
        conn.commit()
        conn.close()
        return changed >= 0

    def delete_strategy_group(self, group_id: int) -> bool:
        conn = db.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("DELETE FROM strategy_group_items WHERE group_id = ?", (group_id,))
        cursor.execute("DELETE FROM strategy_groups WHERE id = ?", (group_id,))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        return deleted > 0
