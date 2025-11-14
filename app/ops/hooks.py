from __future__ import annotations

import os

from app.alerts.notifier import Event, get_notifier


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def ops_alert(event: Event) -> None:
    if not _env_flag("FF_ALERTS", False):
        return
    get_notifier().emit(event)
