"""Environment settings shared by the notifier and scraper."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def today_jst():
    return datetime.now(JST).date()


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    if value.lower() in {"1", "true", "yes"}:
        return True
    if value.lower() in {"0", "false", "no"}:
        return False
    raise ValueError(f"{name} must be true or false")


def env_positive_int(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def reservation_credentials() -> tuple[str, str]:
    user = os.getenv("ARAKAWA_USER_ID", "").strip()
    password = os.getenv("ARAKAWA_PASSWORD", "")
    if not user or not password:
        raise RuntimeError("ARAKAWA_USER_ID and ARAKAWA_PASSWORD must be configured")
    return user, password


SHOW_BROWSER = env_bool("ARAKAWA_SHOW_BROWSER", False)
INCLUDE_WEEKDAYS_FOR_DEBUG = env_bool("ARAKAWA_INCLUDE_WEEKDAYS", False)
INCLUDE_ALL_TIME_SLOTS_FOR_DEBUG = env_bool("ARAKAWA_INCLUDE_ALL_TIME_SLOTS", False)
BOOKING_ENABLED = env_bool("ARAKAWA_BOOKING_ENABLED", True)
SCRAPER_TIMEOUT_SECONDS = env_positive_int("ARAKAWA_TIMEOUT_SECONDS", 480)
MAX_SCAN_SECONDS = env_positive_int("ARAKAWA_MAX_SCAN_SECONDS", 420)
TO_EMAIL = os.getenv("ARAKAWA_NOTIFY_EMAIL") or "seirantomo1999@gmail.com"
