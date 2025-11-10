"""Optional operations notifier with Telegram support and audit log."""

from __future__ import annotations

import json
import logging
import os
import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, Iterable, List, Mapping, MutableSequence, Optional

import requests
from fastapi import FastAPI

LOGGER = logging.getLogger(__name__)

_DEFAULT_MAX_RECORDS = 500
_QUEUE: Deque[Dict[str, object]] = deque()
_QUEUE_EVENT = threading.Event()
_STOP_EVENT = threading.Event()
_WORKER: threading.Thread | None = None
_LOCK = threading.Lock()


@dataclass
class TelegramConfig:
    enabled: bool
    token: str | None
    chat_id: str | None
    timeout: float = 10.0

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        enabled = _env_flag("TELEGRAM_ENABLE", False)
        token = os.getenv("TELEGRAM_BOT_TOKEN")
        chat_id = os.getenv("TELEGRAM_CHAT_ID")
        return cls(enabled=enabled, token=token, chat_id=chat_id)

    @property
    def is_ready(self) -> bool:
        return self.enabled and bool(self.token) and bool(self.chat_id)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(str(raw))
    except ValueError:
        return default


def _data_dir() -> Path:
    override = os.getenv("OPS_ALERTS_DIR")
    if override:
        path = Path(override)
    else:
        path = Path(__file__).resolve().parents[2] / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _alerts_path() -> Path:
    override = os.getenv("OPS_ALERTS_FILE")
    if override:
        path = Path(override)
    else:
        path = _data_dir() / "ops_alerts.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _max_records() -> int:
    limit = _env_int("OPS_ALERTS_MAX_RECORDS", _DEFAULT_MAX_RECORDS)
    return max(1, min(limit, 10_000))


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_alerts(path: Path) -> List[Dict[str, object]]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError:
        LOGGER.warning("invalid alerts JSON at %s; resetting", path)
        return []
    except OSError:
        return []
    if isinstance(payload, list):
        result: List[Dict[str, object]] = []
        for entry in payload:
            if isinstance(entry, Mapping):
                result.append(dict(entry))
        return result
    return []


def _write_alerts(path: Path, alerts: Iterable[Mapping[str, object]]) -> None:
    snapshot = [dict(entry) for entry in alerts]
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
    except OSError as exc:  # pragma: no cover - disk errors are logged
        LOGGER.warning("failed to persist alerts: %s", exc)


def _trim_records(collection: MutableSequence[Mapping[str, object]], limit: int) -> None:
    while len(collection) > limit:
        del collection[0]


def emit_alert(
    kind: str,
    text: str,
    *,
    extra: Mapping[str, object] | None = None,
    active: bool | None = None,
    alert_id: str | None = None,
) -> Dict[str, object]:
    """Append an ops alert and enqueue Telegram notification if enabled."""

    record: Dict[str, object] = {"ts": _timestamp(), "kind": kind, "text": text}
    if extra:
        record["extra"] = dict(extra)
    if active is not None:
        record["active"] = bool(active)
    if alert_id:
        record["alert_id"] = alert_id

    path = _alerts_path()
    with _LOCK:
        alerts = _load_alerts(path)
        alerts.append(record)
        _trim_records(alerts, _max_records())
        _write_alerts(path, alerts)

    _enqueue_telegram(record)
    return record


def alert_ops(
    text: str,
    *,
    kind: str = "ops_alert",
    extra: Mapping[str, object] | None = None,
) -> Dict[str, object]:
    """Lightweight helper for operator-facing alerts."""

    return emit_alert(kind=kind, text=text, extra=extra or None)


def alert_slo_breach(text: str, *, extra: Mapping[str, object] | None = None) -> Dict[str, object]:
    """Emit a dedicated SLO breach alert."""

    return emit_alert("slo_breach", text, extra=extra or None)


def _enqueue_telegram(record: Mapping[str, object]) -> None:
    config = TelegramConfig.from_env()
    if not config.is_ready:
        return
    _QUEUE.append(dict(record))
    _QUEUE_EVENT.set()


