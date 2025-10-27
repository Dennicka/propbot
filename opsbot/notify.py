"""Helpers for dispatching operator alerts."""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping

logger = logging.getLogger(__name__)

_ALERT_LIMIT = 500
_ALERT_LOCK = threading.Lock()
_OPS_BOT: "OpsBotSender | None" = None


class OpsBotSender:  # pragma: no cover - typing helper
    """Protocol-like base class for sending alerts to the operator bot."""

    def publish_alert(self, text: str) -> None:  # pragma: no cover - documentation only
        raise NotImplementedError


def set_ops_bot(bot: OpsBotSender | None) -> None:
    """Register the operations bot instance used for alert delivery."""

    global _OPS_BOT
    _OPS_BOT = bot


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _alerts_path() -> Path:
    override = os.getenv("OPS_ALERTS_PATH")
    if override:
        return Path(override)
    return Path("data/ops_alerts.json")


def _load_alerts(path: Path) -> list[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except FileNotFoundError:
        return []
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    entries: list[dict[str, Any]] = []
    for entry in payload:
        if isinstance(entry, Mapping):
            entries.append(dict(entry))
    return entries


def _write_alerts(path: Path, entries: Iterable[Mapping[str, Any]]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(list(entries), handle, indent=2, sort_keys=True)
    except OSError:
        logger.debug("Failed to persist ops alerts", exc_info=True)


def _coerce_details(details: Dict[str, Any] | str | Any) -> Dict[str, Any] | str:
    if isinstance(details, Mapping):
        return {str(key): value for key, value in details.items()}
    if isinstance(details, MutableMapping):  # pragma: no cover - defensive
        return {str(key): value for key, value in details.items()}
    return str(details)


def read_alerts(*, limit: int | None = None) -> list[dict[str, Any]]:
    """Return the most recent operator alerts."""

    entries = _load_alerts(_alerts_path())
    if limit is None or limit <= 0:
        return entries
    return entries[-limit:]


def _format_stats_line(details: Mapping[str, Any]) -> str | None:
    stats: list[str] = []
    notional = details.get("notional_usdt")
    if notional is not None:
        try:
            stats.append(f"notional={float(notional):.2f}USDT")
        except (TypeError, ValueError):
            stats.append(f"notional={notional}")
    leverage = details.get("leverage")
    if leverage is not None:
        try:
            stats.append(f"leverage={float(leverage):.2f}x")
        except (TypeError, ValueError):
            stats.append(f"leverage={leverage}")
    spread = details.get("spread_bps") or details.get("spread")
    if spread is not None:
        try:
            stats.append(f"spread={float(spread):.2f}bps")
        except (TypeError, ValueError):
            stats.append(f"spread={spread}")
    if not stats:
        return None
    return ", ".join(stats)


def format_alert_message(event: str, details: Dict[str, Any] | str | Any) -> str:
    """Build a human readable Telegram message for an ops alert."""

    lines = [f"[{event}]", _ts()]
    if isinstance(details, Mapping):
        symbol = details.get("symbol")
        long_venue = details.get("long_venue") or details.get("cheap_exchange")
        short_venue = details.get("short_venue") or details.get("expensive_exchange")
        venues_line: list[str] = []
        if symbol:
            venues_line.append(str(symbol).upper())
        if long_venue or short_venue:
            venues_line.append(f"{long_venue or '?'} ↔ {short_venue or '?'}")
        if venues_line:
            lines.append(" ".join(venues_line))
        stats_line = _format_stats_line(details)
        if stats_line:
            lines.append(stats_line)
        reason = details.get("reason") or details.get("status")
        if reason:
            lines.append(f"reason: {reason}")
        note = details.get("note")
        if note:
            lines.append(f"note: {note}")
        extra = details.get("details")
        if extra and extra != reason:
            lines.append(str(extra))
    else:
        lines.append(str(details))
    return "\n".join(line for line in lines if line)


def notify_ops(event: str, details: Dict[str, Any] | str | Any) -> dict[str, Any]:
    """Record and broadcast an operator alert."""

    payload = _coerce_details(details)
    record: dict[str, Any] = {
        "ts": _ts(),
        "event": event,
        "details": payload,
    }
    record["message"] = format_alert_message(event, payload)

    try:
        path = _alerts_path()
        with _ALERT_LOCK:
            entries = _load_alerts(path)
            entries.append(record)
            if len(entries) > _ALERT_LIMIT:
                entries = entries[-_ALERT_LIMIT:]
            _write_alerts(path, entries)
    except Exception:  # pragma: no cover - logging only
        logger.exception("Failed to persist ops alert")

    bot = _OPS_BOT
    if bot is None:
        return record
    try:
        bot.publish_alert(record["message"])
    except Exception:  # pragma: no cover - log but do not raise
        logger.exception("Failed to dispatch ops alert")
    return record


__all__ = ["notify_ops", "read_alerts", "set_ops_bot", "format_alert_message"]
