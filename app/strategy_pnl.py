"""Persistent per-strategy realised PnL tracker.

The rolling seven day figure is derived from the cumulative realised PnL
across the last seven calendar entries that have been recorded. This keeps the
implementation simple while still surfacing short-term drawdown trends.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DEFAULT_STATE_PATH = Path("data/strategy_pnl_state.json")
_STATE_LOCK = threading.RLock()


LOGGER = logging.getLogger(__name__)


def _get_state_path() -> Path:
    override = os.environ.get("STRATEGY_PNL_STATE_PATH")
    if override:
        return Path(override)
    return _DEFAULT_STATE_PATH


def _load_state_unlocked() -> dict[str, dict[str, Any]]:
    path = _get_state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    raw = raw.strip()
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, Mapping):
        return {}
    state: dict[str, dict[str, Any]] = {}
    for key, value in payload.items():
        if not isinstance(value, Mapping):
            continue
        state[str(key)] = _normalise_entry(dict(value))
    return state


def _normalise_entry(entry: dict[str, Any] | None) -> dict[str, Any]:
    data = dict(entry or {})
    data.setdefault("realized_pnl_total", 0.0)
    data.setdefault("realized_pnl_today", 0.0)
    data.setdefault("realized_pnl_7d", 0.0)
    data.setdefault("max_drawdown_observed", 0.0)
    data.setdefault("last_update_ts", 0.0)
    data.setdefault("peak_realized_total", 0.0)
    history = data.get("daily_history")
    if isinstance(history, list):
        cleaned: list[dict[str, Any]] = []
        for item in history:
            if not isinstance(item, Mapping):
                continue
            date_str = str(item.get("date") or "").strip()
            if not date_str:
                continue
            pnl_value = _coerce_float(item.get("pnl"))
            cleaned.append({"date": date_str, "pnl": pnl_value})
        cleaned.sort(key=lambda item: item["date"])
        data["daily_history"] = cleaned
    else:
        data["daily_history"] = []
    return data


def _write_state_unlocked(state: Mapping[str, Mapping[str, Any]]) -> None:
    path = _get_state_path()
    serialisable = {
        name: {
            "realized_pnl_total": entry.get("realized_pnl_total", 0.0),
            "realized_pnl_today": entry.get("realized_pnl_today", 0.0),
            "realized_pnl_7d": entry.get("realized_pnl_7d", 0.0),
            "max_drawdown_observed": entry.get("max_drawdown_observed", 0.0),
            "last_update_ts": entry.get("last_update_ts", 0.0),
            "peak_realized_total": entry.get("peak_realized_total", 0.0),
            "daily_history": list(entry.get("daily_history", [])),
        }
        for name, entry in state.items()
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.debug("strategy_pnl parent creation failed path=%s error=%s", path.parent, exc)
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(serialisable, handle, indent=2, sort_keys=True)
    except OSError as exc:
        LOGGER.debug("strategy_pnl write failed path=%s error=%s", path, exc)


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _ensure_entry(state: dict[str, dict[str, Any]], strategy_name: str) -> dict[str, Any]:
    key = strategy_name.strip()
    if not key:
        raise ValueError("strategy_name must be non-empty")
    if key not in state:
        state[key] = _normalise_entry({})
    return state[key]


def _today_key(now: float | None = None) -> str:
    ts = datetime.fromtimestamp(now or time.time(), tz=timezone.utc)
    return ts.date().isoformat()


def _recompute_windows(entry: dict[str, Any]) -> None:
    history: list[dict[str, Any]] = list(entry.get("daily_history", []))
    history.sort(key=lambda item: item["date"])
    history = history[-14:]
    entry["daily_history"] = history
    last_ts = entry.get("last_update_ts") or time.time()
    today_key = _today_key(last_ts)
    today_value = 0.0
    rolling_total = 0.0
    cutoff_dates = {item["date"] for item in history[-7:]}
    for item in history:
        pnl_value = _coerce_float(item.get("pnl"), 0.0)
        if item["date"] == today_key:
            today_value = pnl_value
        if item["date"] in cutoff_dates:
            rolling_total += pnl_value
    entry["realized_pnl_today"] = today_value
    entry["realized_pnl_7d"] = rolling_total


def _build_snapshot(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "realized_pnl_total": _coerce_float(entry.get("realized_pnl_total", 0.0)),
        "realized_pnl_today": _coerce_float(entry.get("realized_pnl_today", 0.0)),
        "realized_pnl_7d": _coerce_float(entry.get("realized_pnl_7d", 0.0)),
        "max_drawdown_observed": _coerce_float(entry.get("max_drawdown_observed", 0.0)),
        "last_update_ts": _coerce_float(entry.get("last_update_ts", 0.0)),
        "daily_history": [
            {"date": item.get("date"), "pnl": _coerce_float(item.get("pnl", 0.0))}
            for item in entry.get("daily_history", [])
            if isinstance(item, Mapping)
        ],
        "peak_realized_total": _coerce_float(entry.get("peak_realized_total", 0.0)),
    }


def record_fill(strategy_name: str, pnl_delta: float) -> dict[str, Any]:
    """Record a realised PnL delta for ``strategy_name``."""

    with _STATE_LOCK:
        state = _load_state_unlocked()
        entry = _ensure_entry(state, strategy_name)
        delta = _coerce_float(pnl_delta, 0.0)
        entry["realized_pnl_total"] = _coerce_float(entry.get("realized_pnl_total", 0.0)) + delta
        now = time.time()
        entry["last_update_ts"] = now
        today = _today_key(now)
        history: list[dict[str, Any]] = list(entry.get("daily_history", []))
        updated = False
        for item in history:
            if item["date"] == today:
                item["pnl"] = _coerce_float(item.get("pnl", 0.0)) + delta
                updated = True
                break
        if not updated:
            history.append({"date": today, "pnl": delta})
        entry["daily_history"] = history
        peak = _coerce_float(entry.get("peak_realized_total", 0.0))
        total = entry["realized_pnl_total"]
        if total > peak:
            peak = total
        drawdown = max(0.0, peak - total)
        entry["peak_realized_total"] = peak
        entry["max_drawdown_observed"] = max(
            _coerce_float(entry.get("max_drawdown_observed", 0.0)), drawdown
        )
        _recompute_windows(entry)
        _write_state_unlocked(state)
        return _build_snapshot(entry)


def snapshot(strategy_name: str) -> dict[str, Any]:
    with _STATE_LOCK:
        state = _load_state_unlocked()
        entry = _ensure_entry(state, strategy_name)
        _recompute_windows(entry)
        return _build_snapshot(entry)


def snapshot_all() -> dict[str, dict[str, Any]]:
    with _STATE_LOCK:
        state = _load_state_unlocked()
        result: dict[str, dict[str, Any]] = {}
        for name in sorted(state):
            entry = _ensure_entry(state, name)
            _recompute_windows(entry)
            result[name] = _build_snapshot(entry)
        return result


def reset_state_for_tests() -> None:
    with _STATE_LOCK:
        path = _get_state_path()
        try:
            path.unlink()
        except OSError as exc:
            LOGGER.debug("strategy_pnl reset failed path=%s error=%s", path, exc)


__all__ = ["record_fill", "snapshot", "snapshot_all", "reset_state_for_tests"]
