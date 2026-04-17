import json
import threading
import time
from datetime import datetime
from typing import Callable, Optional

from db import compat as db


TaskHandler = Callable[[dict, "TaskExecutionContext"], dict]
TASK_LIST_COLUMNS = """
    id,
    task_type,
    queue_name,
    status,
    priority,
    run_token,
    target_type,
    target_id,
    target_name,
    progress_current,
    progress_total,
    progress_message,
    result_text,
    result_payload,
    error_text,
    started_at,
    completed_at,
    created_at,
    updated_at
"""


def _now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class TaskStore:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def create_task(
        self,
        task_type: str,
        payload: dict,
        queue_name: str = "default",
        priority: int = 100,
        run_token: str = "",
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        target_name: str = "",
    ) -> dict:
        conn = db.connect(self.db_path)
        c = conn.cursor()
        now = _now_text()
        c.execute(
            """
            INSERT INTO task_jobs (
                task_type, queue_name, status, priority, payload_text, run_token,
                target_type, target_id, target_name, progress_current, progress_total,
                progress_message, created_at, updated_at
            )
            VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, ?, 0, 0, '', ?, ?)
            """,
            (
                task_type,
                queue_name,
                priority,
                json.dumps(payload or {}, ensure_ascii=False),
                run_token,
                target_type,
                target_id,
                target_name,
                now,
                now,
            ),
        )
        task_id = c.lastrowid
        conn.commit()
        conn.close()
        self.append_log(task_id, "info", "任务已进入队列")
        return self.get_task(task_id)

    def get_task(self, task_id: int) -> Optional[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        c = conn.cursor()
        row = c.execute("SELECT * FROM task_jobs WHERE id = ?", (task_id,)).fetchone()
        conn.close()
        return self._decode_row(dict(row)) if row else None

    def list_tasks(
        self,
        task_type: Optional[str] = None,
        status: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        limit: int = 30,
        sort_mode: str = "recent",
    ) -> list[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        c = conn.cursor()
        sql = f"SELECT {TASK_LIST_COLUMNS} FROM task_jobs WHERE 1 = 1"
        params = []
        if task_type:
            sql += " AND task_type = ?"
            params.append(task_type)
        if status:
            sql += " AND status = ?"
            params.append(status)
        if target_type:
            sql += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        if sort_mode == "queue":
            sql += """
                ORDER BY
                    CASE
                        WHEN status = 'running' THEN 0
                        WHEN status = 'queued' THEN 1
                        ELSE 2
                    END ASC,
                    CASE WHEN status IN ('running', 'queued') THEN priority ELSE NULL END ASC,
                    CASE WHEN status IN ('running', 'queued') THEN id ELSE NULL END ASC,
                    id DESC
            """
        else:
            sql += " ORDER BY id DESC"
        sql += " LIMIT ?"
        params.append(limit)
        rows = c.execute(sql, params).fetchall()
        conn.close()
        return [self._decode_row(dict(row)) for row in rows]

    def list_task_logs(self, task_id: int, limit: int = 100) -> list[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        c = conn.cursor()
        rows = c.execute(
            "SELECT * FROM task_logs WHERE task_id = ? ORDER BY id DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_latest_task(
        self,
        task_type: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        statuses: Optional[list[str]] = None,
    ) -> Optional[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        c = conn.cursor()
        sql = f"SELECT {TASK_LIST_COLUMNS} FROM task_jobs WHERE 1 = 1"
        params = []
        if task_type:
            sql += " AND task_type = ?"
            params.append(task_type)
        if target_type:
            sql += " AND target_type = ?"
            params.append(target_type)
        if target_id is not None:
            sql += " AND target_id = ?"
            params.append(target_id)
        if statuses:
            placeholders = ", ".join(["?"] * len(statuses))
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY id DESC LIMIT 1"
        row = c.execute(sql, params).fetchone()
        conn.close()
        return self._decode_row(dict(row)) if row else None

    def acquire_next_task(self) -> Optional[dict]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        c = conn.cursor()
        row = c.execute(
            """
            SELECT * FROM task_jobs
            WHERE status = 'queued'
            ORDER BY priority ASC, id ASC
            LIMIT 1
            """
        ).fetchone()
        if not row:
            conn.close()
            return None

        task = dict(row)
        now = _now_text()
        c.execute(
            """
            UPDATE task_jobs
            SET status = 'running', started_at = ?, updated_at = ?
            WHERE id = ? AND status = 'queued'
            """,
            (now, now, task["id"]),
        )
        changed = getattr(c, "rowcount", 0)
        conn.commit()
        conn.close()
        if changed == 0:
            return None
        self.append_log(task["id"], "info", "任务开始执行")
        return self.get_task(task["id"])

    def recover_interrupted_tasks(self) -> int:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        c = conn.cursor()
        rows = c.execute("SELECT id FROM task_jobs WHERE status = 'running'").fetchall()
        now = _now_text()
        c.execute(
            """
            UPDATE task_jobs
            SET status = 'failed',
                completed_at = ?,
                error_text = COALESCE(error_text, '任务因服务重启中断，请重新发起'),
                updated_at = ?
            WHERE status = 'running'
            """,
            (now, now),
        )
        conn.commit()
        conn.close()
        for row in rows:
            self.append_log(int(row["id"]), "error", "任务因服务重启中断，已标记为失败")
        return len(rows)

    def update_task(
        self,
        task_id: int,
        *,
        status: Optional[str] = None,
        progress_current: Optional[int] = None,
        progress_total: Optional[int] = None,
        progress_message: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        target_name: Optional[str] = None,
        started_at: Optional[str] = None,
        completed_at: Optional[str] = None,
        result_text: Optional[str] = None,
        result_payload: Optional[dict] = None,
        error_text: Optional[str] = None,
    ) -> None:
        updates = []
        params = []
        if status is not None:
            updates.append("status = ?")
            params.append(status)
        if progress_current is not None:
            updates.append("progress_current = ?")
            params.append(progress_current)
        if progress_total is not None:
            updates.append("progress_total = ?")
            params.append(progress_total)
        if progress_message is not None:
            updates.append("progress_message = ?")
            params.append(progress_message)
        if target_type is not None:
            updates.append("target_type = ?")
            params.append(target_type)
        if target_id is not None:
            updates.append("target_id = ?")
            params.append(target_id)
        if target_name is not None:
            updates.append("target_name = ?")
            params.append(target_name)
        if started_at is not None:
            updates.append("started_at = ?")
            params.append(started_at)
        if completed_at is not None:
            updates.append("completed_at = ?")
            params.append(completed_at)
        if result_text is not None:
            updates.append("result_text = ?")
            params.append(result_text)
        if result_payload is not None:
            updates.append("result_payload = ?")
            params.append(json.dumps(result_payload, ensure_ascii=False))
        if error_text is not None:
            updates.append("error_text = ?")
            params.append(error_text)

        updates.append("updated_at = ?")
        params.append(_now_text())
        params.append(task_id)

        conn = db.connect(self.db_path)
        c = conn.cursor()
        c.execute(f"UPDATE task_jobs SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        conn.close()

    def append_log(self, task_id: int, level: str, message: str) -> None:
        conn = db.connect(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO task_logs (task_id, level, message, created_at) VALUES (?, ?, ?, ?)",
            (task_id, level, message, _now_text()),
        )
        conn.commit()
        conn.close()

    def delete_task(self, task_id: int) -> tuple[bool, str]:
        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        c = conn.cursor()
        row = c.execute("SELECT * FROM task_jobs WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            return False, "任务不存在"

        task = dict(row)
        if task.get("status") == "running":
            conn.close()
            return False, "运行中的任务不允许删除"

        c.execute("DELETE FROM task_jobs WHERE id = ? AND status != 'running'", (task_id,))
        changed = getattr(c, "rowcount", 0)
        conn.commit()
        conn.close()
        if changed == 0:
            return False, "任务状态已变化，请刷新后重试"
        return True, "任务已删除"

    def reorder_task(self, task_id: int, action: str) -> tuple[bool, str]:
        if action not in {"up", "down", "top"}:
            return False, "不支持的任务排序动作"

        conn = db.connect(self.db_path)
        conn.row_factory = db.Row
        c = conn.cursor()
        row = c.execute("SELECT * FROM task_jobs WHERE id = ?", (task_id,)).fetchone()
        if not row:
            conn.close()
            return False, "任务不存在"

        task = dict(row)
        if task.get("status") != "queued":
            conn.close()
            return False, "只有排队中的任务才能调整顺序"

        rows = c.execute(
            """
            SELECT * FROM task_jobs
            WHERE status = 'queued' AND task_type = ? AND queue_name = ?
            ORDER BY priority ASC, id ASC
            """,
            (task.get("task_type"), task.get("queue_name")),
        ).fetchall()
        queued_tasks = [dict(item) for item in rows]
        index = next((idx for idx, item in enumerate(queued_tasks) if int(item["id"]) == int(task_id)), -1)
        if index < 0:
            conn.close()
            return False, "任务不在排队列表中"

        if action == "up":
            if index == 0:
                conn.close()
                return False, "任务已经在最前面"
            queued_tasks[index - 1], queued_tasks[index] = queued_tasks[index], queued_tasks[index - 1]
            message = "任务顺序已上移"
        elif action == "down":
            if index >= len(queued_tasks) - 1:
                conn.close()
                return False, "任务已经在最后面"
            queued_tasks[index], queued_tasks[index + 1] = queued_tasks[index + 1], queued_tasks[index]
            message = "任务顺序已下移"
        else:
            if index == 0:
                conn.close()
                return False, "任务已经在队列最前"
            queued_tasks.insert(0, queued_tasks.pop(index))
            message = "任务已插队到最前面"

        for idx, item in enumerate(queued_tasks, start=1):
            c.execute(
                "UPDATE task_jobs SET priority = ?, updated_at = ? WHERE id = ?",
                (idx * 10, _now_text(), item["id"]),
            )
        conn.commit()
        conn.close()
        self.append_log(task_id, "info", message)
        return True, message

    def _decode_row(self, row: dict) -> dict:
        payload_text = row.get("payload_text") or "{}"
        result_payload = row.get("result_payload") or ""
        try:
            row["payload"] = json.loads(payload_text)
        except Exception:
            row["payload"] = {}
        try:
            row["result"] = json.loads(result_payload) if result_payload else {}
        except Exception:
            row["result"] = {}
        return row


class TaskExecutionContext:
    def __init__(self, store: TaskStore, task: dict):
        self.store = store
        self.task = task
        self._last_progress_key = None

    @property
    def task_id(self) -> int:
        return int(self.task["id"])

    def set_progress(self, current: int, total: int, message: str = "") -> None:
        progress_key = (current, total, message)
        if progress_key == self._last_progress_key:
            return
        self._last_progress_key = progress_key
        self.store.update_task(
            self.task_id,
            progress_current=current,
            progress_total=total,
            progress_message=message,
        )

    def set_target(self, target_type: str, target_id: Optional[int], target_name: str) -> None:
        self.store.update_task(
            self.task_id,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
        )

    def log(self, message: str, level: str = "info") -> None:
        self.store.append_log(self.task_id, level, message)

    def complete(self, result: dict, result_text: str = "") -> None:
        completed_at = _now_text()
        self.store.update_task(
            self.task_id,
            status="completed",
            completed_at=completed_at,
            result_text=result_text,
            result_payload=result,
        )
        self.store.append_log(self.task_id, "info", result_text or "任务执行完成")

    def fail(self, error_text: str, result: Optional[dict] = None) -> None:
        completed_at = _now_text()
        self.store.update_task(
            self.task_id,
            status="failed",
            completed_at=completed_at,
            error_text=error_text,
            result_payload=result or {},
        )
        self.store.append_log(self.task_id, "error", error_text)


class TaskManager:
    def __init__(self, db_path: str, poll_interval: float = 1.0):
        self.store = TaskStore(db_path)
        self.poll_interval = poll_interval
        self.handlers: dict[str, TaskHandler] = {}
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register_handler(self, task_type: str, handler: TaskHandler) -> None:
        self.handlers[task_type] = handler

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        recovered = self.store.recover_interrupted_tasks()
        if recovered:
            print(f"[任务系统] 已恢复 {recovered} 个因服务重启中断的任务")
        self._thread = threading.Thread(target=self._worker_loop, daemon=True)
        self._thread.start()

    def enqueue(
        self,
        task_type: str,
        payload: dict,
        queue_name: str = "default",
        priority: int = 100,
        run_token: str = "",
        target_type: Optional[str] = None,
        target_id: Optional[int] = None,
        target_name: str = "",
    ) -> dict:
        task = self.store.create_task(
            task_type=task_type,
            payload=payload,
            queue_name=queue_name,
            priority=priority,
            run_token=run_token,
            target_type=target_type,
            target_id=target_id,
            target_name=target_name,
        )
        self._wake_event.set()
        return task

    def list_tasks(self, **kwargs) -> list[dict]:
        return self.store.list_tasks(**kwargs)

    def get_task(self, task_id: int) -> Optional[dict]:
        task = self.store.get_task(task_id)
        if not task:
            return None
        task["logs"] = self.store.list_task_logs(task_id)
        return task

    def get_latest_task(self, **kwargs) -> Optional[dict]:
        return self.store.get_latest_task(**kwargs)

    def delete_task(self, task_id: int) -> tuple[bool, str]:
        return self.store.delete_task(task_id)

    def reorder_task(self, task_id: int, action: str) -> tuple[bool, str]:
        changed, message = self.store.reorder_task(task_id, action)
        if changed:
            self._wake_event.set()
        return changed, message

    def _worker_loop(self) -> None:
        while not self._stop_event.is_set():
            task = self.store.acquire_next_task()
            if not task:
                self._wake_event.wait(self.poll_interval)
                self._wake_event.clear()
                continue

            handler = self.handlers.get(task["task_type"])
            if not handler:
                self.store.update_task(
                    task["id"],
                    status="failed",
                    completed_at=_now_text(),
                    error_text=f"未注册任务处理器: {task['task_type']}",
                )
                self.store.append_log(task["id"], "error", f"未注册任务处理器: {task['task_type']}")
                continue

            context = TaskExecutionContext(self.store, task)
            try:
                result = handler(task, context) or {}
                result_text = str(result.get("summary", "") or "任务执行完成")
                context.complete(result, result_text=result_text)
            except Exception as exc:
                context.fail(str(exc))
            finally:
                time.sleep(0.05)
