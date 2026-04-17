"""
Background worker entrypoint for screening tasks and schedulers.
"""

from __future__ import annotations

import signal
import time

from db.schema import init_db
from app.config import require_database_url
from app.services.screener_service import bootstrap_task_system


_running = True


def _handle_exit(signum, frame) -> None:  # pragma: no cover - signal handler
    global _running
    _running = False


def main() -> None:
    init_db(require_database_url())
    bootstrap_task_system()

    signal.signal(signal.SIGTERM, _handle_exit)
    signal.signal(signal.SIGINT, _handle_exit)

    while _running:
        time.sleep(1)


if __name__ == "__main__":
    main()
