from __future__ import annotations

import secrets
import threading
import time
from datetime import datetime
from typing import Optional

from app.config import settings
from app.core.screening_tasks import ScreeningTaskHandler
from app.repositories.screening_repository import ScreeningRepository
from app.runtime import task_manager
from app.services.market_service import next_daily_run, next_trading_day_run, sync_market_cache_for_all_stocks
from app.services.strategy_service import resolve_screening_target


screening_repository = ScreeningRepository(settings.db_path)
_scheduler_started = False


def save_screening_run(*args, **kwargs) -> None:
    screening_repository.save_run(*args, **kwargs)


def enqueue_screening_task(target_type: Optional[str] = None, target_id: Optional[int] = None, source: str = "manual") -> dict:
    requested_target_type = target_type or "strategy"
    requested_target_id = int(target_id) if target_id is not None else None
    target_info = resolve_screening_target(requested_target_type, requested_target_id)
    if not target_info:
        raise ValueError("没有可执行的策略或策略组")
    run_token = secrets.token_hex(8)
    return task_manager.enqueue(
        task_type="screening",
        payload={"target_type": requested_target_type, "target_id": requested_target_id, "source": source},
        queue_name="screening",
        priority=50 if source == "scheduler" else 100,
        run_token=run_token,
        target_type=target_info["target_type"],
        target_id=target_info["target_id"],
        target_name=target_info["target_name"],
    )


def scheduled_screening() -> None:
    while True:
        now = datetime.now(settings.market_tz)
        target = next_trading_day_run(7, 0, now=now)
        time.sleep((target - now).total_seconds())
        try:
            enqueue_screening_task(source="scheduler")
        except Exception as exc:
            print(f"[选股定时任务] 入队失败: {exc}")


def scheduled_cleanup() -> None:
    while True:
        now = datetime.now(settings.market_tz)
        target = next_daily_run(3, 0, now=now)
        time.sleep((target - now).total_seconds())
        deleted = screening_repository.cleanup_old_data(settings.db_path)
        print(f"[清理] 已删除 {deleted} 条7天前的选股记录")


def scheduled_market_cache_sync() -> None:
    while True:
        now = datetime.now(settings.market_tz)
        target = next_trading_day_run(15, 30, now=now)
        time.sleep((target - now).total_seconds())
        sync_market_cache_for_all_stocks()


def bootstrap_task_system() -> None:
    global _scheduler_started
    task_manager.register_handler(
        "screening",
        ScreeningTaskHandler(
            target_resolver=resolve_screening_target,
            run_saver=save_screening_run,
            max_workers=settings.screening_max_workers,
            submit_batch=settings.screening_submit_batch,
            save_interval=settings.screening_save_interval,
        ),
    )
    task_manager.start()
    if _scheduler_started:
        return
    threading.Thread(target=scheduled_screening, daemon=True).start()
    threading.Thread(target=scheduled_cleanup, daemon=True).start()
    threading.Thread(target=scheduled_market_cache_sync, daemon=True).start()
    _scheduler_started = True
