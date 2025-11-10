from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping

from positions import list_positions

from .capital_manager import get_capital_manager
from .strategy_orchestrator import get_strategy_orchestrator


LOGGER = logging.getLogger(__name__)


def _coerce_float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_pnl_snapshot(positions_snapshot: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a read-only PnL and risk summary for the operator dashboard."""

    snapshot = positions_snapshot or {}
    totals = snapshot.get("totals") if isinstance(snapshot, Mapping) else None
    exposure = snapshot.get("exposure") if isinstance(snapshot, Mapping) else None

    unrealized_pnl = 0.0
    if isinstance(totals, Mapping):
        unrealized_pnl = _coerce_float(totals.get("unrealized_pnl_usdt"))

    total_exposure = 0.0
    if isinstance(exposure, Mapping):
        for payload in exposure.values():
            if not isinstance(payload, Mapping):
                continue
            long_value = _coerce_float(payload.get("long_notional"))
            short_value = _coerce_float(payload.get("short_notional"))
            total_exposure += max(long_value, 0.0) + max(short_value, 0.0)

    manager = get_capital_manager()
    capital_snapshot = manager.snapshot()
    headroom = capital_snapshot.get("headroom")
    if not isinstance(headroom, Mapping):
        headroom = {}

    return {
        "unrealized_pnl_usdt": unrealized_pnl,
        # Realised PnL is aggregated downstream via the daily reporter.
        "realised_pnl_today_usdt": 0.0,
        "total_exposure_usdt": total_exposure,
        "capital_headroom_per_strategy": dict(headroom),
        "capital_snapshot": capital_snapshot,
    }


_OPEN_STATUSES = {"open", "partial"}


def _ensure_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.error(
            "pnl_report.ensure_directory_failed",
            extra={"path": str(path)},
            exc_info=exc,
        )
        raise


def _iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_timestamp_date(timestamp: str | None) -> str:
    if not timestamp:
        return datetime.now(timezone.utc).date().isoformat()
    try:
        normalised = str(timestamp).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalised)
    except ValueError:
        return datetime.now(timezone.utc).date().isoformat()
    return parsed.astimezone(timezone.utc).date().isoformat()


def _resolve_leg_notional(payload: Mapping[str, Any]) -> float:
    notional = _coerce_float(payload.get("notional_usdt"))
    if notional > 0.0:
        return abs(notional)
    entry_price = _coerce_float(payload.get("entry_price"))
    base_size = _coerce_float(payload.get("base_size"))
    return abs(entry_price * base_size)


def _aggregate_leg_exposure(
    *,
    venue: str,
    side: str,
    notional: float,
    target: MutableMapping[str, dict[str, float]],
) -> None:
    if not venue or notional <= 0.0:
        return
    entry = target.setdefault(
        venue,
        {"long_notional": 0.0, "short_notional": 0.0, "net_usdt": 0.0},
    )
    if side == "short":
        entry["short_notional"] += notional
        entry["net_usdt"] -= notional
    else:
        entry["long_notional"] += notional
        entry["net_usdt"] += notional


def _normalise_positions_snapshot(
    positions: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    exposure: dict[str, dict[str, float]] = {}
    unrealised_total = 0.0
    if not positions:
        return {"positions": [], "exposure": exposure, "totals": {"unrealized_pnl_usdt": 0.0}}
    for entry in positions:
        if not isinstance(entry, Mapping):
            continue
        status = str(entry.get("status") or "").lower()
        if status not in _OPEN_STATUSES:
            continue
        if bool(entry.get("simulated")):
            continue
        legs = entry.get("legs")
        legs_iterable = legs if isinstance(legs, Iterable) else None
        leg_unrealised = 0.0
        if legs_iterable:
            for leg in legs_iterable:
                if not isinstance(leg, Mapping):
                    continue
                venue = str(leg.get("venue") or "")
                side = str(leg.get("side") or "").lower()
                notional = _resolve_leg_notional(leg)
                _aggregate_leg_exposure(
                    venue=venue,
                    side=side,
                    notional=notional,
                    target=exposure,
                )
                leg_unrealised += _coerce_float(leg.get("unrealized_pnl_usdt"))
        else:
            notional = abs(_coerce_float(entry.get("notional_usdt")))
            long_venue = str(entry.get("long_venue") or "")
            short_venue = str(entry.get("short_venue") or "")
            if long_venue and notional:
                _aggregate_leg_exposure(
                    venue=long_venue,
                    side="long",
                    notional=notional,
                    target=exposure,
                )
            if short_venue and notional:
                _aggregate_leg_exposure(
                    venue=short_venue,
                    side="short",
                    notional=notional,
                    target=exposure,
                )
        unrealised_value = _coerce_float(entry.get("unrealized_pnl_usdt"))
        if unrealised_value == 0.0 and leg_unrealised != 0.0:
            unrealised_value = leg_unrealised
        unrealised_total += unrealised_value
    return {
        "positions": list(positions),
        "exposure": {key: dict(values) for key, values in exposure.items()},
        "totals": {"unrealized_pnl_usdt": unrealised_total},
    }


@dataclass
class DailyPnLReporter:
    """Collect and persist daily PnL / exposure snapshots for audits."""

    positions_provider: Callable[[], Iterable[Mapping[str, Any]]] = list_positions

    def build_daily_snapshot(self) -> dict[str, Any]:
        """Return a serialisable snapshot of the current risk posture."""

        positions = tuple(self.positions_provider())
        positions_snapshot = _normalise_positions_snapshot(positions)
        pnl_snapshot = build_pnl_snapshot(positions_snapshot)

        capital_manager = get_capital_manager()
        capital_snapshot = capital_manager.snapshot()
        headroom = capital_snapshot.get("headroom")
        if not isinstance(headroom, Mapping):
            headroom = {}

        orchestrator = get_strategy_orchestrator()
        orchestrator_snapshot = orchestrator.snapshot()
        enabled = orchestrator_snapshot.get("enabled_strategies")
        if isinstance(enabled, Iterable) and not isinstance(enabled, (str, bytes)):
            enabled_strategies = [str(name) for name in enabled]
        else:
            enabled_strategies = []

        snapshot = {
            "timestamp": _iso_timestamp(),
            "unrealised_pnl_usdt": _coerce_float(pnl_snapshot.get("unrealized_pnl_usdt")),
            "realised_pnl_today_usdt": 0.0,
            "total_exposure_usdt": _coerce_float(pnl_snapshot.get("total_exposure_usdt")),
            "per_strategy_headroom": {
                str(key): dict(value) for key, value in dict(headroom).items()
            },
            "enabled_strategies": sorted(enabled_strategies),
            "autopilot_active": bool(orchestrator_snapshot.get("autopilot_active")),
        }
        return snapshot

    def write_snapshot_to_file(
        self,
        dir_path: str | Path | None,
        *,
        snapshot: Mapping[str, Any] | None = None,
    ) -> Path:
        """Persist ``snapshot`` to ``dir_path``/``YYYY-MM-DD.json`` and return the path."""

        payload = dict(snapshot or self.build_daily_snapshot())
        directory = Path(dir_path) if dir_path else Path("data/daily_reports")
        _ensure_directory(directory)
        filename = f"{_parse_timestamp_date(str(payload.get('timestamp')))}.json"
        target = directory / filename
        serialisable = json.dumps(payload, indent=2, sort_keys=True)
        tmp_file = directory / f".{filename}.tmp"
        try:
            tmp_file.write_text(serialisable, encoding="utf-8")
            os.replace(tmp_file, target)
        except OSError as exc:
            LOGGER.error(
                "pnl_report.write_snapshot_failed",
                extra={"target": str(target)},
                exc_info=exc,
            )
            raise
        return target
