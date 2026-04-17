from __future__ import annotations

from .config import settings
from .core.task_system import TaskManager


task_manager = TaskManager(settings.db_path)
