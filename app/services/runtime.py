from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import os
import signal
import time
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import threading
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Mapping, Sequence, Tuple

from ..audit_log import log_operator_action
from .. import ledger
from ..core.config import GuardsConfig, LoadedConfig, load_app_config
from ..exchange_watchdog import get_exchange_watchdog
from ..metrics import set_auto_trade_state, slo
from ..persistence import state_store
from ..runtime import leader_lock
from ..runtime_state_store import (
    load_runtime_payload as _store_load_runtime_payload,
    write_runtime_payload as _store_write_runtime_payload,
)
from . import approvals_store
from .derivatives import DerivativesRuntime, bootstrap_derivatives
from .marketdata import MarketDataAggregator
from ..risk.runaway_guard import get_guard
from ..utils.chaos import (
    ChaosSettings,
    configure as configure_chaos,
    resolve_settings as resolve_chaos_settings,
)


LOGGER = logging.getLogger(__name__)


_SHUTDOWN_LOCK: asyncio.Lock | None = None
_SHUTDOWN_STARTED = False
_LAST_SHUTDOWN_RESULT: Dict[str, object] | None = None
_SIGNAL_LOOP: asyncio.AbstractEventLoop | None = None


DEFAULT_CONFIG_PATHS = {
    "paper": "configs/config.paper.yaml",
    "testnet": "configs/config.testnet.yaml",
    "live": "configs/config.live.yaml",
}


_UNSET = object()


_STATE_LOCK = threading.RLock()


def _resolve_config_path() -> str:
    profile = (
        os.environ.get("PROFILE")
        or os.environ.get("EXCHANGE_PROFILE")
        or os.environ.get("ENVIRONMENT")
        or os.environ.get("ENV")
        or "paper"
    )
    profile_normalised = str(profile).lower()
    return DEFAULT_CONFIG_PATHS.get(profile_normalised, DEFAULT_CONFIG_PATHS["paper"])


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError:
        return default


def _env_optional_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def send_notifier_alert(
    kind: str, text: str, extra: Mapping[str, object] | None = None
) -> None:
    """Send an ops notifier alert while swallowing notifier errors."""

    try:
        from ..opsbot.notifier import emit_alert
    except Exception:
        return
    try:
        emit_alert(kind=kind, text=text, extra=extra or None)
    except Exception:
        pass


def _emit_ops_alert(kind: str, text: str, extra: Mapping[str, object] | None = None) -> None:
    send_notifier_alert(kind, text, extra)


def _env_limit_map(name: str, *, normaliser) -> Dict[str, float]:
    mapping: Dict[str, float] = {}
    base = os.environ.get(name)
    if base is not None:
        try:
            mapping["__default__"] = float(base)
        except ValueError:
            pass
    prefix = f"{name}__"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        scope = normaliser(key[len(prefix) :])
        try:
            mapping[scope] = float(value)
        except ValueError:
            continue
    return mapping


def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()


CRITICAL_ACTION_RESUME = "resume"
CRITICAL_ACTION_RAISE_LIMIT = "raise_limit"
CRITICAL_ACTION_EXIT_DRY_RUN = "exit_dry_run"


@dataclass
class GuardState:
    enabled: bool = True
    status: str = "OK"
    summary: str = "normal"
    metrics: Dict[str, float] = field(default_factory=dict)
    updated_ts: str = field(default_factory=_ts)


@dataclass
class ControlState:
    mode: str = "HOLD"
    safe_mode: bool = True
    two_man_rule: bool = True
    approvals: Dict[str, str] = field(default_factory=dict)
    preflight_passed: bool = False
    last_preflight_ts: str | None = None
    deployment_mode: str = "paper"
    post_only: bool = True
    reduce_only: bool = False
    environment: str = "paper"
    dry_run: bool = True
    dry_run_mode: bool = False
    order_notional_usdt: float = 50.0
    max_slippage_bps: int = 2
    taker_fee_bps_binance: int = 2
    taker_fee_bps_okx: int = 2
    poll_interval_sec: int = 5
    min_spread_bps: float = 0.0
    auto_loop: bool = False
    loop_pair: str | None = None
    loop_venues: List[str] = field(default_factory=list)

    @property
    def flags(self) -> Dict[str, object]:
        return {
            "MODE": self.deployment_mode,
            "SAFE_MODE": self.safe_mode,
            "TWO_MAN_RULE": self.two_man_rule,
            "POST_ONLY": self.post_only,
            "REDUCE_ONLY": self.reduce_only,
            "ENV": self.environment,
            "DRY_RUN": self.dry_run,
            "DRY_RUN_MODE": self.dry_run_mode,
            "ORDER_NOTIONAL_USDT": self.order_notional_usdt,
            "MAX_SLIPPAGE_BPS": self.max_slippage_bps,
            "TAKER_FEE_BPS_BINANCE": self.taker_fee_bps_binance,
            "TAKER_FEE_BPS_OKX": self.taker_fee_bps_okx,
            "POLL_INTERVAL_SEC": self.poll_interval_sec,
            "MIN_SPREAD_BPS": self.min_spread_bps,
            "AUTO_LOOP": self.auto_loop,
            "LOOP_PAIR": self.loop_pair or "",
            "LOOP_VENUES": ",".join(self.loop_venues) if self.loop_venues else "",
        }


@dataclass
class MetricsState:
    slo: Dict[str, float] = field(default_factory=lambda: {
        "ws_gap_ms_p95": 120.0,
        "order_cycle_ms_p95": 180.0,
        "reject_rate": 0.0,
        "cancel_fail_rate": 0.0,
        "recon_mismatch": 0.0,
        "max_day_drawdown_bps": 0.0,
        "budget_remaining": 1_000_000.0,
    })
    counters: Dict[str, float] = field(default_factory=dict)
    latency_samples_ms: List[float] = field(default_factory=list)
    slo_breach_started_at: Dict[str, str] = field(default_factory=dict)


@dataclass
class DryRunState:
    last_cycle_ts: str | None = None
    last_plan: Dict[str, object] | None = None
    last_execution: Dict[str, object] | None = None
    last_error: str | None = None
    last_spread_bps: float | None = None
    last_spread_usdt: float | None = None
    last_fees_usdt: float | None = None
    cycles_completed: int = 0
    poll_interval_sec: int = 5
    min_spread_bps: float = 0.0


@dataclass
class LoopState:
    status: str = "HOLD"
    running: bool = False
    last_cycle_ts: str | None = None
    last_plan: Dict[str, object] | None = None
    last_execution: Dict[str, object] | None = None
    last_error: str | None = None
    cycles_completed: int = 0
    last_spread_bps: float | None = None
    last_spread_usdt: float | None = None
    pair: str | None = None
    venues: List[str] = field(default_factory=list)
    notional_usdt: float | None = None
    last_summary: Dict[str, object] | None = None

    def as_dict(self) -> Dict[str, object | None]:
        return {
            "status": self.status,
            "running": self.running,
            "last_cycle_ts": self.last_cycle_ts,
            "last_plan": self.last_plan,
            "last_execution": self.last_execution,
            "last_error": self.last_error,
            "cycles_completed": self.cycles_completed,
            "last_spread_bps": self.last_spread_bps,
            "last_spread_usdt": self.last_spread_usdt,
            "pair": self.pair,
            "venues": list(self.venues),
            "notional_usdt": self.notional_usdt,
            "last_summary": self.last_summary,
        }


@dataclass
class LoopConfigState:
    pair: str | None = None
    venues: List[str] = field(default_factory=list)
    notional_usdt: float | None = None

    def as_dict(self) -> Dict[str, object | None]:
        return {
            "pair": self.pair,
            "venues": list(self.venues),
            "notional_usdt": self.notional_usdt,
        }


@dataclass
class RiskLimitsState:
    max_position_usdt: Dict[str, float] = field(default_factory=dict)
    max_open_orders: Dict[str, int] = field(default_factory=dict)
    max_daily_loss_usdt: float | None = None

    def as_dict(self) -> Dict[str, object]:
        return {
            "max_position_usdt": dict(self.max_position_usdt),
            "max_open_orders": dict(self.max_open_orders),
            "max_daily_loss_usdt": self.max_daily_loss_usdt,
        }


@dataclass
class RiskCurrentState:
    position_usdt: Dict[str, float] = field(default_factory=dict)
    open_orders: Dict[str, int] = field(default_factory=dict)
    daily_loss_usdt: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "position_usdt": dict(self.position_usdt),
            "open_orders": dict(self.open_orders),
            "daily_loss_usdt": self.daily_loss_usdt,
        }


@dataclass
class RiskBreach:
    limit: str
    scope: str
    current: float
    threshold: float
    detail: str | None = None

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "limit": self.limit,
            "scope": self.scope,
            "current": self.current,
            "threshold": self.threshold,
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload


@dataclass
class RiskState:
    limits: RiskLimitsState = field(default_factory=RiskLimitsState)
    current: RiskCurrentState = field(default_factory=RiskCurrentState)
    breaches: List[RiskBreach] = field(default_factory=list)

    def as_dict(self) -> Dict[str, object]:
        return {
            "limits": self.limits.as_dict(),
            "current": self.current.as_dict(),
            "breaches": [breach.as_dict() for breach in self.breaches],
        }


@dataclass
@dataclass
class OpportunityState:
    opportunity: Dict[str, Any] | None = None
    status: str = "blocked_by_risk"

    def as_dict(self) -> Dict[str, Any | None]:
        return {
            "opportunity": deepcopy(self.opportunity) if self.opportunity else None,
            "status": self.status,
        }


@dataclass
class AutoHedgeState:
    enabled: bool = False
    last_opportunity_checked_ts: str | None = None
    last_execution_result: str | None = "idle"
    last_execution_ts: str | None = None
    consecutive_failures: int = 0
    last_success_ts: str | None = None

    def as_dict(self) -> Dict[str, object | None]:
        return {
            "enabled": self.enabled,
            "last_opportunity_checked_ts": self.last_opportunity_checked_ts,
            "last_execution_result": self.last_execution_result,
            "last_execution_ts": self.last_execution_ts,
            "consecutive_failures": self.consecutive_failures,
            "last_success_ts": self.last_success_ts,
        }


@dataclass
class AutopilotState:
    enabled: bool = False
    last_action: str = "none"
    last_reason: str | None = None
    last_attempt_ts: str | None = None
    armed: bool = False
    target_mode: str = "HOLD"
    target_safe_mode: bool = True
    last_decision: str = "unknown"
    last_decision_reason: str | None = None
    last_decision_ts: str | None = None

    def as_dict(self) -> Dict[str, object | None]:
        return {
            "enabled": self.enabled,
            "last_action": self.last_action,
            "last_reason": self.last_reason,
            "last_attempt_ts": self.last_attempt_ts,
            "armed": self.armed,
            "target_mode": self.target_mode,
            "target_safe_mode": self.target_safe_mode,
            "last_decision": self.last_decision,
            "last_decision_reason": self.last_decision_reason,
            "last_decision_ts": self.last_decision_ts,
        }


@dataclass
class ResumeRequestState:
    reason: str
    requested_by: str | None = None
    requested_ts: str = field(default_factory=_ts)
    request_id: str | None = None
    approved_ts: str | None = None
    approved_by: str | None = None

    def approve(self, *, actor: str | None = None) -> None:
        self.approved_ts = _ts()
        self.approved_by = actor

    def as_dict(self) -> Dict[str, object | None]:
        return {
            "id": self.request_id,
            "reason": self.reason,
            "requested_at": self.requested_ts,
            "requested_by": self.requested_by,
            "approved_at": self.approved_ts,
            "approved_by": self.approved_by,
            "pending": self.approved_ts is None,
        }


@dataclass
class RunawayCounterState:
    orders_placed_last_min: int = 0
    cancels_last_min: int = 0
    window_started_at: float = field(default_factory=time.time)

    def reset_if_needed(self, *, now: float) -> None:
        if now - self.window_started_at >= 60.0:
            self.window_started_at = now
            self.orders_placed_last_min = 0
            self.cancels_last_min = 0

    def as_dict(self) -> Dict[str, int]:
        return {
            "orders_placed_last_min": self.orders_placed_last_min,
            "cancels_last_min": self.cancels_last_min,
        }


