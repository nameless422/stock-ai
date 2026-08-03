from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo


BASE_DIR = Path(__file__).resolve().parent.parent
@dataclass(frozen=True)
class Settings:
    base_dir: Path = BASE_DIR
    db_path: str = os.getenv("STOCK_AI_DB_URL", "")
    web_concurrency: int = max(1, min(8, int(os.getenv("WEB_CONCURRENCY", "2"))))
    screening_max_workers: int = max(4, min(16, int(os.getenv("SCREENING_MAX_WORKERS", "12"))))
    screening_submit_batch: int = max(50, int(os.getenv("SCREENING_SUBMIT_BATCH", "240")))
    screening_save_interval: int = max(20, int(os.getenv("SCREENING_SAVE_INTERVAL", "50")))
    stock_info_ttl: float = float(os.getenv("STOCK_INFO_TTL", "5"))
    kline_ttl: float = float(os.getenv("KLINE_TTL", "20"))
    search_ttl: float = float(os.getenv("SEARCH_TTL", "15"))
    assistant_asr_backend: str = os.getenv("ASSISTANT_ASR_BACKEND", "").strip().lower()
    assistant_asr_language: str = os.getenv("ASSISTANT_ASR_LANGUAGE", "zh").strip() or "zh"
    assistant_asr_threads: int = max(1, int(os.getenv("ASSISTANT_ASR_THREADS", "2")))
    assistant_asr_timeout: int = max(10, int(os.getenv("ASSISTANT_ASR_TIMEOUT", "90")))
    assistant_asr_max_bytes: int = max(1024 * 1024, int(os.getenv("ASSISTANT_ASR_MAX_BYTES", str(10 * 1024 * 1024))))
    whisper_cpp_bin: str = os.getenv("WHISPER_CPP_BIN", "/opt/whisper.cpp/build/bin/whisper-cli")
    whisper_cpp_model: str = os.getenv("WHISPER_CPP_MODEL", "/opt/whisper.cpp/models/ggml-base.bin")
    ffmpeg_bin: str = os.getenv("FFMPEG_BIN", "ffmpeg")
    market_tz: ZoneInfo = ZoneInfo("Asia/Shanghai")
    market_period_config: dict = None

    def __post_init__(self) -> None:
        if self.market_period_config is None:
            object.__setattr__(
                self,
                "market_period_config",
                {
                    "daily": {
                        "period_key": "day",
                        "payload_key": "qfqday",
                        "default_bars": 180,
                        "eastmoney_klt": "101",
                    },
                    "weekly": {
                        "period_key": "week",
                        "payload_key": "qfqweek",
                        "default_bars": 60,
                        "eastmoney_klt": "102",
                    },
                    "monthly": {
                        "period_key": "month",
                        "payload_key": "qfqmonth",
                        "default_bars": 180,
                        "eastmoney_klt": "103",
                    },
                },
            )


settings = Settings()


def has_database_config() -> bool:
    return bool(settings.db_path.strip())


def require_database_url() -> str:
    db_url = settings.db_path.strip()
    if not db_url:
        raise RuntimeError("STOCK_AI_DB_URL is required and must point to a MySQL database.")
    return db_url
