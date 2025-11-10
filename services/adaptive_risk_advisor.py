"""Adaptive risk advisor that produces manual suggestions for operators."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass
from statistics import mean
from typing import Mapping, MutableMapping, Sequence

from app.services import risk_guard

_DEFAULT_SNAPSHOT_WINDOW = 5
_POSITIVE_PNL_BUFFER = 0.0
_EXPOSURE_SAFE_RATIO = 0.7
_LOOSEN_PCT = 0.10
_TIGHTEN_PCT = 0.20


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _as_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: object, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class RiskAdvisorConfig:
    window: int = _DEFAULT_SNAPSHOT_WINDOW
    loosen_pct: float = _LOOSEN_PCT
    tighten_pct: float = _TIGHTEN_PCT
    exposure_safe_ratio: float = _EXPOSURE_SAFE_RATIO


def _current_limits(overrides: Mapping[str, object] | None = None) -> dict[str, float | int]:
    payload: MutableMapping[str, float | int] = {
        "MAX_TOTAL_NOTIONAL_USDT": _env_float("MAX_TOTAL_NOTIONAL_USDT", 150_000.0),
        "MAX_OPEN_POSITIONS": _env_int("MAX_OPEN_POSITIONS", 3),
    }
    if overrides:
        for key, value in overrides.items():
            if key.upper() == "MAX_TOTAL_NOTIONAL_USDT":
                payload["MAX_TOTAL_NOTIONAL_USDT"] = _as_float(
                    value, payload["MAX_TOTAL_NOTIONAL_USDT"]
                )
            elif key.upper() == "MAX_OPEN_POSITIONS":
                payload["MAX_OPEN_POSITIONS"] = _as_int(value, payload["MAX_OPEN_POSITIONS"])
    return dict(payload)


def _auto_throttle_recent(
    *,
    hold_info: Mapping[str, object] | None,
    risk_throttled: bool | None,
) -> bool:
    if risk_throttled:
        return True
    if not hold_info:
        return False
    reason = str(hold_info.get("hold_reason") or "").upper()
    if reason.startswith(risk_guard.AUTO_THROTTLE_PREFIX):
        return True
    released_reason = str(hold_info.get("last_hold_reason") or "").upper()
    if released_reason.startswith(risk_guard.AUTO_THROTTLE_PREFIX):
        return True
    return False


def _partial_ratio(snapshots: Sequence[Mapping[str, object]]) -> float:
    partial_counts = [_as_float(row.get("partial_positions"), 0.0) for row in snapshots]
    open_counts = [_as_float(row.get("open_positions"), 0.0) for row in snapshots]
    partial_avg = mean(partial_counts) if partial_counts else 0.0
    open_avg = mean(open_counts) if open_counts else 0.0
    if open_avg <= 0.0:
        return partial_avg
    return partial_avg / max(open_avg, 1e-9)


def generate_risk_advice(
    snapshots: Sequence[Mapping[str, object]],
    *,
    current_limits: Mapping[str, object] | None = None,
    hold_info: Mapping[str, object] | None = None,
    dry_run_mode: bool | None = None,
    risk_throttled: bool | None = None,
    config: RiskAdvisorConfig | None = None,
) -> dict[str, object]:
    cfg = config or RiskAdvisorConfig()
    if cfg.window <= 0:
        windowed: list[Mapping[str, object]] = [
            row for row in snapshots if isinstance(row, Mapping)
        ]
    else:
        windowed = [row for row in snapshots[: cfg.window] if isinstance(row, Mapping)]
    limits = _current_limits(current_limits)
    max_notional = _as_float(limits.get("MAX_TOTAL_NOTIONAL_USDT"), 0.0)
    max_positions = _as_int(limits.get("MAX_OPEN_POSITIONS"), 0)

    base_response: dict[str, object] = {
        "current_max_notional": max_notional,
        "current_max_positions": max_positions,
        "suggested_max_notional": max_notional,
        "suggested_max_positions": max_positions,
        "recommend_dry_run_mode": bool(dry_run_mode),
        "analysis_window": len(windowed),
        "recommendation": "maintain",
        "reason": (
            "Insufficient data to adjust limits."
            if not windowed
            else "Signals mixed; keep limits unchanged for now."
        ),
    }

    if not windowed:
        return base_response

    pnl_series = [_as_float(row.get("unrealized_pnl_total"), 0.0) for row in windowed]
    exposure_series = [_as_float(row.get("total_exposure_usd_total"), 0.0) for row in windowed]
    partial_ratio_value = _partial_ratio(windowed)

    auto_throttle_recent = _auto_throttle_recent(hold_info=hold_info, risk_throttled=risk_throttled)

    pnl_mean = mean(pnl_series) if pnl_series else 0.0
    pnl_first = pnl_series[0] if pnl_series else 0.0
    pnl_last = pnl_series[-1] if pnl_series else 0.0
    pnl_min = min(pnl_series) if pnl_series else 0.0

    exposure_peak = max(exposure_series) if exposure_series else 0.0
    exposure_ratio = (exposure_peak / max_notional) if max_notional > 0 else 0.0

    positive_trend = pnl_min > _POSITIVE_PNL_BUFFER and exposure_ratio < cfg.exposure_safe_ratio

    negative_trend = (
        pnl_last < pnl_first
        and pnl_mean < 0.0
        and pnl_last < 0.0
        and partial_ratio_value >= 0.5
        and auto_throttle_recent
    )

    if negative_trend:
        tightened_notional = max_notional
        if max_notional > 0:
            tightened_notional = round(max_notional * (1.0 - cfg.tighten_pct), 2)
        tightened_positions = max_positions
        if max_positions > 0:
            tightened_positions = max(1, math.floor(max_positions * (1.0 - cfg.tighten_pct)))
            if tightened_positions >= max_positions:
                tightened_positions = max(max_positions - 1, 1)
        base_response.update(
            {
                "suggested_max_notional": tightened_notional,
                "suggested_max_positions": tightened_positions,
                "recommend_dry_run_mode": True,
                "recommendation": "tighten",
                "reason": (
                    "Negative unrealised PnL trend with outstanding partial hedges and recent auto-"
                    "throttle HOLD detected. Recommend tightening caps by ~20% and keeping DRY_RUN_MODE engaged until conditions improve."
                ),
            }
        )
        return base_response

    if positive_trend:
        loosened_notional = max_notional
        if max_notional > 0:
            loosened_notional = round(max_notional * (1.0 + cfg.loosen_pct), 2)
        loosened_positions = max_positions
        if max_positions > 0:
            loosened_positions = max(
                max_positions + 1, math.ceil(max_positions * (1.0 + cfg.loosen_pct))
            )
        base_response.update(
            {
                "suggested_max_notional": loosened_notional,
                "suggested_max_positions": loosened_positions,
                "recommend_dry_run_mode": bool(dry_run_mode),
                "recommendation": "loosen",
                "reason": (
                    f"Unrealised PnL stayed positive across the last {len(windowed)} snapshots while exposure remained well below the cap. "
                    "Manual review can consider loosening limits by ~10% via the existing approval flow."
                ),
            }
        )
        return base_response

    base_response["reason"] = (
        "Signals are mixed (trend, exposure, or hedge quality), so keep limits unchanged and reassess with more data."
    )
    return base_response


__all__ = ["RiskAdvisorConfig", "generate_risk_advice", "_DEFAULT_SNAPSHOT_WINDOW"]