@dataclass
class RunawayGuardV2State:
    enabled: bool = False
    max_cancels_per_min: int = 0
    cooldown_sec: int = 0
    last_trigger_ts: str | None = None
    per_venue: Dict[str, Dict[str, int]] = field(default_factory=dict)
    last_block: Dict[str, object] | None = None

    def as_dict(self) -> Dict[str, object | None]:
        payload: Dict[str, object | None] = {
            "enabled": self.enabled,
            "max_cancels_per_min": self.max_cancels_per_min,
            "cooldown_sec": self.cooldown_sec,
            "per_venue": {venue: dict(symbols) for venue, symbols in self.per_venue.items()},
            "last_trigger_ts": self.last_trigger_ts,
        }
        if self.last_block:
            payload["last_block"] = dict(self.last_block)
        return payload

    def update_from_snapshot(self, snapshot: Mapping[str, object]) -> None:
        self.enabled = bool(snapshot.get("enabled", self.enabled))
        max_cancels = snapshot.get("max_cancels_per_min")
        if isinstance(max_cancels, (int, float)):
            self.max_cancels_per_min = max(0, int(float(max_cancels)))
        cooldown = snapshot.get("cooldown_sec")
        if isinstance(cooldown, (int, float)):
            self.cooldown_sec = max(0, int(float(cooldown)))
        per_venue_payload = snapshot.get("per_venue")
        per_venue: Dict[str, Dict[str, int]] = {}
        if isinstance(per_venue_payload, Mapping):
            for venue, symbols in per_venue_payload.items():
                if not isinstance(symbols, Mapping):
                    continue
                per_venue[str(venue)] = {
                    str(symbol): max(0, int(float(value)))
                    for symbol, value in symbols.items()
                    if isinstance(value, (int, float))
                }
        self.per_venue = per_venue
        last_trigger = snapshot.get("last_trigger_ts")
        self.last_trigger_ts = str(last_trigger) if last_trigger else None
        last_block = snapshot.get("last_block")
        if isinstance(last_block, Mapping):
            self.last_block = {str(key): value for key, value in last_block.items()}
        else:
            self.last_block = None


@dataclass
class SafetyLimits:
    max_orders_per_min: int = 300
    max_cancels_per_min: int = 600

    def as_dict(self) -> Dict[str, int]:
        return {
            "max_orders_per_min": self.max_orders_per_min,
            "max_cancels_per_min": self.max_cancels_per_min,
        }


@dataclass
class SafetyState:
    hold_active: bool = False
    hold_reason: str | None = None
    hold_source: str | None = None
    hold_since: str | None = None
    last_released_ts: str | None = None
    resume_request: ResumeRequestState | None = None
    limits: SafetyLimits = field(default_factory=SafetyLimits)
    counters: RunawayCounterState = field(default_factory=RunawayCounterState)
    runaway_guard: RunawayGuardV2State = field(default_factory=RunawayGuardV2State)
    clock_skew_s: float | None = None
    clock_skew_checked_ts: str | None = None
    risk_snapshot: Dict[str, object] = field(default_factory=dict)
    liquidity_blocked: bool = False
    liquidity_reason: str | None = None
    liquidity_snapshot: Dict[str, object] = field(default_factory=dict)
    desync_detected: bool = False
    reconciliation_snapshot: Dict[str, object] = field(default_factory=dict)

    def engage_hold(self, reason: str, *, source: str) -> bool:
        changed = not self.hold_active
        self.hold_active = True
        self.hold_reason = reason
        self.hold_source = source
        self.hold_since = _ts()
        self.last_released_ts = None
        self.resume_request = None
        return changed

    def clear_hold(self) -> bool:
        if not self.hold_active:
            return False
        self.hold_active = False
        self.last_released_ts = _ts()
        return True

    def as_dict(self) -> Dict[str, object | None]:
        payload: Dict[str, object | None] = {
            "hold_active": self.hold_active,
            "hold_reason": self.hold_reason,
            "hold_source": self.hold_source,
            "hold_since": self.hold_since,
            "last_released_ts": self.last_released_ts,
        }
        if self.resume_request:
            payload["resume_request"] = self.resume_request.as_dict()
        payload["clock_skew_s"] = self.clock_skew_s
        payload["clock_skew_checked_ts"] = self.clock_skew_checked_ts
        payload["risk_snapshot"] = dict(self.risk_snapshot)
        payload["liquidity_blocked"] = self.liquidity_blocked
        payload["liquidity_reason"] = self.liquidity_reason
        payload["liquidity_snapshot"] = dict(self.liquidity_snapshot)
        payload["desync_detected"] = bool(self.desync_detected)
        if self.reconciliation_snapshot:
            snapshot = {str(k): v for k, v in self.reconciliation_snapshot.items()}
        else:
            snapshot = {}
        snapshot.setdefault("desync_detected", bool(self.desync_detected))
        issues = snapshot.get("issues")
        if isinstance(issues, Sequence):
            snapshot["issues"] = [dict(issue) for issue in issues if isinstance(issue, Mapping)]
        else:
            snapshot["issues"] = []
        diffs = snapshot.get("diffs")
        if isinstance(diffs, Sequence):
            snapshot["diffs"] = [dict(diff) for diff in diffs if isinstance(diff, Mapping)]
        else:
            snapshot["diffs"] = []
        snapshot.setdefault("issue_count", len(snapshot["issues"]))
        snapshot.setdefault("diff_count", len(snapshot["diffs"]))
        snapshot.setdefault("auto_hold", False)
        payload["reconciliation"] = snapshot
        payload["runaway_guard"] = self.runaway_guard.as_dict()
        return payload

    def status_payload(self) -> Dict[str, object | None]:
        payload = self.as_dict()
        payload["counters"] = self.counters.as_dict()
        payload["limits"] = self.limits.as_dict()
        return payload


@dataclass
class UniverseState:
    unknown_pairs: set[str] = field(default_factory=set)

    def record_unknown(self, pair: str | None) -> None:
        value = str(pair or "").strip().upper()
        if not value:
            return
        self.unknown_pairs.add(value)

    def list_unknown(self) -> list[str]:
        return sorted(self.unknown_pairs)

    def clear(self) -> None:
        self.unknown_pairs.clear()


@dataclass
class RuntimeState:
    config: LoadedConfig
    guards: Dict[str, GuardState]
    control: ControlState
    metrics: MetricsState
    incidents: List[Dict[str, object]] = field(default_factory=list)
    derivatives: DerivativesRuntime | None = None
    market_data: MarketDataAggregator | None = None
    dryrun: DryRunState | None = None
    loop: LoopState = field(default_factory=LoopState)
    loop_config: LoopConfigState = field(default_factory=LoopConfigState)
    open_orders: List[Dict[str, object]] = field(default_factory=list)
    risk: RiskState = field(default_factory=RiskState)
    hedge_positions: List[Dict[str, Any]] = field(default_factory=list)
    last_opportunity: OpportunityState = field(default_factory=OpportunityState)
    auto_hedge: AutoHedgeState = field(default_factory=AutoHedgeState)
    autopilot: AutopilotState = field(default_factory=AutopilotState)
    safety: SafetyState = field(default_factory=SafetyState)
    universe: UniverseState = field(default_factory=UniverseState)
    chaos: ChaosSettings = field(default_factory=ChaosSettings)


def _sync_loop_from_control(state: RuntimeState) -> None:
    loop_config = state.loop_config
    loop_config.pair = state.control.loop_pair
    loop_config.venues = list(state.control.loop_venues)
    loop_config.notional_usdt = state.control.order_notional_usdt
    loop_state = state.loop
    loop_state.pair = state.control.loop_pair
    loop_state.venues = list(state.control.loop_venues)
    loop_state.notional_usdt = state.control.order_notional_usdt


def _init_guards(cfg: LoadedConfig) -> Dict[str, GuardState]:
    guards_cfg: GuardsConfig | None = cfg.data.guards
    defaults = {
        "cancel_on_disconnect": GuardState(enabled=True, summary="connection stable"),
        "rate_limit": GuardState(enabled=True, summary="within limits", metrics={"place_per_min": 0, "cancel_per_min": 0}),
        "clock_skew": GuardState(enabled=True, summary="synced"),
        "snapshot_diff": GuardState(enabled=True, summary="in sync"),
        "kill_caps": GuardState(enabled=True, summary="caps respected"),
        "runaway_breaker": GuardState(enabled=True, summary="stable"),
        "maintenance_calendar": GuardState(enabled=True, summary="no maintenance window"),
    }
    if not guards_cfg:
        return defaults
    defaults["cancel_on_disconnect"].enabled = guards_cfg.cancel_on_disconnect
    defaults["clock_skew"].metrics = {"threshold_ms": guards_cfg.clock_skew_guard_ms}
    defaults["snapshot_diff"].enabled = guards_cfg.snapshot_diff_check
    defaults["kill_caps"].enabled = guards_cfg.kill_caps.enabled
    defaults["runaway_breaker"].metrics = {
        "place_limit": guards_cfg.runaway_breaker.place_per_min,
        "cancel_limit": guards_cfg.runaway_breaker.cancel_per_min,
    }
    defaults["rate_limit"].metrics = {
        "place_limit": guards_cfg.rate_limit.place_per_min,
        "cancel_limit": guards_cfg.rate_limit.cancel_per_min,
        "place_per_min": 0,
        "cancel_per_min": 0,
    }
    defaults["maintenance_calendar"].summary = "no window active" if not guards_cfg.maintenance_calendar else "window configured"
    return defaults


