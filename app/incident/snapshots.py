from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, RootModel, ValidationError

from ..exchange_watchdog import get_exchange_watchdog
from ..audit_log import log_operator_action
from ..services.runtime import (
    apply_control_snapshot,
    apply_risk_limits_snapshot,
    get_state,
    set_mode,
)
from ..strategy_budget import get_strategy_budget_manager
from ..utils.operators import resolve_operator_identity
from positions import list_open_positions


LOGGER = logging.getLogger(__name__)

SNAPSHOT_LIMIT = 50
SNAPSHOT_VERSION = 1
SNAPSHOT_DIR = Path(__file__).resolve().parents[2] / "data" / "snapshots"
INCIDENT_MODE_ENABLED = os.getenv("INCIDENT_MODE_ENABLED", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CRITICAL_ACTION_INCIDENT_ROLLBACK = "incident_rollback"


class ControlSnapshotModel(BaseModel):
    mode: str = Field(pattern=r"^(RUN|HOLD)$")
    safe_mode: bool
    dry_run: bool
    dry_run_mode: bool
    two_man_rule: bool
    auto_loop: bool
    loop_pair: str | None = None
    loop_venues: list[str] = Field(default_factory=list)
    order_notional_usdt: float
    max_slippage_bps: int
    min_spread_bps: float
    poll_interval_sec: int
    post_only: bool
    reduce_only: bool
    taker_fee_bps_binance: int
    taker_fee_bps_okx: int
    approvals: dict[str, str] = Field(default_factory=dict)
    preflight_passed: bool | None = None
    last_preflight_ts: str | None = None
    deployment_mode: str | None = None
    environment: str | None = None

    model_config = {
        "extra": "ignore",
    }


class RiskLimitsModel(BaseModel):
    max_position_usdt: dict[str, float] = Field(default_factory=dict)
    max_open_orders: dict[str, int] = Field(default_factory=dict)
    max_daily_loss_usdt: float | None = None

    model_config = {"extra": "ignore"}


class BudgetEntryModel(BaseModel):
    max_notional_usdt: float | None = None
    max_open_positions: int | None = None
    current_notional_usdt: float = 0.0
    current_open_positions: int = 0
    blocked: bool | None = None

    model_config = {"extra": "ignore"}


class BudgetsModel(RootModel[dict[str, BudgetEntryModel]]):
    def as_mapping(self) -> dict[str, dict[str, float | int | None]]:
        return {name: entry.model_dump() for name, entry in self.root.items()}


class OpenTradesModel(BaseModel):
    count: int = Field(ge=0)
    max_limit: int | None = Field(default=None, ge=0)

    model_config = {"extra": "ignore"}


class WatchdogModel(BaseModel):
    overall_ok: bool | None = None
    exchanges: dict[str, dict[str, Any]] = Field(default_factory=dict)

    model_config = {"extra": "ignore"}


class IncidentSnapshotModel(BaseModel):
    version: int = Field(default=SNAPSHOT_VERSION)
    created_ts: str
    note: str | None = None
    control: ControlSnapshotModel
    feature_flags: dict[str, Any] = Field(default_factory=dict)
    risk_limits: RiskLimitsModel
    budgets: BudgetsModel
    open_trades: OpenTradesModel
    watchdog: WatchdogModel

    model_config = {"extra": "allow"}


def _ensure_enabled() -> None:
    if not INCIDENT_MODE_ENABLED:
        raise RuntimeError("incident_mode_disabled")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _cleanup_old_snapshots(directory: Path) -> None:
    try:
        entries = [p for p in directory.glob("*.json") if p.is_file()]
    except OSError:
        return
    if len(entries) <= SNAPSHOT_LIMIT:
        return
    entries.sort(key=_snapshot_mtime, reverse=True)
    for stale in entries[SNAPSHOT_LIMIT:]:
        try:
            stale.unlink()
        except OSError:
            continue


def _normalise_note(note: str | None) -> str | None:
    if note is None:
        return None
    cleaned = note.strip()
    return cleaned or None


def _resolve_snapshot_path() -> Path:
    directory = SNAPSHOT_DIR
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        LOGGER.warning(
            "incident snapshot directory creation failed path=%s error=%s", directory, exc
        )
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    unique = uuid.uuid4().hex[:8]
    return directory / f"incident_{ts}_{unique}.json"


def _snapshot_mtime(entry: Path) -> float:
    try:
        return entry.stat().st_mtime
    except OSError:
        return 0.0


def save_snapshot(*, note: str | None = None, token: str | None = None) -> Path:
    """Persist the current runtime state to a JSON snapshot."""

    _ensure_enabled()
    state = get_state()
    control_dict = asdict(state.control)
    control_payload = {
        key: control_dict.get(key) for key in ControlSnapshotModel.model_fields.keys()
    }
    control_payload["mode"] = state.control.mode
    control_payload["loop_venues"] = list(state.control.loop_venues)
    control_payload["approvals"] = dict(state.control.approvals)
    risk_payload = state.risk.limits.as_dict()
    manager = get_strategy_budget_manager()
    budgets_payload = manager.snapshot()
    open_positions = list_open_positions()
    open_trades_payload = {
        "count": len(open_positions),
        "max_limit": _env_int("MAX_OPEN_POSITIONS", 0) or None,
    }
    watchdog = get_exchange_watchdog()
    watchdog_payload = {
        "overall_ok": watchdog.overall_ok(),
        "exchanges": watchdog.get_state(),
    }
    snapshot = IncidentSnapshotModel(
        version=SNAPSHOT_VERSION,
        created_ts=datetime.now(timezone.utc).isoformat(),
        note=_normalise_note(note),
        control=control_payload,
        feature_flags=dict(state.control.flags),
        risk_limits=risk_payload,
        budgets={name: entry for name, entry in budgets_payload.items()},
        open_trades=open_trades_payload,
        watchdog=watchdog_payload,
    )
    path = _resolve_snapshot_path()
    try:
        with path.open("w", encoding="utf-8") as handle:
            json.dump(snapshot.model_dump(mode="json"), handle, indent=2, sort_keys=True)
    except OSError as exc:
        raise RuntimeError("snapshot_write_failed") from exc
    _cleanup_old_snapshots(path.parent)
    identity = resolve_operator_identity(token or "") if token else None
    if identity:
        operator, role = identity
        log_operator_action(
            operator,
            role,
            "INCIDENT_SNAPSHOT_SAVE",
            details={"path": str(path), "note": snapshot.note},
        )
    return path


def _coerce_snapshot_path(path: str | os.PathLike[str]) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = SNAPSHOT_DIR / candidate
    resolved = candidate.resolve()
    directory = SNAPSHOT_DIR.resolve()
    if directory not in resolved.parents and resolved != directory:
        raise RuntimeError("snapshot_outside_directory")
    if not resolved.exists() or not resolved.is_file():
        raise RuntimeError("snapshot_missing")
    return resolved


def load_snapshot(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Load ``path`` and apply the runtime state stored within."""

    _ensure_enabled()
    snapshot_path = _coerce_snapshot_path(path)
    try:
        with snapshot_path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except json.JSONDecodeError as exc:
        raise RuntimeError("snapshot_invalid_json") from exc
    except OSError as exc:
        raise RuntimeError("snapshot_read_failed") from exc
    try:
        snapshot = IncidentSnapshotModel.model_validate(payload)
    except ValidationError as exc:
        raise RuntimeError("snapshot_invalid_schema") from exc
    control_data = snapshot.control.model_dump()
    set_mode(control_data.get("mode", "HOLD"))
    apply_control_snapshot(control_data)
    apply_risk_limits_snapshot(snapshot.risk_limits.model_dump())
    manager = get_strategy_budget_manager()
    manager.apply_snapshot(snapshot.budgets.as_mapping())
    watchdog = get_exchange_watchdog()
    if snapshot.watchdog.exchanges:
        watchdog.restore_snapshot(snapshot.watchdog.exchanges)
    return snapshot.model_dump(mode="json")
