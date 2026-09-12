from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    grades_source: str
    local_grades_dir: Path
    drive_folder_id: str | None
    google_credentials_file: Path | None
    timezone: ZoneInfo
    data_dir: Path
    cache_ttl_seconds: int
    send_empty_summaries: bool
    cache_stale_after_seconds: int = 1800


def _bool_from_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_settings() -> Settings:
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    source = os.getenv("GRADES_SOURCE", "local").strip().lower()
    if source not in {"local", "drive"}:
        raise ValueError("GRADES_SOURCE must be either 'local' or 'drive'.")

    local_dir = Path(os.getenv("GRADES_LOCAL_DIR", "examples")).expanduser()
    drive_folder_id = os.getenv("GRADES_DRIVE_FOLDER_ID", "").strip() or None
    credentials = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    credentials_path = Path(credentials).expanduser() if credentials else None
    timezone = ZoneInfo(os.getenv("BOT_TIMEZONE", "Europe/Kyiv"))
    data_dir = Path(os.getenv("DATA_DIR", ".bot_data")).expanduser()

    return Settings(
        telegram_bot_token=token,
        grades_source=source,
        local_grades_dir=local_dir,
        drive_folder_id=drive_folder_id,
        google_credentials_file=credentials_path,
        timezone=timezone,
        data_dir=data_dir,
        cache_ttl_seconds=int(os.getenv("CACHE_TTL_SECONDS", "300")),
        send_empty_summaries=_bool_from_env("SEND_EMPTY_SUMMARIES", True),
        cache_stale_after_seconds=int(os.getenv("CACHE_STALE_AFTER_SECONDS", "1800")),
    )