def _bootstrap_runtime() -> RuntimeState:
    config_path = _resolve_config_path()
    loaded = load_app_config(config_path)
    chaos_settings = resolve_chaos_settings(getattr(loaded.data, "chaos", None))
    configure_chaos(chaos_settings)

    control_cfg = loaded.data.control
    safe_mode = _env_flag("SAFE_MODE", control_cfg.safe_mode if control_cfg else True)
    dry_run_only = _env_flag("DRY_RUN_ONLY", control_cfg.dry_run if control_cfg else False)
    order_notional = _env_float("ORDER_NOTIONAL_USDT", 50.0)
    slippage_bps = _env_int("MAX_SLIPPAGE_BPS", 2)
    fee_binance = _env_int("TAKER_FEE_BPS_BINANCE", 2)
    fee_okx = _env_int("TAKER_FEE_BPS_OKX", 2)
    poll_interval = _env_int("POLL_INTERVAL_SEC", 5)
    min_spread_bps = _env_float("MIN_SPREAD_BPS", 0.0)
    dry_run_mode = _env_flag("DRY_RUN_MODE", False)
    profile = (
        os.environ.get("PROFILE")
        or os.environ.get("EXCHANGE_PROFILE")
        or os.environ.get("ENVIRONMENT")
        or os.environ.get("ENV")
        or "paper"
    ).lower()
    environment = os.environ.get("MODE") or os.environ.get("ENVIRONMENT") or os.environ.get("ENV") or profile
    loop_pair_env = os.environ.get("LOOP_PAIR")
    loop_venues_env = os.environ.get("LOOP_VENUES")
    loop_venues = []
    if loop_venues_env:
        loop_venues = [entry.strip() for entry in loop_venues_env.split(",") if entry.strip()]
    control = ControlState(
        mode="HOLD" if safe_mode else "RUN",
        safe_mode=safe_mode,
        two_man_rule=_env_flag("TWO_MAN_RULE", control_cfg.two_man_rule if control_cfg else True),
        deployment_mode=profile,
        post_only=_env_flag("POST_ONLY", control_cfg.post_only if control_cfg else True),
        reduce_only=_env_flag("REDUCE_ONLY", control_cfg.reduce_only if control_cfg else False),
        environment=environment,
        dry_run=dry_run_only,
        dry_run_mode=dry_run_mode,
        order_notional_usdt=order_notional,
        max_slippage_bps=slippage_bps,
        taker_fee_bps_binance=fee_binance,
        taker_fee_bps_okx=fee_okx,
        poll_interval_sec=poll_interval,
        min_spread_bps=min_spread_bps,
        auto_loop=False,
        loop_pair=loop_pair_env.upper() if loop_pair_env else None,
        loop_venues=loop_venues,
    )
    autopilot_enabled = _env_flag("AUTOPILOT_ENABLE", False)
    autopilot_state = AutopilotState(
        enabled=autopilot_enabled,
        target_mode=control.mode,
        target_safe_mode=control.safe_mode,
    )
    guards = _init_guards(loaded)
    metrics = MetricsState()
    derivatives = bootstrap_derivatives(loaded, safe_mode=safe_mode)
    market_data = MarketDataAggregator(stale_after=1.5)
    if derivatives and derivatives.venues:
        for venue_id, venue_rt in derivatives.venues.items():
            symbol_map: Dict[str, str] = {}
            for entry in venue_rt.config.symbols:
                symbol_map[entry.upper()] = entry
                cleaned = entry.replace("-", "").replace("_", "").replace("/", "").upper()
                symbol_map.setdefault(cleaned, entry)
                parts = entry.replace("_", "-").split("-")
                if len(parts) >= 2:
                    pair_key = "".join(parts[:2]).upper()
                    symbol_map.setdefault(pair_key, entry)

            def _fetcher(symbol: str, *, client=venue_rt.client, mapping=symbol_map):
                lookup = symbol.upper()
                target = mapping.get(lookup, symbol)
                return client.get_orderbook_top(target)

            market_data.register_rest_fetcher(venue_id.replace("_", "-"), _fetcher)
    dryrun_state = DryRunState(
        poll_interval_sec=poll_interval,
        min_spread_bps=min_spread_bps,
    )
    position_limits_env = {
        key.upper(): value
        for key, value in _env_limit_map("MAX_POSITION_USDT", normaliser=lambda entry: str(entry).upper()).items()
    }
    open_order_limits_env = {
        key.lower(): int(value)
        for key, value in _env_limit_map("MAX_OPEN_ORDERS", normaliser=lambda entry: str(entry).lower()).items()
    }
    risk_state = RiskState(
        limits=RiskLimitsState(
            max_position_usdt=position_limits_env,
            max_open_orders=open_order_limits_env,
            max_daily_loss_usdt=_env_optional_float("MAX_DAILY_LOSS_USDT"),
        )
    )
    safety_limits = SafetyLimits(
        max_orders_per_min=_env_int("MAX_ORDERS_PER_MIN", 300),
        max_cancels_per_min=_env_int("MAX_CANCELS_PER_MIN", 600),
    )
    runaway_cfg = getattr(getattr(loaded.data, "risk", None), "runaway", None)
    runaway_guard_config = {
        "max_cancels_per_min": getattr(runaway_cfg, "max_cancels_per_min", 0) if runaway_cfg else 0,
        "cooldown_sec": getattr(runaway_cfg, "cooldown_sec", 0) if runaway_cfg else 0,
    }
    guard_instance = get_guard()
    guard_instance.configure(
        max_cancels_per_min=runaway_guard_config["max_cancels_per_min"],
        cooldown_sec=runaway_guard_config["cooldown_sec"],
    )
    runaway_guard_state = RunawayGuardV2State(
        max_cancels_per_min=runaway_guard_config["max_cancels_per_min"],
        cooldown_sec=runaway_guard_config["cooldown_sec"],
        enabled=guard_instance.feature_enabled()
        and runaway_guard_config["max_cancels_per_min"] > 0,
    )
    runaway_guard_state.update_from_snapshot(guard_instance.snapshot())
    state = RuntimeState(
        config=loaded,
        guards=guards,
        control=control,
        metrics=metrics,
        incidents=[],
        derivatives=derivatives,
        dryrun=dryrun_state,
        market_data=market_data,
        loop=LoopState(),
        loop_config=LoopConfigState(
            pair=control.loop_pair,
            venues=list(control.loop_venues),
            notional_usdt=control.order_notional_usdt,
        ),
        open_orders=[],
        risk=risk_state,
        auto_hedge=AutoHedgeState(enabled=_env_flag("AUTO_HEDGE_ENABLED", False)),
        autopilot=autopilot_state,
        safety=SafetyState(limits=safety_limits, runaway_guard=runaway_guard_state),
        chaos=chaos_settings,
    )
    _sync_loop_from_control(state)
    return state




def get_state() -> RuntimeState:
    return _STATE


def get_market_data() -> MarketDataAggregator:
    if _STATE.market_data is None:
        _STATE.market_data = MarketDataAggregator()
    return _STATE.market_data


def ensure_dryrun_state() -> DryRunState:
    if _STATE.dryrun is None:
        _STATE.dryrun = DryRunState()
    return _STATE.dryrun


def get_loop_state() -> LoopState:
    return _STATE.loop


def get_safety_status() -> Dict[str, object]:
    with _STATE_LOCK:
        return dict(_STATE.safety.status_payload())


def update_runaway_guard_snapshot(snapshot: Mapping[str, object]) -> None:
    persist_snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        safety = _STATE.safety
        safety.runaway_guard.update_from_snapshot(snapshot)
        persist_snapshot = safety.as_dict()
    if persist_snapshot is not None:
        _persist_safety_snapshot(persist_snapshot)


def get_chaos_state() -> ChaosSettings:
    with _STATE_LOCK:
        return _STATE.chaos


def record_universe_unknown_pair(pair_id: str | None) -> None:
    with _STATE_LOCK:
        _STATE.universe.record_unknown(pair_id)


def get_universe_unknown_pairs() -> list[str]:
    with _STATE_LOCK:
        return _STATE.universe.list_unknown()


def clear_universe_unknown_pairs() -> None:
    with _STATE_LOCK:
        _STATE.universe.clear()


def is_hold_active() -> bool:
    with _STATE_LOCK:
        return bool(_STATE.safety.hold_active)


def is_dry_run_mode() -> bool:
    with _STATE_LOCK:
        control = _STATE.control
        return bool(getattr(control, "dry_run_mode", False))


def set_loop_config(*, pair: str | None, venues: List[str], notional_usdt: float) -> LoopState:
    persist_snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        state = get_state()
        state.control.loop_pair = pair.upper() if pair else None
        state.control.loop_venues = [str(entry) for entry in venues]
        state.control.order_notional_usdt = float(notional_usdt)
        _sync_loop_from_control(state)
        persist_snapshot = asdict(state.control)
        loop_state = state.loop
    if persist_snapshot is not None:
        _persist_control_snapshot(persist_snapshot)
    return loop_state


def update_loop_summary(summary: Dict[str, object]) -> None:
    loop_state = get_loop_state()
    loop_state.last_summary = dict(summary)


def get_loop_config() -> LoopConfigState:
    return _STATE.loop_config


def record_resume_request(reason: str, *, requested_by: str | None = None) -> Dict[str, object]:
    cleaned_reason = str(reason)
    with _STATE_LOCK:
        existing = _STATE.safety.resume_request
        if existing and existing.approved_ts is None:
            return existing.as_dict()
    record = approvals_store.create_request(
        CRITICAL_ACTION_RESUME,
        requested_by=requested_by,
        parameters={"reason": cleaned_reason},
    )
    with _STATE_LOCK:
        safety = _STATE.safety
        resume_state = ResumeRequestState(reason=cleaned_reason, requested_by=requested_by)
        resume_state.request_id = str(record.get("id"))
        requested_ts = record.get("requested_ts")
        if requested_ts:
            resume_state.requested_ts = str(requested_ts)
        safety.resume_request = resume_state
        snapshot = safety.as_dict()
    _persist_safety_snapshot(snapshot)
    resume_snapshot = snapshot.get("resume_request")
    _emit_ops_alert(
        "resume_requested",
        "Resume request pending approval",
        {
            "requested_by": requested_by or "system",
            "reason": cleaned_reason,
            "request_id": record.get("id"),
        },
    )
    return dict(resume_snapshot) if isinstance(resume_snapshot, Mapping) else {}


def approve_resume(
    request_id: str | None = None,
    *,
    actor: str | None = None,
) -> Dict[str, object]:
    with _STATE_LOCK:
        safety = _STATE.safety
        resume_state = safety.resume_request
        if resume_state is None:
            raise ValueError("resume_request_missing")
        state_request_id = resume_state.request_id
        if request_id and state_request_id and state_request_id != request_id:
            raise ValueError("resume_request_mismatch")
        target_request_id = request_id or state_request_id
        reason = resume_state.reason
    if not target_request_id:
        raise ValueError("resume_request_id_missing")
    try:
        record = approvals_store.approve_request(target_request_id, actor=actor)
    except KeyError as exc:  # pragma: no cover - defensive propagation
        raise ValueError("resume_request_missing") from exc
    with _STATE_LOCK:
        safety = _STATE.safety
        resume_state = safety.resume_request
        if resume_state:
            resume_state.approve(actor=actor)
        cleared = safety.clear_hold()
        safety_snapshot = safety.as_dict()
    _persist_safety_snapshot(safety_snapshot)
    _emit_ops_alert(
        "resume_confirmed",
        "Resume approval processed",
        {
            "actor": actor or "system",
            "hold_cleared": cleared,
            "request_id": record.get("id"),
            "reason": reason,
        },
    )
    return {"hold_cleared": cleared, "safety": safety_snapshot, "request": record}


def _normalise_risk_request(
    limit: str,
    scope: str | None,
    value: object,
) -> Tuple[str, str | None, float | int]:
    limit_key = str(limit or "").strip().lower()
    if not limit_key:
        raise ValueError("risk_limit_required")
    if value is None:
        raise ValueError("risk_limit_value_required")
    if limit_key == "max_position_usdt":
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("risk_limit_invalid_value") from exc
        if numeric_value <= 0:
            raise ValueError("risk_limit_invalid_value")
        normalised_scope = str(scope or "__default__").strip().upper() or "__DEFAULT__"
        return limit_key, normalised_scope, numeric_value
    if limit_key == "max_open_orders":
        try:
            numeric_value = int(round(float(value)))
        except (TypeError, ValueError) as exc:
            raise ValueError("risk_limit_invalid_value") from exc
        if numeric_value <= 0:
            raise ValueError("risk_limit_invalid_value")
        normalised_scope = str(scope or "__default__").strip().lower() or "__default__"
        return limit_key, normalised_scope, numeric_value
    if limit_key == "max_daily_loss_usdt":
        try:
            numeric_value = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("risk_limit_invalid_value") from exc
        if numeric_value <= 0:
            raise ValueError("risk_limit_invalid_value")
        return limit_key, None, numeric_value
    raise ValueError("risk_limit_unsupported")


def _apply_risk_limit_change(limit: str, scope: str | None, value: object) -> Dict[str, object]:
    limit_key, normalised_scope, numeric_value = _normalise_risk_request(limit, scope, value)
    with _STATE_LOCK:
        risk_limits = _STATE.risk.limits
        if limit_key == "max_position_usdt":
            existing = risk_limits.max_position_usdt.get(normalised_scope)
            if existing is not None and numeric_value < existing:
                raise ValueError("risk_limit_must_increase")
            risk_limits.max_position_usdt[normalised_scope] = float(numeric_value)
        elif limit_key == "max_open_orders":
            existing = risk_limits.max_open_orders.get(normalised_scope)
            if existing is not None and numeric_value < existing:
                raise ValueError("risk_limit_must_increase")
            risk_limits.max_open_orders[normalised_scope] = int(numeric_value)
        else:
            existing = risk_limits.max_daily_loss_usdt
            if existing is not None and numeric_value < existing:
                raise ValueError("risk_limit_must_increase")
            risk_limits.max_daily_loss_usdt = float(numeric_value)
        snapshot = risk_limits.as_dict()
    _persist_runtime_payload({"risk_limits": snapshot})
    return {
        "limit": limit_key,
        "scope": normalised_scope,
        "value": numeric_value,
        "risk_limits": snapshot,
    }


def request_risk_limit_change(
    limit: str,
    scope: str | None,
    new_value: object,
    *,
    reason: str,
    requested_by: str | None = None,
) -> Dict[str, object]:
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ValueError("risk_limit_reason_required")
    limit_key, normalised_scope, numeric_value = _normalise_risk_request(limit, scope, new_value)
    record = approvals_store.create_request(
        CRITICAL_ACTION_RAISE_LIMIT,
        requested_by=requested_by,
        parameters={
            "limit": limit_key,
            "scope": normalised_scope,
            "value": numeric_value,
            "reason": cleaned_reason,
        },
    )
    _emit_ops_alert(
        "risk_limit_requested",
        "Risk limit raise pending approval",
        {
            "limit": limit_key,
            "scope": normalised_scope,
            "value": numeric_value,
            "requested_by": requested_by or "system",
            "reason": cleaned_reason,
            "request_id": record.get("id"),
        },
    )
    return record


def approve_risk_limit_change(request_id: str, *, actor: str | None = None) -> Dict[str, object]:
    record = approvals_store.approve_request(request_id, actor=actor)
    parameters = record.get("parameters") if isinstance(record, Mapping) else None
    if not isinstance(parameters, Mapping):
        raise ValueError("risk_limit_parameters_missing")
    limit = parameters.get("limit")
    scope = parameters.get("scope")
    value = parameters.get("value")
    result = _apply_risk_limit_change(limit, scope, value)
    _emit_ops_alert(
        "risk_limit_approved",
        "Risk limit change approved",
        {
            "actor": actor or "system",
            "limit": result["limit"],
            "scope": result.get("scope"),
            "value": result["value"],
            "request_id": record.get("id"),
            "reason": parameters.get("reason"),
        },
    )
    return {"request": record, "result": result}