def send_watchdog_alert(
    exchange: str,
    reason: str,
    mode: str = "AUTO_HOLD",
    *,
    status_text: str = "HOLD active; trading paused",
    timestamp: Optional[str] = None,
    hold_reason: Optional[str] = None,
    context: Optional[str] = None,
) -> Dict[str, object]:
    """Emit a structured watchdog alert to the ops channel and Telegram."""

    exchange_label = str(exchange or "").strip() or "exchange"
    reason_label = str(reason or "").strip() or "unknown"
    headline = f"[ALERT] Exchange watchdog triggered {mode}".strip()
    lines = [
        headline,
        f"Exchange: {exchange_label}",
        f"Reason: {reason_label}",
        f"Status: {status_text}",
    ]
    text = "\n".join(lines)
    extra: Dict[str, object] = {
        "exchange": exchange_label,
        "reason": reason_label,
        "mode": mode,
        "status": "hold_active",
        "status_text": status_text,
        "hold_active": True,
        "actor": "system",
        "initiated_by": "system",
    }
    if timestamp:
        extra["timestamp"] = timestamp
    if hold_reason:
        extra["hold_reason"] = hold_reason
    if context:
        extra["context"] = context
    return emit_alert("watchdog_alert", text, extra=extra)


def _telegram_url(config: TelegramConfig) -> str:
    token = config.token or ""
    return f"https://api.telegram.org/bot{token}/sendMessage"


def _worker_loop(config: TelegramConfig) -> None:
    LOGGER.info("ops notifier worker started")
    try:
        while not _STOP_EVENT.is_set():
            triggered = _QUEUE_EVENT.wait(timeout=5.0)
            if not triggered and not _QUEUE:
                continue
            while _QUEUE:
                record = _QUEUE.popleft()
                _send_telegram(config, record)
            _QUEUE_EVENT.clear()
    finally:
        LOGGER.info("ops notifier worker stopped")


def _send_telegram(config: TelegramConfig, record: Mapping[str, object]) -> None:
    try:
        text = str(record.get("text") or "")
        if not text:
            return
        payload = {
            "chat_id": config.chat_id,
            "text": text,
        }
        response = requests.post(
            _telegram_url(config),
            json=payload,
            timeout=config.timeout,
        )
        if response.status_code >= 400:
            LOGGER.warning(
                "telegram notification failed: %s %s", response.status_code, response.text
            )
    except Exception as exc:  # pragma: no cover - defensive logging
        LOGGER.warning("telegram notification error: %s", exc)


def start_worker() -> None:
    global _WORKER
    config = TelegramConfig.from_env()
    if not config.is_ready:
        LOGGER.info("ops notifier disabled or missing credentials; worker not started")
        return
    if _WORKER and _WORKER.is_alive():
        return
    _STOP_EVENT.clear()
    _QUEUE_EVENT.clear()
    _WORKER = threading.Thread(
        target=_worker_loop,
        name="ops-telegram-notifier",
        daemon=True,
        args=(config,),
    )
    _WORKER.start()


def stop_worker() -> None:
    global _WORKER
    if not _WORKER:
        return
    _STOP_EVENT.set()
    _QUEUE_EVENT.set()
    _WORKER.join(timeout=5.0)
    _WORKER = None


def get_recent_alerts(*, limit: int = 100, since: str | None = None) -> List[Dict[str, object]]:
    path = _alerts_path()
    alerts = _load_alerts(path)
    since_ts = _parse_timestamp(since) if since else None
    if since_ts is not None:
        filtered = []
        for entry in alerts:
            entry_ts = _parse_timestamp(str(entry.get("ts") or ""))
            if entry_ts is None:
                continue
            if entry_ts >= since_ts:
                filtered.append(entry)
        alerts = filtered
    if limit > 0:
        alerts = alerts[-limit:]
    return list(reversed(alerts))


def read_audit_events(limit: int | None = None) -> List[Dict[str, object]]:
    """Return the most recent audit entries without reversing order."""

    path = _alerts_path()
    alerts = _load_alerts(path)
    if limit is not None and limit > 0:
        alerts = alerts[-int(limit) :]
    return [dict(entry) for entry in alerts]


def _parse_timestamp(raw: str | None) -> datetime | None:
    if not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def setup_notifier(app: FastAPI) -> None:
    @app.on_event("startup")
    async def _start_notifier() -> None:  # pragma: no cover - exercised in integration
        start_worker()

    @app.on_event("shutdown")
    async def _stop_notifier() -> None:  # pragma: no cover - exercised in integration
        stop_worker()


__all__ = [
    "alert_ops",
    "emit_alert",
    "send_watchdog_alert",
    "get_recent_alerts",
    "setup_notifier",
    "start_worker",
    "stop_worker",
]
