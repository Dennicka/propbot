from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from . import wire_telegram
from .levels import AlertLevel, should_route
from .registry import REGISTRY as alerts_registry

_LOG_FILE_LOCK = threading.Lock()


def _normalise_meta(meta: Mapping[str, Any]) -> Mapping[str, Any]:
    serialisable: dict[str, Any] = {}
    for key, value in meta.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            serialisable[key] = value
            continue
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            serialisable[key] = str(value)
        else:
            serialisable[key] = value
    return serialisable


def _format_record(level: AlertLevel, message: str, meta: Mapping[str, Any]) -> str:
    payload: dict[str, Any] = {
        "ts": time.time(),
        "level": level.value,
        "message": message,
    }
    if meta:
        payload["meta"] = meta
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def _write_stdout(record: str) -> None:
    sys.stdout.write(record + "\n")
    sys.stdout.flush()


def _write_logfile(record: str) -> None:
    path = Path(os.getenv("ALERTS_FILE_PATH", "data/alerts.log"))
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_FILE_LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(record + "\n")
            handle.flush()


def _format_telegram(level: AlertLevel, message: str, meta: Mapping[str, Any]) -> str:
    text = f"<b>{level.value}</b> · {message}"
    if meta:
        meta_json = json.dumps(meta, ensure_ascii=False, sort_keys=True)
        text = f"{text}\n<pre>{meta_json}</pre>"
    return text


def notify(level: AlertLevel | str, message: str, **meta: Any) -> None:
    resolved = AlertLevel.coerce(level)
    serialisable_meta = _normalise_meta(meta)
    record = _format_record(resolved, message, serialisable_meta)

    registry_details = dict(serialisable_meta)
    source = registry_details.pop("source", "ops")
    code = registry_details.pop("code", None)
    nested_details = registry_details.pop("details", None)
    if isinstance(nested_details, Mapping):
        registry_details.update(dict(nested_details))
    elif nested_details is not None:
        registry_details["details"] = nested_details

    alerts_registry.record(
        level=resolved.value,
        source=source,
        message=message,
        code=str(code) if code is not None else None,
        details=registry_details,
    )
    _write_stdout(record)
    _write_logfile(record)

    profile = meta.get("profile") if isinstance(meta, Mapping) else None
    routes = should_route(resolved, profile=str(profile) if profile is not None else None)
    if not routes.get("telegram"):
        return

    token = os.getenv("ALERTS_TG_BOT_TOKEN", "")
    chat_id = os.getenv("ALERTS_TG_CHAT_ID", "")
    if not token or not chat_id:
        return

    timeout_raw = os.getenv("ALERTS_TG_TIMEOUT_SEC", "5")
    try:
        timeout = float(timeout_raw)
    except ValueError:
        timeout = 5.0

    text = _format_telegram(resolved, message, serialisable_meta)
    try:
        wire_telegram.send_message(
            token=token,
            chat_id=chat_id,
            text=text,
            timeout=timeout,
            extra={"disable_web_page_preview": "true"},
        )
    except wire_telegram.TelegramWireError:
        pass


__all__ = ["notify"]