def _set_dry_run_flags(*, dry_run: bool, dry_run_mode: bool) -> Dict[str, object]:
    with _STATE_LOCK:
        control = _STATE.control
        control.dry_run = bool(dry_run)
        control.dry_run_mode = bool(dry_run_mode)
        snapshot = asdict(control)
    _persist_control_snapshot(snapshot)
    return snapshot


def request_exit_dry_run(reason: str, *, requested_by: str | None = None) -> Dict[str, object]:
    cleaned_reason = str(reason or "").strip()
    if not cleaned_reason:
        raise ValueError("exit_dry_run_reason_required")
    record = approvals_store.create_request(
        CRITICAL_ACTION_EXIT_DRY_RUN,
        requested_by=requested_by,
        parameters={"reason": cleaned_reason},
    )
    _emit_ops_alert(
        "exit_dry_run_requested",
        "Exit DRY_RUN_MODE pending approval",
        {
            "requested_by": requested_by or "system",
            "reason": cleaned_reason,
            "request_id": record.get("id"),
        },
    )
    return record


def approve_exit_dry_run(request_id: str, *, actor: str | None = None) -> Dict[str, object]:
    record = approvals_store.approve_request(request_id, actor=actor)
    snapshot = _set_dry_run_flags(dry_run=False, dry_run_mode=False)
    parameters = record.get("parameters") if isinstance(record, Mapping) else {}
    _emit_ops_alert(
        "exit_dry_run_approved",
        "DRY_RUN_MODE disabled after approval",
        {
            "actor": actor or "system",
            "request_id": record.get("id"),
            "reason": parameters.get("reason"),
        },
    )
    return {"control": snapshot, "request": record}


def ensure_exchange_watchdog_all_clear(*, context: str = "runtime") -> None:
    """Raise ``HoldActiveError`` when the exchange watchdog is critical."""

    reason = evaluate_exchange_watchdog(context=context)
    if reason:
        raise HoldActiveError(reason)


def _register_action_counter(
    action: str,
    delta: int,
    *,
    reason: str,
    source: str,
) -> None:
    ensure_exchange_watchdog_all_clear(context=f"{source}:{action}")
    triggered_hold = False
    detail = ""
    hold_blocked = False
    snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        safety = _STATE.safety
        now = time.time()
        safety.counters.reset_if_needed(now=now)
        if safety.hold_active:
            hold_blocked = True
        else:
            if action == "orders":
                limit = safety.limits.max_orders_per_min
                current = safety.counters.orders_placed_last_min
            else:
                limit = safety.limits.max_cancels_per_min
                current = safety.counters.cancels_last_min
            projected = current + max(delta, 0)
            if limit > 0 and projected > limit:
                triggered_hold = True
                detail = f"{action}_per_min_limit_exceeded:{projected}>{limit}"
                if action == "orders":
                    safety.counters.orders_placed_last_min = projected
                else:
                    safety.counters.cancels_last_min = projected
            else:
                if action == "orders":
                    safety.counters.orders_placed_last_min = projected
                else:
                    safety.counters.cancels_last_min = projected
            snapshot = safety.as_dict()
    if hold_blocked:
        raise HoldActiveError("hold_active")
    if triggered_hold:
        engage_safety_hold(reason, source=source)
        raise HoldActiveError(detail or f"{action}_limit")
    if snapshot is not None:
        _persist_safety_snapshot(snapshot)


def register_order_attempt(delta: int = 1, *, reason: str, source: str) -> None:
    _register_action_counter("orders", delta, reason=reason, source=source)


def register_cancel_attempt(delta: int = 1, *, reason: str, source: str) -> None:
    _register_action_counter("cancels", delta, reason=reason, source=source)


