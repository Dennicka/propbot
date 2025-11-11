"""Daily PnL, exposure, and ops-summary reporter."""

from __future__ import annotations

import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pnl_history_store import list_snapshots as list_pnl_snapshots
from positions_store import list_records as list_position_records
from services.execution_stats_store import list_recent as list_recent_execution_stats


LOGGER = logging.getLogger(__name__)

_STORE_ENV = "DAILY_REPORTS_PATH"
_DEFAULT_STORE_PATH = Path("data/daily_reports.json")
_MAX_RECORDS = 180  # keep roughly six months of history

_ALERTS_ENV = "OPS_ALERTS_FILE"
_DEFAULT_ALERTS_PATH = Path("data/ops_alerts.json")

_DEFAULT_LOOKBACK_HOURS = 24


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _reports_path() -> Path:
    override = os.getenv(_STORE_ENV)
    if override:
        return Path(override)
    return _DEFAULT_STORE_PATH


def _alerts_path() -> Path:
    override = os.getenv(_ALERTS_ENV)
    if override:
        return Path(override)
    return _DEFAULT_ALERTS_PATH


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.warning("daily reporter parent creation failed path=%s error=%s", path.parent, exc)


def _load_json_list(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    if not raw.strip():
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    entries: list[dict[str, Any]] = []
    for entry in payload:
        if isinstance(entry, Mapping):
            entries.append({str(key): value for key, value in entry.items()})
    return entries


def _write_json_list(path: Path, entries: Iterable[Mapping[str, Any]]) -> None:
    snapshot = [dict(entry) for entry in entries]
    _ensure_parent(path)
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, indent=2, sort_keys=True)
    except OSError as exc:
        LOGGER.warning("daily reporter write failed path=%s error=%s", path, exc)


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _within_window(ts: datetime | None, *, now: datetime, window: timedelta) -> bool:
    if ts is None:
        return False
    if ts > now:
        return False
    return now - ts <= window


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / float(len(values))


def _percentile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    if quantile <= 0:
        return float(min(values))
    if quantile >= 1:
        return float(max(values))
    sorted_values = sorted(values)
    position = (len(sorted_values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(sorted_values[int(position)])
    lower_value = float(sorted_values[lower])
    upper_value = float(sorted_values[upper])
    weight = position - lower
    return lower_value + (upper_value - lower_value) * weight


def list_reports(*, limit: int | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    target = path or _reports_path()
    entries = _load_json_list(target)
    if limit is not None and limit > 0:
        entries = entries[-limit:]
    return [dict(entry) for entry in entries]


def load_latest_report(*, path: Path | None = None) -> dict[str, Any] | None:
    entries = list_reports(limit=1, path=path)
    if not entries:
        return None
    return dict(entries[-1])


def append_report(
    report: Mapping[str, Any], *, path: Path | None = None, max_records: int = _MAX_RECORDS
) -> dict[str, Any]:
    payload = dict(report)
    payload.setdefault("timestamp", _now().isoformat())
    target = path or _reports_path()
    entries = _load_json_list(target)
    entries.append(payload)
    if max_records and max_records > 0 and len(entries) > max_records:
        entries = entries[-max_records:]
    _write_json_list(target, entries)
    return dict(payload)


def _load_ops_alerts(*, path: Path | None = None) -> list[dict[str, Any]]:
    target = path or _alerts_path()
    return _load_json_list(target)


def build_daily_report(
    *,
    now: datetime | None = None,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    positions: Sequence[Mapping[str, Any]] | None = None,
    pnl_snapshots: Sequence[Mapping[str, Any]] | None = None,
    execution_stats: Sequence[Mapping[str, Any]] | None = None,
    ops_alerts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    current_time = now.astimezone(timezone.utc) if isinstance(now, datetime) else _now()
    window_hours = max(1, int(lookback_hours or _DEFAULT_LOOKBACK_HOURS))
    window = timedelta(hours=window_hours)

    if positions is None:
        positions = list_position_records()
    if pnl_snapshots is None:
        pnl_snapshots = list_pnl_snapshots()
    if execution_stats is None:
        execution_stats = list_recent_execution_stats(limit=250)
    if ops_alerts is None:
        ops_alerts = _load_ops_alerts()

    realized_values: list[float] = []
    for position in positions:
        if not isinstance(position, Mapping):
            continue
        status = str(position.get("status") or "").lower()
        if status != "closed":
            continue
        ts = _parse_ts(position.get("closed_ts") or position.get("timestamp"))
        if not _within_window(ts, now=current_time, window=window):
            continue
        realized_values.append(_coerce_float(position.get("pnl_usdt")))

    snapshot_values: list[Mapping[str, Any]] = []
    for snapshot in pnl_snapshots:
        if not isinstance(snapshot, Mapping):
            continue
        ts = _parse_ts(snapshot.get("timestamp"))
        if not _within_window(ts, now=current_time, window=window):
            continue
        snapshot_values.append(snapshot)

    unrealized_samples: list[float] = []
    exposure_samples: list[float] = []
    for snapshot in snapshot_values:
        totals = (
            snapshot.get("pnl_totals") if isinstance(snapshot.get("pnl_totals"), Mapping) else {}
        )
        if "unrealized" in totals:
            unrealized_samples.append(_coerce_float(totals.get("unrealized")))
        else:
            unrealized_samples.append(_coerce_float(snapshot.get("unrealized_pnl_total")))
        exposure_samples.append(_coerce_float(snapshot.get("total_exposure_usd_total")))

    slippage_samples: list[float] = []
    for stat in execution_stats:
        if not isinstance(stat, Mapping):
            continue
        ts = _parse_ts(stat.get("timestamp"))
        if not _within_window(ts, now=current_time, window=window):
            continue
        slippage = stat.get("slippage_bps")
        if slippage in (None, ""):
            continue
        try:
            slippage_samples.append(float(slippage))
        except (TypeError, ValueError):
            continue

    hold_count = 0
    throttle_count = 0
    for alert in ops_alerts:
        if not isinstance(alert, Mapping):
            continue
        ts = _parse_ts(alert.get("ts") or alert.get("timestamp"))
        if not _within_window(ts, now=current_time, window=window):
            continue
        kind = str(alert.get("kind") or "").lower()
        if kind == "risk_guard_force_hold":
            throttle_count += 1
        elif kind in {"safety_hold", "hold"} or "hold" in kind:
            hold_count += 1

    report = {
        "timestamp": current_time.isoformat(),
        "window_hours": window_hours,
        "pnl_realized_total": sum(realized_values),
        "pnl_realized_count": len(realized_values),
        "pnl_unrealized_avg": _mean(unrealized_samples),
        "pnl_unrealized_latest": unrealized_samples[-1] if unrealized_samples else 0.0,
        "pnl_unrealized_samples": len(unrealized_samples),
        "exposure_avg": _mean(exposure_samples),
        "exposure_max": max(exposure_samples) if exposure_samples else 0.0,
        "exposure_samples": len(exposure_samples),
        "slippage_avg_bps": _mean(slippage_samples) if slippage_samples else None,
        "slippage_p95_bps": _percentile(slippage_samples, 0.95),
        "slippage_samples": len(slippage_samples),
        "hold_events": hold_count + throttle_count,
        "hold_breakdown": {
            "safety_hold": hold_count,
            "risk_throttle": throttle_count,
        },
    }
    return report


def record_daily_report(
    *,
    now: datetime | None = None,
    lookback_hours: int = _DEFAULT_LOOKBACK_HOURS,
    path: Path | None = None,
    max_records: int = _MAX_RECORDS,
) -> dict[str, Any]:
    report = build_daily_report(now=now, lookback_hours=lookback_hours)
    append_report(report, path=path, max_records=max_records)
    return report


__all__ = [
    "append_report",
    "build_daily_report",
    "list_reports",
    "load_latest_report",
    "record_daily_report",
]