def evaluate_exchange_watchdog(*, context: str = "runtime") -> str | None:
    """Engage HOLD when the exchange watchdog reports a critical failure."""

    watchdog = get_exchange_watchdog()
    failure = watchdog.most_recent_failure()
    if not failure:
        return None
    exchange, payload = failure
    reason_text = str(payload.get("reason") or "degraded").strip() or "degraded"
    hold_reason = f"exchange_watchdog:{exchange} {reason_text}".strip()
    with _STATE_LOCK:
        previous_reason = str(_STATE.safety.hold_reason or "")
    engaged = engage_safety_hold(hold_reason, source=f"watchdog:{exchange}")
    if engaged:
        slo.inc_skipped("watchdog")
    if engaged or previous_reason != hold_reason:
        log_operator_action(
            "system",
            "system",
            "AUTO_HOLD_WATCHDOG",
            details={
                "exchange": exchange,
                "reason": reason_text,
                "context": context,
                "hold_reason": hold_reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )
    return hold_reason


def update_clock_skew(skew_seconds: float | None, *, source: str = "clock_skew_checker") -> None:
    persist_snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        safety = _STATE.safety
        if skew_seconds is None:
            safety.clock_skew_s = None
        else:
            safety.clock_skew_s = float(skew_seconds)
        safety.clock_skew_checked_ts = _ts()
        persist_snapshot = safety.as_dict()
    if persist_snapshot is not None:
        _persist_safety_snapshot(persist_snapshot)


def update_risk_snapshot(snapshot: Mapping[str, object]) -> None:
    persist_snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        safety = _STATE.safety
        safety.risk_snapshot = dict(snapshot)
        persist_snapshot = safety.as_dict()
    if persist_snapshot is not None:
        _persist_safety_snapshot(persist_snapshot)


def update_liquidity_snapshot(
    snapshot: Mapping[str, object],
    *,
    blocked: bool,
    reason: str | None = None,
    source: str = "balances_monitor",
    auto_hold: bool = True,
) -> None:
    persist_snapshot: Dict[str, object] | None = None
    alert_needed = False
    with _STATE_LOCK:
        safety = _STATE.safety
        previous_blocked = bool(safety.liquidity_blocked)
        clean_snapshot: Dict[str, object] = {}
        for venue, payload in snapshot.items():
            key = str(venue)
            if isinstance(payload, Mapping):
                clean_snapshot[key] = {str(k): v for k, v in payload.items()}
            else:
                clean_snapshot[key] = payload
        safety.liquidity_snapshot = clean_snapshot
        safety.liquidity_blocked = bool(blocked)
        safety.liquidity_reason = reason or ("liquidity_blocked" if blocked else "ok")
        persist_snapshot = safety.as_dict()
        alert_needed = bool(blocked and not previous_blocked)
    if persist_snapshot is not None:
        _persist_safety_snapshot(persist_snapshot)
    if blocked and auto_hold:
        engage_safety_hold(reason or "liquidity_blocked", source=source)
    if alert_needed:
        _emit_ops_alert(
            "liquidity_blocked",
            reason or "Liquidity blocked — insufficient balance",
            {"source": source, "reason": reason or "liquidity_blocked"},
        )


def get_liquidity_status() -> Dict[str, object]:
    with _STATE_LOCK:
        safety = _STATE.safety
        snapshot = {
            str(venue): {str(k): v for k, v in payload.items()} if isinstance(payload, Mapping) else payload
            for venue, payload in safety.liquidity_snapshot.items()
        }
        reason = safety.liquidity_reason or ("liquidity_blocked" if safety.liquidity_blocked else "ok")
        return {
            "liquidity_blocked": bool(safety.liquidity_blocked),
            "reason": reason,
            "per_venue": snapshot,
        }


def update_reconciliation_status(
    *,
    desync_detected: bool | None = None,
    issues: Sequence[Mapping[str, Any]] | None = None,
    diffs: Sequence[Mapping[str, Any]] | None = None,
    last_checked: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> Dict[str, object]:
    timestamp = last_checked or _ts()
    issue_list = [dict(issue) for issue in issues] if issues else []
    diff_list = [dict(diff) for diff in diffs] if diffs else []
    desync_flag = bool(issue_list or diff_list)
    if desync_detected is not None:
        desync_flag = bool(desync_detected) or desync_flag
    snapshot: Dict[str, Any] = {
        "desync_detected": desync_flag,
        "last_checked": timestamp,
        "issues": issue_list,
        "diffs": diff_list,
        "issue_count": len(issue_list),
        "diff_count": len(diff_list),
    }
    if metadata:
        snapshot.update({str(key): value for key, value in metadata.items()})
    persist_snapshot: Dict[str, Any] | None = None
    with _STATE_LOCK:
        safety = _STATE.safety
        previous_snapshot = dict(safety.reconciliation_snapshot)
        previous_flag = bool(safety.desync_detected)
        safety.desync_detected = bool(desync_flag)
        safety.reconciliation_snapshot = snapshot
        if previous_flag != bool(desync_flag) or previous_snapshot != snapshot:
            persist_snapshot = safety.as_dict()
    if persist_snapshot is not None:
        _persist_safety_snapshot(persist_snapshot)
    return dict(snapshot)


def get_reconciliation_status() -> Dict[str, object]:
    with _STATE_LOCK:
        safety = _STATE.safety
        snapshot = dict(safety.reconciliation_snapshot)
        if not snapshot:
            snapshot = {
                "desync_detected": bool(safety.desync_detected),
                "issues": [],
                "diffs": [],
                "issue_count": 0,
                "diff_count": 0,
            }
        snapshot.setdefault("desync_detected", bool(safety.desync_detected))
        snapshot.setdefault("issue_count", len(snapshot.get("issues", [])))
        snapshot.setdefault("diffs", [])
        snapshot.setdefault("diff_count", len(snapshot.get("diffs", [])))
        snapshot.setdefault("auto_hold", False)
        return snapshot


def get_open_orders() -> List[Dict[str, object]]:
    return list(_STATE.open_orders)


def set_open_orders(orders: List[Dict[str, object]]) -> List[Dict[str, object]]:
    with _STATE_LOCK:
        _STATE.open_orders = [dict(order) for order in orders]
        return _STATE.open_orders


def update_guard(name: str, status: str, summary: str, metrics: Dict[str, float] | None = None) -> GuardState:
    guard = _STATE.guards.setdefault(name, GuardState())
    guard.status = status
    guard.summary = summary
    guard.updated_ts = _ts()
    if metrics:
        guard.metrics.update(metrics)
    return guard


def record_incident(kind: str, details: Dict[str, object]) -> None:
    _STATE.incidents.append({"ts": _ts(), "kind": kind, "details": details})


def append_latency_sample(ms: float) -> None:
    _STATE.metrics.latency_samples_ms.append(ms)


def set_preflight_result(ok: bool) -> None:
    snapshot: Dict[str, object] | None = None
    autopilot_snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        control = _STATE.control
        control.preflight_passed = ok
        control.last_preflight_ts = _ts()
        snapshot = asdict(control)
        autopilot_snapshot = _sync_autopilot_targets_locked()
    if snapshot is not None:
        _persist_control_snapshot(snapshot)
    if autopilot_snapshot is not None:
        _persist_autopilot_snapshot(autopilot_snapshot)


def register_approval(actor: str, value: str) -> None:
    _STATE.control.approvals[actor] = value


def bump_counter(name: str, delta: float = 1.0) -> float:
    _STATE.metrics.counters[name] = _STATE.metrics.counters.get(name, 0.0) + delta
    return _STATE.metrics.counters[name]


def _sync_autopilot_targets_locked() -> Dict[str, object]:
    autopilot = _STATE.autopilot
    control = _STATE.control
    autopilot.target_mode = control.mode
    autopilot.target_safe_mode = control.safe_mode
    return autopilot.as_dict()


def get_autopilot_state() -> AutopilotState:
    return _STATE.autopilot


def autopilot_mark_action(action: str, reason: str | None, *, armed: bool) -> AutopilotState:
    with _STATE_LOCK:
        autopilot = _STATE.autopilot
        autopilot.last_action = str(action or "none")
        autopilot.last_reason = reason
        autopilot.last_attempt_ts = _ts()
        autopilot.armed = armed
        snapshot = autopilot.as_dict()
    _persist_autopilot_snapshot(snapshot)
    return _STATE.autopilot


def set_autopilot_decision(decision: str, *, reason: str | None = None) -> AutopilotState:
    decision_value = str(decision or "unknown")
    timestamp = _ts()
    with _STATE_LOCK:
        autopilot = _STATE.autopilot
        autopilot.last_decision = decision_value
        autopilot.last_decision_reason = reason
        autopilot.last_decision_ts = timestamp
        snapshot = autopilot.as_dict()
    _persist_autopilot_snapshot(snapshot)
    return _STATE.autopilot


def autopilot_apply_resume(*, safe_mode: bool) -> Dict[str, object]:
    persist_control: Dict[str, object] | None = None
    persist_safety: Dict[str, object] | None = None
    persist_autopilot: Dict[str, object] | None = None
    hold_cleared = False
    auto_loop_enabled = False
    with _STATE_LOCK:
        control = _STATE.control
        safety = _STATE.safety
        loop_state = _STATE.loop
        hold_cleared = safety.clear_hold()
        safety_snapshot = safety.as_dict()
        control.safe_mode = bool(safe_mode)
        control.mode = "RUN"
        control.auto_loop = True
        auto_loop_enabled = bool(control.auto_loop)
        loop_state.status = "RUN"
        loop_state.running = True
        persist_control = asdict(control)
        persist_safety = safety_snapshot
        persist_autopilot = _sync_autopilot_targets_locked()
    if persist_safety is not None:
        _persist_safety_snapshot(persist_safety)
    if persist_control is not None:
        _persist_control_snapshot(persist_control)
    if persist_autopilot is not None:
        _persist_autopilot_snapshot(persist_autopilot)
    set_auto_trade_state(auto_loop_enabled)
    return {"hold_cleared": hold_cleared, "control": persist_control, "safety": persist_safety}


def set_mode(mode: str) -> None:
    normalised = mode.upper()
    if normalised not in {"RUN", "HOLD"}:
        raise ValueError(f"unsupported mode {mode}")
    snapshot: Dict[str, object] | None = None
    autopilot_snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        control = _STATE.control
        if control.mode != normalised:
            control.mode = normalised
            snapshot = asdict(control)
            autopilot_snapshot = _sync_autopilot_targets_locked()
    if snapshot is not None:
        _persist_control_snapshot(snapshot)
    if autopilot_snapshot is not None:
        _persist_autopilot_snapshot(autopilot_snapshot)


def engage_safety_hold(reason: str, *, source: str = "slo_monitor") -> bool:
    """Force the runtime into HOLD/SAFE_MODE and stop auto-loop if needed."""

    persist_snapshot: Dict[str, object] | None = None
    safety_snapshot: Dict[str, object] | None = None
    autopilot_snapshot: Dict[str, object] | None = None
    changed = False
    hold_changed = False
    auto_loop_enabled: bool | None = None
    with _STATE_LOCK:
        control = _STATE.control
        safety = _STATE.safety
        hold_changed = safety.engage_hold(reason, source=source)
        safety_snapshot = safety.as_dict()
        if control.mode != "HOLD":
            control.mode = "HOLD"
            changed = True
        if not control.safe_mode:
            control.safe_mode = True
            changed = True
        if control.auto_loop:
            control.auto_loop = False
            changed = True
        auto_loop_enabled = bool(control.auto_loop)
        loop_state = _STATE.loop
        if loop_state.running:
            loop_state.running = False
            changed = True
        if loop_state.status != "HOLD":
            loop_state.status = "HOLD"
            changed = True
        if changed:
            persist_snapshot = asdict(control)
        if changed or hold_changed:
            autopilot_snapshot = _sync_autopilot_targets_locked()
        duplicate = next(
            (
                incident
                for incident in _STATE.incidents
                if incident.get("kind") == "auto_hold"
                and incident.get("details", {}).get("reason") == reason
            ),
            None,
        )
        if duplicate is None and (changed or hold_changed):
            _STATE.incidents.append(
                {"ts": _ts(), "kind": "auto_hold", "details": {"reason": reason, "source": source}}
            )
    if persist_snapshot is not None:
        _persist_control_snapshot(persist_snapshot)
    if safety_snapshot is not None:
        _persist_safety_snapshot(safety_snapshot)
    if autopilot_snapshot is not None:
        _persist_autopilot_snapshot(autopilot_snapshot)
    if auto_loop_enabled is not None:
        set_auto_trade_state(auto_loop_enabled)
    if changed or hold_changed:
        _emit_ops_alert(
            "safety_hold",
            f"Safety hold engaged: {reason}",
            {"source": source},
        )
    return changed or hold_changed


def set_last_plan(plan: Dict[str, object]) -> None:
    dryrun_state = ensure_dryrun_state()
    dryrun_state.last_plan = plan
    dryrun_state.last_cycle_ts = _ts()


def set_last_execution(payload: Dict[str, object]) -> None:
    dryrun_state = ensure_dryrun_state()
    dryrun_state.last_execution = payload
    dryrun_state.last_cycle_ts = _ts()


def get_last_plan() -> Dict[str, object] | None:
    if _STATE.dryrun:
        return _STATE.dryrun.last_plan
    return None


def reset_for_tests() -> None:
    """Helper used in tests to reset runtime state."""
    global _STATE, _SHUTDOWN_LOCK, _SHUTDOWN_STARTED, _LAST_SHUTDOWN_RESULT
    _SHUTDOWN_LOCK = None
    _SHUTDOWN_STARTED = False
    _LAST_SHUTDOWN_RESULT = None
    with _STATE_LOCK:
        _STATE = _bootstrap_runtime()
        _load_persisted_state(_STATE)
        _sync_loop_from_control(_STATE)
        _STATE.control.safe_mode = True
        _STATE.control.dry_run = False
        _STATE.control.dry_run_mode = _env_flag("DRY_RUN_MODE", False)
        _STATE.hedge_positions = []
        _STATE.last_opportunity = OpportunityState()
        limits = _STATE.safety.limits
        per_venue_copy = {
            str(venue): dict(symbols)
            for venue, symbols in _STATE.safety.runaway_guard.per_venue.items()
        }
        last_block_value = None
        if isinstance(_STATE.safety.runaway_guard.last_block, Mapping):
            last_block_value = dict(_STATE.safety.runaway_guard.last_block)
        existing_guard = RunawayGuardV2State(
            enabled=_STATE.safety.runaway_guard.enabled,
            max_cancels_per_min=_STATE.safety.runaway_guard.max_cancels_per_min,
            cooldown_sec=_STATE.safety.runaway_guard.cooldown_sec,
            last_trigger_ts=_STATE.safety.runaway_guard.last_trigger_ts,
            per_venue=per_venue_copy,
            last_block=last_block_value,
        )
        _STATE.safety = SafetyState(limits=limits, runaway_guard=existing_guard)
        _STATE.auto_hedge = AutoHedgeState(enabled=_env_flag("AUTO_HEDGE_ENABLED", False))
        _STATE.autopilot = AutopilotState(enabled=_env_flag("AUTOPILOT_ENABLE", False))
        _enforce_safe_start(_STATE)
    try:
        from positions_store import reset_store as _reset_positions_store
    except Exception:  # pragma: no cover - defensive import
        _reset_positions_store = None
    if _reset_positions_store is not None:
        _reset_positions_store()
    globals()["_STATE"] = _STATE
    _persist_safety_snapshot(_STATE.safety.as_dict())
    _persist_autopilot_snapshot(_STATE.autopilot.as_dict())
    update_auto_hedge_state(
        enabled=_STATE.auto_hedge.enabled,
        last_checked_ts=None,
        last_execution_result="idle",
        last_execution_ts=None,
        last_success_ts=None,
        consecutive_failures=0,
    )
    approvals_store.reset_for_tests()
    try:
        from ..strategy_budget import get_strategy_budget_manager

        get_strategy_budget_manager().reset_all_usage()
    except Exception:
        pass


def control_as_dict() -> Dict[str, object]:
    with _STATE_LOCK:
        return asdict(_STATE.control)


def get_positions_state() -> List[Dict[str, Any]]:
    with _STATE_LOCK:
        return [dict(entry) for entry in _STATE.hedge_positions]


def append_position_state(entry: Mapping[str, Any]) -> Dict[str, Any]:
    with _STATE_LOCK:
        record = dict(entry)
        _STATE.hedge_positions.append(record)
        snapshot = [dict(item) for item in _STATE.hedge_positions]
    _persist_runtime_payload({"positions": snapshot})
    return record


def set_positions_state(entries: List[Mapping[str, Any]]) -> None:
    with _STATE_LOCK:
        snapshot = [dict(entry) for entry in entries]
        _STATE.hedge_positions = [dict(entry) for entry in snapshot]
    _persist_runtime_payload({"positions": snapshot})


def get_last_opportunity_state() -> tuple[Dict[str, Any] | None, str]:
    with _STATE_LOCK:
        state = _STATE.last_opportunity
        if not isinstance(state, OpportunityState):
            return None, "blocked_by_risk"
        payload = state.as_dict()
    return (
        dict(payload["opportunity"]) if isinstance(payload.get("opportunity"), Mapping) else None,
        str(payload.get("status") or "blocked_by_risk"),
    )


def set_last_opportunity_state(opportunity: Mapping[str, Any] | None, status: str) -> Dict[str, Any]:
    snapshot: Dict[str, Any | None]
    with _STATE_LOCK:
        state_payload: Dict[str, Any | None] = {
            "opportunity": dict(opportunity) if opportunity is not None else None,
            "status": status,
        }
        _STATE.last_opportunity = OpportunityState(
            opportunity=dict(opportunity) if opportunity is not None else None,
            status=status,
        )
        snapshot = state_payload
    _persist_runtime_payload({"last_opportunity": snapshot})
    return snapshot


def get_auto_hedge_state() -> AutoHedgeState:
    return _STATE.auto_hedge


def update_auto_hedge_state(
    *,
    enabled: bool | object = _UNSET,
    last_checked_ts: str | None | object = _UNSET,
    last_execution_result: str | None | object = _UNSET,
    last_execution_ts: str | None | object = _UNSET,
    last_success_ts: str | None | object = _UNSET,
    consecutive_failures: int | object = _UNSET,
) -> Dict[str, object | None]:
    with _STATE_LOCK:
        auto_state = _STATE.auto_hedge
        if enabled is not _UNSET:
            auto_state.enabled = bool(enabled)
        if last_checked_ts is not _UNSET:
            auto_state.last_opportunity_checked_ts = last_checked_ts  # type: ignore[assignment]
        if last_execution_result is not _UNSET:
            auto_state.last_execution_result = last_execution_result  # type: ignore[assignment]
        if last_execution_ts is not _UNSET:
            auto_state.last_execution_ts = last_execution_ts  # type: ignore[assignment]
        if last_success_ts is not _UNSET:
            auto_state.last_success_ts = last_success_ts  # type: ignore[assignment]
        if consecutive_failures is not _UNSET:
            try:
                auto_state.consecutive_failures = int(consecutive_failures)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                auto_state.consecutive_failures = 0
        snapshot = auto_state.as_dict()
    _persist_runtime_payload({"auto_hedge": snapshot})
    return snapshot


def _normalise_loop_inputs(
    *,
    loop_pair: str | None = None,
    loop_venues: List[str] | None = None,
    notional_usdt: float | None = None,
) -> Tuple[str | None, List[str], float | None]:
    pair = loop_pair.upper() if loop_pair else None
    venues = [str(entry) for entry in loop_venues] if loop_venues else []
    notional = float(notional_usdt) if notional_usdt is not None else None
    return pair, venues, notional


def apply_control_patch(patch: Mapping[str, object]) -> Tuple[ControlState, Dict[str, object]]:
    normalised_patch = _normalise_control_patch(patch)
    updates: Dict[str, object] = {}
    persist_snapshot: Dict[str, object] | None = None
    autopilot_snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        control = _STATE.control
        updates = _apply_control_updates(control, normalised_patch)
        if updates:
            _sync_loop_from_control(_STATE)
            persist_snapshot = asdict(control)
            autopilot_snapshot = _sync_autopilot_targets_locked()
    if persist_snapshot is not None:
        _persist_control_snapshot(persist_snapshot)
    if autopilot_snapshot is not None:
        _persist_autopilot_snapshot(autopilot_snapshot)
    return _STATE.control, updates


def apply_control_snapshot(payload: Mapping[str, object]) -> Dict[str, object]:
    """Replace the in-memory control state with ``payload``."""

    if not isinstance(payload, Mapping):
        raise ValueError("control_snapshot_invalid")
    persist_snapshot: Dict[str, object] | None = None
    autopilot_snapshot: Dict[str, object] | None = None
    auto_loop_changed = False
    auto_loop_enabled: bool | None = None
    result_snapshot: Dict[str, object] | None = None
    with _STATE_LOCK:
        control = _STATE.control
        before_auto_loop = control.auto_loop
        try:
            if "safe_mode" in payload:
                control.safe_mode = _coerce_bool("safe_mode", payload.get("safe_mode"))
            if "two_man_rule" in payload:
                control.two_man_rule = _coerce_bool("two_man_rule", payload.get("two_man_rule"))
            if "dry_run" in payload:
                control.dry_run = _coerce_bool("dry_run", payload.get("dry_run"))
            if "dry_run_mode" in payload:
                control.dry_run_mode = _coerce_bool("dry_run_mode", payload.get("dry_run_mode"))
            if "auto_loop" in payload:
                control.auto_loop = _coerce_bool("auto_loop", payload.get("auto_loop"))
            if "post_only" in payload:
                control.post_only = _coerce_bool("post_only", payload.get("post_only"))
            if "reduce_only" in payload:
                control.reduce_only = _coerce_bool("reduce_only", payload.get("reduce_only"))
            if "order_notional_usdt" in payload:
                control.order_notional_usdt = _coerce_float(
                    "order_notional_usdt", payload.get("order_notional_usdt"), minimum=1.0, maximum=1_000_000.0
                )
            if "max_slippage_bps" in payload:
                control.max_slippage_bps = _coerce_int(
                    "max_slippage_bps", payload.get("max_slippage_bps"), minimum=0, maximum=50
                )
            if "min_spread_bps" in payload:
                control.min_spread_bps = _coerce_float(
                    "min_spread_bps", payload.get("min_spread_bps"), minimum=0.0, maximum=100.0
                )
            if "poll_interval_sec" in payload:
                control.poll_interval_sec = _coerce_int("poll_interval_sec", payload.get("poll_interval_sec"), minimum=1)
            if "loop_pair" in payload:
                control.loop_pair = _coerce_loop_pair(payload.get("loop_pair"))
            if "loop_venues" in payload:
                control.loop_venues = _coerce_loop_venues(payload.get("loop_venues"))
            if "taker_fee_bps_binance" in payload:
                control.taker_fee_bps_binance = _coerce_int(
                    "taker_fee_bps_binance", payload.get("taker_fee_bps_binance"), minimum=0, maximum=10_000
                )
            if "taker_fee_bps_okx" in payload:
                control.taker_fee_bps_okx = _coerce_int(
                    "taker_fee_bps_okx", payload.get("taker_fee_bps_okx"), minimum=0, maximum=10_000
                )
        except ValueError as exc:  # pragma: no cover - defensive propagation
            raise ValueError("control_snapshot_invalid") from exc
        approvals_payload = payload.get("approvals")
        if approvals_payload is not None:
            if not isinstance(approvals_payload, Mapping):
                raise ValueError("control_snapshot_invalid")
            control.approvals = {str(actor): str(ts) for actor, ts in approvals_payload.items()}
        if "preflight_passed" in payload:
            try:
                control.preflight_passed = _coerce_bool("preflight_passed", payload.get("preflight_passed"))
            except ValueError:
                control.preflight_passed = bool(payload.get("preflight_passed"))
        if "last_preflight_ts" in payload:
            ts_value = payload.get("last_preflight_ts")
            control.last_preflight_ts = str(ts_value) if ts_value is not None else None
        if "deployment_mode" in payload:
            mode_value = payload.get("deployment_mode")
            control.deployment_mode = str(mode_value) if mode_value is not None else None
        if "environment" in payload:
            env_value = payload.get("environment")
            control.environment = str(env_value) if env_value is not None else None
        _sync_loop_from_control(_STATE)
        result_snapshot = asdict(control)
        persist_snapshot = dict(result_snapshot)
        autopilot_snapshot = _sync_autopilot_targets_locked()
        auto_loop_changed = before_auto_loop != control.auto_loop
        auto_loop_enabled = control.auto_loop
    if persist_snapshot is not None:
        _persist_control_snapshot(persist_snapshot)
    if autopilot_snapshot is not None:
        _persist_autopilot_snapshot(autopilot_snapshot)
    if auto_loop_changed and auto_loop_enabled is not None:
        set_auto_trade_state(auto_loop_enabled)
    return result_snapshot or asdict(get_state().control)


def apply_risk_limits_snapshot(payload: Mapping[str, object]) -> Dict[str, object]:
    """Replace the risk limits state with ``payload``."""

    if not isinstance(payload, Mapping):
        raise ValueError("risk_limits_snapshot_invalid")
    with _STATE_LOCK:
        limits = _STATE.risk.limits
        positions_payload = payload.get("max_position_usdt")
        if positions_payload is not None:
            if not isinstance(positions_payload, Mapping):
                raise ValueError("risk_limits_snapshot_invalid")
            updated_positions: Dict[str, float] = {}
            for symbol, value in positions_payload.items():
                try:
                    updated_positions[str(symbol).upper()] = float(value)
                except (TypeError, ValueError):
                    continue
            limits.max_position_usdt = updated_positions
        open_orders_payload = payload.get("max_open_orders")
        if open_orders_payload is not None:
            if not isinstance(open_orders_payload, Mapping):
                raise ValueError("risk_limits_snapshot_invalid")
            updated_orders: Dict[str, int] = {}
            for venue, value in open_orders_payload.items():
                try:
                    updated_orders[str(venue).lower()] = int(round(float(value)))
                except (TypeError, ValueError):
                    continue
            limits.max_open_orders = updated_orders
        if "max_daily_loss_usdt" in payload:
            value = payload.get("max_daily_loss_usdt")
            if value is None:
                limits.max_daily_loss_usdt = None
            else:
                try:
                    limits.max_daily_loss_usdt = float(value)
                except (TypeError, ValueError) as exc:
                    raise ValueError("risk_limits_snapshot_invalid") from exc
        snapshot = limits.as_dict()
    _persist_runtime_payload({"risk_limits": snapshot})
    return snapshot


def _normalise_control_patch(patch: Mapping[str, object]) -> Dict[str, object]:
    if not isinstance(patch, Mapping):
        raise ValueError("control patch payload must be a mapping")
    allowed_fields = {
        "min_spread_bps",
        "max_slippage_bps",
        "order_notional_usdt",
        "safe_mode",
        "dry_run_only",
        "two_man_rule",
        "auto_loop",
        "loop_pair",
        "loop_venues",
    }
    normalised: Dict[str, object] = {}
    for field, value in patch.items():
        if field not in allowed_fields or value is None:
            continue
        if field in {"safe_mode", "dry_run_only", "two_man_rule", "auto_loop"}:
            normalised[field] = _coerce_bool(field, value)
            continue
        if field == "max_slippage_bps":
            normalised[field] = _coerce_int(field, value, minimum=0, maximum=50)
            continue
        if field == "min_spread_bps":
            normalised[field] = _coerce_float(field, value, minimum=0.0, maximum=100.0)
            continue
        if field == "order_notional_usdt":
            normalised[field] = _coerce_float(field, value, minimum=1.0, maximum=1_000_000.0)
            continue
        if field == "loop_pair":
            normalised[field] = _coerce_loop_pair(value)
            continue
        if field == "loop_venues":
            normalised[field] = _coerce_loop_venues(value)
            continue
    return normalised


def _coerce_bool(field: str, value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in {0, 1}:
            return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    raise ValueError(f"invalid value for {field}")


def _coerce_float(field: str, value: object, *, minimum: float | None = None, maximum: float | None = None) -> float:
    numeric: float
    if isinstance(value, bool):
        raise ValueError(f"invalid value for {field}")
    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ValueError(f"invalid value for {field}")
        try:
            numeric = float(stripped)
        except ValueError as exc:
            raise ValueError(f"invalid value for {field}") from exc
    else:
        raise ValueError(f"invalid value for {field}")
    if minimum is not None and numeric < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    if maximum is not None and numeric > maximum:
        raise ValueError(f"{field} must be <= {maximum}")
    return numeric


def _coerce_int(field: str, value: object, *, minimum: int | None = None, maximum: int | None = None) -> int:
    numeric = _coerce_float(field, value, minimum=float(minimum) if minimum is not None else None, maximum=float(maximum) if maximum is not None else None)
    if abs(numeric - round(numeric)) > 1e-9:
        raise ValueError(f"{field} must be an integer")
    integer = int(round(numeric))
    if minimum is not None and integer < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    if maximum is not None and integer > maximum:
        raise ValueError(f"{field} must be <= {maximum}")
    return integer


def _coerce_loop_pair(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip().upper()
        return cleaned or None
    raise ValueError("invalid value for loop_pair")


def _coerce_loop_venues(value: object) -> List[str]:
    venues: List[str] = []
    if isinstance(value, str):
        venues = [entry.strip() for entry in value.split(",") if entry.strip()]
    elif isinstance(value, (list, tuple, set)):
        venues = [str(entry).strip() for entry in value if str(entry).strip()]
    else:
        raise ValueError("invalid value for loop_venues")
    return venues


def _apply_control_updates(control: ControlState, updates: Mapping[str, object]) -> Dict[str, object]:
    changes: Dict[str, object] = {}
    for field, value in updates.items():
        target_field = "dry_run" if field == "dry_run_only" else field
        current = getattr(control, target_field, None)
        if current != value:
            setattr(control, target_field, value)
            changes[field] = value
    return changes


def _load_runtime_payload() -> Dict[str, Any]:
    payload = _store_load_runtime_payload()
    if not isinstance(payload, Mapping):
        return {}
    return dict(payload)


def _runtime_status_snapshot() -> Dict[str, Any]:
    with _STATE_LOCK:
        control = _STATE.control
        safety = _STATE.safety
        auto = _STATE.auto_hedge
        resume_request = safety.resume_request
        resume_pending = bool(resume_request and getattr(resume_request, "approved_ts", None) is None)
        status: Dict[str, Any] = {
            "mode": control.mode,
            "safe_mode": control.safe_mode,
            "two_man_resume_required": control.two_man_rule,
            "resume_pending": resume_pending,
            "hold_active": safety.hold_active,
            "hold_reason": safety.hold_reason,
            "hold_source": safety.hold_source,
            "hold_since": safety.hold_since,
            "last_hold_release_ts": safety.last_released_ts,
            "runaway_counters": safety.counters.as_dict(),
            "runaway_limits": safety.limits.as_dict(),
            "auto_hedge_enabled": auto.enabled,
            "auto_hedge_consecutive_failures": auto.consecutive_failures,
            "auto_hedge_last_execution_result": auto.last_execution_result,
            "auto_hedge_last_execution_ts": auto.last_execution_ts,
            "auto_hedge_last_success_ts": getattr(auto, "last_success_ts", None),
            "risk_limits": _STATE.risk.limits.as_dict(),
            "operational_flags": control.flags,
        }
        status["autopilot"] = _STATE.autopilot.as_dict()
        return status


def _persist_runtime_payload(updates: Mapping[str, Any]) -> None:
    payload = _load_runtime_payload()
    payload.update(updates)
    payload.update(_runtime_status_snapshot())
    leader_status = leader_lock.get_status()
    payload["leader_lock"] = leader_status
    payload["leader_fencing_id"] = leader_status.get("fencing_id")
    _store_write_runtime_payload(payload)


def _restore_on_start_enabled(cfg: LoadedConfig) -> bool:
    if os.environ.get("INCIDENT_RESTORE_ON_START") is not None:
        return _env_flag("INCIDENT_RESTORE_ON_START", True)
    incident_cfg = getattr(cfg.data, "incident", None)
    if incident_cfg is None:
        return True
    if isinstance(incident_cfg, Mapping):
        candidate = incident_cfg.get("restore_on_start")
    else:
        candidate = getattr(incident_cfg, "restore_on_start", None)
    if candidate is None:
        return True
    if isinstance(candidate, str):
        return candidate.strip().lower() in {"1", "true", "yes", "on"}
    try:
        return bool(candidate)
    except Exception:
        return True


def _restore_runtime_snapshot(state: RuntimeState) -> bool:
    if not _restore_on_start_enabled(state.config):
        return False
    snapshot = state_store.load()
    if not snapshot:
        return False

    control_payload = snapshot.get("control")
    safety_payload = snapshot.get("safety")
    positions_payload = snapshot.get("positions")
    restored = False
    positions_snapshot: list[dict[str, Any]] = []

    with _STATE_LOCK:
        if isinstance(control_payload, Mapping) and control_payload:
            mode = str(control_payload.get("mode") or state.control.mode).upper()
            if mode in {"RUN", "HOLD"}:
                state.control.mode = mode
            if "safe_mode" in control_payload:
                state.control.safe_mode = bool(control_payload.get("safe_mode"))
            if "auto_loop" in control_payload:
                state.control.auto_loop = bool(control_payload.get("auto_loop"))
            restored = True

        if isinstance(safety_payload, Mapping) and safety_payload:
            if "hold_active" in safety_payload:
                state.safety.hold_active = bool(safety_payload.get("hold_active"))
            if "hold_reason" in safety_payload:
                reason = safety_payload.get("hold_reason")
                state.safety.hold_reason = str(reason) if reason not in (None, "") else None
            if "hold_source" in safety_payload:
                source = safety_payload.get("hold_source")
                state.safety.hold_source = str(source) if source not in (None, "") else None
            if "hold_since" in safety_payload:
                since = safety_payload.get("hold_since")
                state.safety.hold_since = str(since) if since not in (None, "") else None
            if "last_released_ts" in safety_payload:
                last = safety_payload.get("last_released_ts")
                state.safety.last_released_ts = str(last) if last not in (None, "") else None
            restored = True

        if isinstance(positions_payload, list):
            for entry in positions_payload:
                if not isinstance(entry, Mapping):
                    continue
                record = {str(key): value for key, value in entry.items()}
                status_value = str(record.get("status") or "").lower()
                if status_value == "closed":
                    continue
                positions_snapshot.append(record)
            if positions_snapshot:
                state.hedge_positions = [dict(entry) for entry in positions_snapshot]
                restored = True

    if positions_snapshot:
        try:
            from positions_store import append_record as _append_position, reset_store as _reset_positions_store
        except Exception:
            LOGGER.exception("failed to import positions store for snapshot restore")
        else:
            try:
                _reset_positions_store()
                for entry in positions_snapshot:
                    _append_position(entry)
            except Exception:
                LOGGER.exception("failed to restore hedge positions store from snapshot")

    if restored:
        _persist_runtime_payload(
            {
                "control": asdict(state.control),
                "safety": state.safety.as_dict(),
                "positions": [dict(entry) for entry in state.hedge_positions],
            }
        )
    return restored


def _to_epoch_timestamp(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        return parsed.timestamp()
    return None


def _reconciliation_state(snapshot: Mapping[str, Any]) -> str:
    enabled = _env_flag("RECON_ENABLED", True)
    if not enabled:
        return "DISABLED"
    status_text = str(snapshot.get("status") or "").replace("-", "_").upper()
    auto_hold = bool(snapshot.get("auto_hold")) or status_text == "AUTO_HOLD"
    if auto_hold:
        return "DEGRADED"
    if status_text in {"DEGRADED", "DRIFT", "DEGRADED_DRIFT"}:
        return "DEGRADED"
    desync = bool(snapshot.get("desync_detected"))
    diff_count_raw = snapshot.get("diff_count")
    issue_count_raw = snapshot.get("issue_count")
    try:
        diff_count = int(diff_count_raw)
    except (TypeError, ValueError):
        diffs = snapshot.get("diffs") if isinstance(snapshot.get("diffs"), Sequence) else []
        diff_count = len(diffs) if isinstance(diffs, Sequence) else 0
    try:
        issue_count = int(issue_count_raw)
    except (TypeError, ValueError):
        issues = snapshot.get("issues") if isinstance(snapshot.get("issues"), Sequence) else []
        issue_count = len(issues) if isinstance(issues, Sequence) else 0
    mismatch_total = max(diff_count, issue_count)
    if desync or mismatch_total > 0 or status_text in {"MISMATCH", "DESYNC"}:
        return "MISMATCH"
    return "OK"


def _build_reconciliation_runtime(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    last_run_value = snapshot.get("last_run_iso") or snapshot.get("last_checked")
    last_run_iso = str(last_run_value) if last_run_value else None
    last_run_ts = _to_epoch_timestamp(last_run_value)
    diffs_payload = snapshot.get("diffs") if isinstance(snapshot.get("diffs"), Sequence) else []
    issues_payload = snapshot.get("issues") if isinstance(snapshot.get("issues"), Sequence) else []
    try:
        diff_count = int(snapshot.get("diff_count", len(diffs_payload)))
    except (TypeError, ValueError):
        diff_count = len(diffs_payload)
    try:
        issue_count = int(snapshot.get("issue_count", len(issues_payload)))
    except (TypeError, ValueError):
        issue_count = len(issues_payload)
    state = _reconciliation_state(snapshot)
    overview = {
        "state": state,
        "summary": f"Reconciliation status: {state}",
        "last_run_ts": last_run_ts,
        "last_run_iso": last_run_iso,
        "desync_detected": bool(snapshot.get("desync_detected")),
        "diff_count": diff_count,
        "issue_count": issue_count,
        "mismatches_count": max(diff_count, issue_count),
        "auto_hold": bool(snapshot.get("auto_hold")),
    }
    status_text = snapshot.get("status")
    if status_text:
        overview["status"] = status_text
    error_text = snapshot.get("error")
    if error_text:
        overview["error"] = error_text
    return overview


def make_runtime_snapshot() -> Dict[str, Any]:
    status_payload = _runtime_status_snapshot()
    reconciliation_snapshot = get_reconciliation_status()
    status_payload["reconciliation"] = _build_reconciliation_runtime(reconciliation_snapshot)
    status_payload["reconciliation_snapshot"] = dict(reconciliation_snapshot)
    leader_status = leader_lock.get_status()
    status_payload["leader_lock"] = leader_status
    status_payload["leader_fencing_id"] = leader_status.get("fencing_id")
    return status_payload


def _persist_control_snapshot(snapshot: Mapping[str, object]) -> None:
    _persist_runtime_payload({"control": dict(snapshot)})


def _persist_safety_snapshot(snapshot: Mapping[str, object]) -> None:
    _persist_runtime_payload({"safety": dict(snapshot)})


def _persist_autopilot_snapshot(snapshot: Mapping[str, object]) -> None:
    _persist_runtime_payload({"autopilot": dict(snapshot)})


def _load_persisted_state(state: RuntimeState) -> None:
    payload = _load_runtime_payload()
    control_payload = payload.get("control") if isinstance(payload, Mapping) else None
    if isinstance(control_payload, Mapping):
        try:
            updates = _normalise_control_patch(control_payload)
        except ValueError:
            updates = {}
        for field, value in updates.items():
            setattr(state.control, field, value)
    autopilot_state = state.autopilot
    autopilot_state.target_mode = state.control.mode
    autopilot_state.target_safe_mode = state.control.safe_mode
    positions_payload = payload.get("positions")
    if isinstance(positions_payload, list):
        state.hedge_positions = [dict(entry) for entry in positions_payload if isinstance(entry, Mapping)]
    opportunity_payload = payload.get("last_opportunity")
    if isinstance(opportunity_payload, Mapping):
        opportunity = opportunity_payload.get("opportunity")
        status = str(opportunity_payload.get("status") or "blocked_by_risk")
        if isinstance(opportunity, Mapping):
            state.last_opportunity = OpportunityState(opportunity=dict(opportunity), status=status)
        elif opportunity is None:
            state.last_opportunity = OpportunityState(opportunity=None, status=status)
    auto_payload = payload.get("auto_hedge")
    if isinstance(auto_payload, Mapping):
        auto_state = state.auto_hedge
        auto_state.enabled = bool(auto_payload.get("enabled", auto_state.enabled))
        last_checked = auto_payload.get("last_opportunity_checked_ts")
        auto_state.last_opportunity_checked_ts = str(last_checked) if last_checked else None
        last_result = auto_payload.get("last_execution_result")
        auto_state.last_execution_result = str(last_result) if last_result else "idle"
        last_ts = auto_payload.get("last_execution_ts")
        auto_state.last_execution_ts = str(last_ts) if last_ts else None
        last_success = auto_payload.get("last_success_ts")
        auto_state.last_success_ts = str(last_success) if last_success else None
        try:
            auto_state.consecutive_failures = int(auto_payload.get("consecutive_failures", 0))
        except (TypeError, ValueError):
            auto_state.consecutive_failures = 0
    autopilot_payload = payload.get("autopilot")
    if isinstance(autopilot_payload, Mapping):
        last_action = autopilot_payload.get("last_action")
        if last_action is not None:
            autopilot_state.last_action = str(last_action)
        autopilot_state.last_reason = autopilot_payload.get("last_reason") or None
        last_attempt = autopilot_payload.get("last_attempt_ts")
        autopilot_state.last_attempt_ts = str(last_attempt) if last_attempt else None
        if "armed" in autopilot_payload:
            autopilot_state.armed = bool(autopilot_payload.get("armed"))
        target_mode = autopilot_payload.get("target_mode")
        if target_mode:
            autopilot_state.target_mode = str(target_mode).upper()
        if "target_safe_mode" in autopilot_payload:
            autopilot_state.target_safe_mode = bool(autopilot_payload.get("target_safe_mode"))
        last_decision = autopilot_payload.get("last_decision")
        if last_decision is not None:
            autopilot_state.last_decision = str(last_decision)
        autopilot_state.last_decision_reason = (
            autopilot_payload.get("last_decision_reason") or None
        )
        decision_ts = autopilot_payload.get("last_decision_ts")
        autopilot_state.last_decision_ts = str(decision_ts) if decision_ts else None
    safety_payload = payload.get("safety")
    safety = state.safety
    if isinstance(safety_payload, Mapping):
        safety.hold_active = bool(safety_payload.get("hold_active", False))
        safety.hold_reason = safety_payload.get("hold_reason") or None
        safety.hold_source = safety_payload.get("hold_source") or None
        safety.hold_since = safety_payload.get("hold_since") or None
        safety.last_released_ts = safety_payload.get("last_released_ts") or None
        risk_snapshot_payload = safety_payload.get("risk_snapshot")
        if isinstance(risk_snapshot_payload, Mapping):
            safety.risk_snapshot = dict(risk_snapshot_payload)
        else:
            safety.risk_snapshot = {}
        safety.liquidity_blocked = bool(safety_payload.get("liquidity_blocked", False))
        safety.liquidity_reason = safety_payload.get("liquidity_reason") or None
        liquidity_snapshot_payload = safety_payload.get("liquidity_snapshot")
        if isinstance(liquidity_snapshot_payload, Mapping):
            safety.liquidity_snapshot = {
                str(venue): dict(payload) if isinstance(payload, Mapping) else payload
                for venue, payload in liquidity_snapshot_payload.items()
            }
        else:
            safety.liquidity_snapshot = {}
        limits_payload = safety_payload.get("limits")
        if isinstance(limits_payload, Mapping):
            max_orders_value = limits_payload.get("max_orders_per_min")
            try:
                safety.limits.max_orders_per_min = max(0, int(float(max_orders_value)))
            except (TypeError, ValueError):
                pass
            max_cancels_value = limits_payload.get("max_cancels_per_min")
            try:
                safety.limits.max_cancels_per_min = max(0, int(float(max_cancels_value)))
            except (TypeError, ValueError):
                pass
        counters_payload = safety_payload.get("counters")
        if isinstance(counters_payload, Mapping):
            orders_counter = counters_payload.get("orders_placed_last_min")
            try:
                safety.counters.orders_placed_last_min = max(
                    0, int(float(orders_counter))
                )
            except (TypeError, ValueError):
                pass
            cancels_counter = counters_payload.get("cancels_last_min")
            try:
                safety.counters.cancels_last_min = max(0, int(float(cancels_counter)))
            except (TypeError, ValueError):
                pass
        runaway_snapshot = safety_payload.get("runaway_guard")
        if isinstance(runaway_snapshot, Mapping):
            safety.runaway_guard.update_from_snapshot(runaway_snapshot)
        skew_value = safety_payload.get("clock_skew_s")
        if isinstance(skew_value, (int, float)):
            safety.clock_skew_s = float(skew_value)
        else:
            safety.clock_skew_s = None
        safety.clock_skew_checked_ts = safety_payload.get("clock_skew_checked_ts") or None
        safety.desync_detected = bool(safety_payload.get("desync_detected"))
        reconciliation_payload = safety_payload.get("reconciliation")
        if isinstance(reconciliation_payload, Mapping):
            snapshot: Dict[str, Any] = {str(key): value for key, value in reconciliation_payload.items()}
            issues_payload = snapshot.get("issues")
            if isinstance(issues_payload, list):
                snapshot["issues"] = [
                    dict(issue) for issue in issues_payload if isinstance(issue, Mapping)
                ]
            else:
                snapshot["issues"] = []
            diffs_payload = snapshot.get("diffs")
            if isinstance(diffs_payload, list):
                snapshot["diffs"] = [
                    dict(diff) for diff in diffs_payload if isinstance(diff, Mapping)
                ]
            else:
                snapshot["diffs"] = []
            snapshot.setdefault("desync_detected", safety.desync_detected)
            snapshot.setdefault("issue_count", len(snapshot.get("issues", [])))
            snapshot.setdefault("diff_count", len(snapshot.get("diffs", [])))
            snapshot.setdefault("auto_hold", False)
            safety.reconciliation_snapshot = snapshot
        else:
            safety.reconciliation_snapshot = {
                "desync_detected": safety.desync_detected,
                "issues": [],
                "diffs": [],
                "issue_count": 0,
                "diff_count": 0,
            }
        resume_payload = safety_payload.get("resume_request")
        if isinstance(resume_payload, Mapping):
            reason = resume_payload.get("reason")
            if reason:
                request = ResumeRequestState(reason=str(reason), requested_by=resume_payload.get("requested_by"))
                requested_at = resume_payload.get("requested_at") or resume_payload.get("requested_ts")
                if requested_at:
                    request.requested_ts = str(requested_at)
                request_id = resume_payload.get("id") or resume_payload.get("request_id")
                if request_id:
                    request.request_id = str(request_id)
                approved_at = resume_payload.get("approved_at")
                if approved_at:
                    request.approved_ts = str(approved_at)
                approved_by = resume_payload.get("approved_by")
                if approved_by:
                    request.approved_by = str(approved_by)
                safety.resume_request = request
        else:
            safety.resume_request = None
    else:
        safety.resume_request = None

    risk_limits_payload = payload.get("risk_limits")
    if isinstance(risk_limits_payload, Mapping):
        limits = state.risk.limits
        positions_payload = risk_limits_payload.get("max_position_usdt")
        if isinstance(positions_payload, Mapping):
            updated_positions: Dict[str, float] = {}
            for symbol, value in positions_payload.items():
                try:
                    updated_positions[str(symbol).upper()] = float(value)
                except (TypeError, ValueError):
                    continue
            if updated_positions:
                limits.max_position_usdt = updated_positions
        open_orders_payload = risk_limits_payload.get("max_open_orders")
        if isinstance(open_orders_payload, Mapping):
            updated_orders: Dict[str, int] = {}
            for venue, value in open_orders_payload.items():
                try:
                    updated_orders[str(venue).lower()] = int(round(float(value)))
                except (TypeError, ValueError):
                    continue
            if updated_orders:
                limits.max_open_orders = updated_orders
        daily_loss_value = risk_limits_payload.get("max_daily_loss_usdt")
        if daily_loss_value is not None:
            try:
                limits.max_daily_loss_usdt = float(daily_loss_value)
            except (TypeError, ValueError):
                pass


def _enforce_safe_start(state: RuntimeState) -> None:
    control = state.control
    control.mode = "HOLD"
    control.safe_mode = True
    control.auto_loop = False
    state.loop.running = False
    state.loop.status = "HOLD"
    safety = state.safety
    if not safety.hold_active:
        safety.engage_hold("restart_safe_mode", source="bootstrap")
    else:
        safety.hold_reason = safety.hold_reason or "restart_safe_mode"
        safety.hold_source = safety.hold_source or "bootstrap"
    safety.resume_request = None
    set_auto_trade_state(False)


_STATE = _bootstrap_runtime()
_load_persisted_state(_STATE)
_sync_loop_from_control(_STATE)
_enforce_safe_start(_STATE)
_restore_runtime_snapshot(_STATE)
_sync_loop_from_control(_STATE)
_persist_runtime_payload({
    "control": asdict(_STATE.control),
    "safety": _STATE.safety.as_dict(),
    "autopilot": _STATE.autopilot.as_dict(),
})
class HoldActiveError(RuntimeError):
    """Raised when execution should stop due to the global hold flag."""

    def __init__(self, reason: str = "hold_active") -> None:
        super().__init__(reason)
        self.reason = reason


def _coerce_text(value: object) -> str:
    return str(value) if value is not None else ""


def _batch_id_for_orders(orders: Iterable[Mapping[str, object]]) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    hasher = hashlib.sha256()
    normalised: list[dict[str, object]] = []
    for order in orders:
        normalised.append(
            {
                "id": int(order.get("id", 0)),
                "venue": _coerce_text(order.get("venue")),
                "symbol": _coerce_text(order.get("symbol")),
                "idemp_key": _coerce_text(order.get("idemp_key")),
            }
        )
    for entry in sorted(normalised, key=lambda item: (item["venue"].lower(), item["id"])):
        hasher.update(str(entry["id"]).encode("utf-8"))
        if entry["venue"]:
            hasher.update(entry["venue"].lower().encode("utf-8"))
        if entry["symbol"]:
            hasher.update(entry["symbol"].upper().encode("utf-8"))
        if entry["idemp_key"]:
            hasher.update(entry["idemp_key"].encode("utf-8"))
    digest = hasher.hexdigest()[:12]
    return f"shutdown-{timestamp}-{digest}"


def setup_signal_handlers(loop: asyncio.AbstractEventLoop | None = None) -> None:
    """Register SIGTERM/SIGINT handlers for graceful shutdown."""

    global _SIGNAL_LOOP

    try:
        candidate_loop = loop or asyncio.get_event_loop()
    except RuntimeError:
        LOGGER.debug("no running loop available for shutdown handlers")
        return

    _SIGNAL_LOOP = candidate_loop

    def _handler(signum: int, frame) -> None:  # pragma: no cover - exercised via tests
        handle_shutdown_signal(signum)

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _handler)
        except (ValueError, RuntimeError):  # pragma: no cover - unsupported in env
            LOGGER.debug("failed to install signal handler", exc_info=True)


def handle_shutdown_signal(signum: int) -> asyncio.Task[Dict[str, object]] | None:
    """Schedule shutdown coroutine; returns created task for observability."""

    loop: asyncio.AbstractEventLoop | None = None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = _SIGNAL_LOOP
    if loop is None or loop.is_closed():
        LOGGER.debug("shutdown signal ignored — no active loop", extra={"signal": signum})
        return None
    try:
        signal_name = signal.Signals(signum).name
    except Exception:  # pragma: no cover - defensive
        signal_name = str(signum)
    LOGGER.info("shutdown signal received", extra={"signal": signal_name})
    return loop.create_task(on_shutdown(reason=signal_name))


async def _stop_component(
    label: str,
    stop_func: Callable[[], Awaitable[object] | object],
) -> dict[str, object]:
    try:
        result = stop_func()
        if inspect.isawaitable(result):
            await result
        LOGGER.info("component stopped", extra={"component": label})
        return {"component": label, "status": "stopped"}
    except Exception as exc:  # pragma: no cover - defensive logging
        LOGGER.exception("component stop failed", extra={"component": label})
        return {"component": label, "status": "error", "error": str(exc)}


async def on_shutdown(*, reason: str | None = None) -> Dict[str, object]:
    """Engage HOLD, stop background workers and cancel open orders."""

    global _SHUTDOWN_LOCK, _SHUTDOWN_STARTED, _LAST_SHUTDOWN_RESULT

    if _SHUTDOWN_LOCK is None:
        _SHUTDOWN_LOCK = asyncio.Lock()

    async with _SHUTDOWN_LOCK:
        if _SHUTDOWN_STARTED:
            return _LAST_SHUTDOWN_RESULT or {"status": "duplicate"}
        _SHUTDOWN_STARTED = True

        shutdown_reason = reason or "signal"
        LOGGER.info("initiating shutdown", extra={"reason": shutdown_reason})
        summary: Dict[str, object] = {"reason": shutdown_reason}

        with _STATE_LOCK:
            pre_shutdown_control = asdict(_STATE.control)
            pre_shutdown_safety = _STATE.safety.as_dict()
            pre_shutdown_positions = [
                dict(entry)
                for entry in _STATE.hedge_positions
                if str(entry.get("status") or "").lower() != "closed"
            ]

        hold_changed = engage_safety_hold("shutdown", source="signal_handler")
        set_mode("HOLD")
        summary["hold_engaged"] = hold_changed or bool(get_state().safety.hold_active)

        stop_results: list[dict[str, object]] = []
        try:
            from . import loop as loop_service

            stop_results.append(await _stop_component("loop", loop_service.stop_loop))
        except Exception as exc:  # pragma: no cover - defensive import failure
            LOGGER.debug("loop.stop_loop not available", exc_info=True)
            stop_results.append({"component": "loop", "status": "error", "error": str(exc)})

        stoppables: list[tuple[str, Callable[[], Awaitable[object] | object]]] = []
        try:
            from .partial_hedge_runner import get_runner as get_partial_runner

            stoppables.append(("partial_hedge_runner", lambda: get_partial_runner().stop()))
        except Exception:  # pragma: no cover
            LOGGER.debug("partial hedge runner unavailable", exc_info=True)
        try:
            from .recon_runner import get_runner as get_recon_runner

            stoppables.append(("recon_runner", lambda: get_recon_runner().stop()))
        except Exception:  # pragma: no cover
            LOGGER.debug("recon runner unavailable", exc_info=True)
        try:
            from .exchange_watchdog_runner import get_runner as get_watchdog_runner

            stoppables.append(("exchange_watchdog", lambda: get_watchdog_runner().stop()))
        except Exception:  # pragma: no cover
            LOGGER.debug("exchange watchdog runner unavailable", exc_info=True)
        try:
            from ..auto_hedge_daemon import _daemon as auto_hedge_daemon

            stoppables.append(("auto_hedge_daemon", auto_hedge_daemon.stop))
        except Exception:  # pragma: no cover
            LOGGER.debug("auto hedge daemon unavailable", exc_info=True)
        try:
            from .autopilot_guard import get_guard as get_autopilot_guard

            stoppables.append(("autopilot_guard", lambda: get_autopilot_guard().stop()))
        except Exception:  # pragma: no cover
            LOGGER.debug("autopilot guard unavailable", exc_info=True)
        try:
            from .orchestrator_alerts import _ALERT_LOOP

            stoppables.append(("orchestrator_alerts", _ALERT_LOOP.stop))
        except Exception:  # pragma: no cover
            LOGGER.debug("orchestrator alerts unavailable", exc_info=True)
        try:
            from services.opportunity_scanner import get_scanner

            stoppables.append(("opportunity_scanner", lambda: get_scanner().stop()))
        except Exception:  # pragma: no cover
            LOGGER.debug("opportunity scanner unavailable", exc_info=True)

        for label, stopper in stoppables:
            stop_results.append(await _stop_component(label, stopper))

        summary["stopped"] = stop_results

        try:
            open_orders = await asyncio.to_thread(ledger.fetch_open_orders)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.exception("failed to load open orders prior to cancel-all")
            open_orders = []

        batch_id = _batch_id_for_orders(open_orders)
        cancel_summary = {"batch_id": batch_id, "cancelled": 0, "failed": 0, "venues": []}
        if open_orders:
            from ..broker.router import ExecutionRouter

            router = ExecutionRouter()
            grouped: Dict[str, list[Dict[str, object]]] = {}
            for order in open_orders:
                venue_key = _coerce_text(order.get("venue")).lower()
                grouped.setdefault(venue_key, []).append(order)
            for venue_key, venue_orders in grouped.items():
                venue_batch = f"{batch_id}:{venue_key}" if batch_id else None
                try:
                    result = await router.cancel_all(
                        venue=venue_key,
                        orders=venue_orders,
                        batch_id=venue_batch,
                    )
                except Exception as exc:  # pragma: no cover - defensive logging
                    LOGGER.exception("cancel-all failed", extra={"venue": venue_key})
                    result = {"venue": venue_key, "cancelled": 0, "failed": len(venue_orders), "error": str(exc)}
                cancel_summary["cancelled"] += int(result.get("cancelled", 0) or 0)
                cancel_summary["failed"] += int(result.get("failed", 0) or 0)
                cancel_summary["venues"].append(result)
        summary["cancel_all"] = cancel_summary

        if cancel_summary["cancelled"]:
            remaining = await asyncio.to_thread(ledger.fetch_open_orders)
            set_open_orders(remaining)

        try:
            state_store.dump(
                control=pre_shutdown_control,
                safety=pre_shutdown_safety,
                positions=pre_shutdown_positions,
            )
        except Exception:
            LOGGER.exception("failed to persist runtime snapshot on shutdown")

        _LAST_SHUTDOWN_RESULT = dict(summary)
        return summary

