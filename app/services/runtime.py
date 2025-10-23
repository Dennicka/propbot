from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List

from ..core.config import GuardsConfig, LoadedConfig, load_app_config
from .derivatives import DerivativesRuntime, bootstrap_derivatives


DEFAULT_CONFIG_PATHS = {
    "paper": "configs/config.paper.yaml",
    "testnet": "configs/config.testnet.yaml",
    "live": "configs/config.live.yaml",
}


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
class RuntimeState:
    config: LoadedConfig
    guards: Dict[str, GuardState]
    control: ControlState
    metrics: MetricsState
    incidents: List[Dict[str, object]] = field(default_factory=list)
    derivatives: DerivativesRuntime | None = None
    dryrun: DryRunState | None = None
    loop: LoopState = field(default_factory=LoopState)
    loop_config: LoopConfigState = field(default_factory=LoopConfigState)
    open_orders: List[Dict[str, object]] = field(default_factory=list)
    risk: RiskState = field(default_factory=RiskState)


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
    control_cfg = loaded.data.control
    safe_mode = _env_flag("SAFE_MODE", control_cfg.safe_mode if control_cfg else True)
    dry_run_only = _env_flag("DRY_RUN_ONLY", control_cfg.dry_run if control_cfg else False)
    order_notional = _env_float("ORDER_NOTIONAL_USDT", 50.0)
    slippage_bps = _env_int("MAX_SLIPPAGE_BPS", 2)
    fee_binance = _env_int("TAKER_FEE_BPS_BINANCE", 2)
    fee_okx = _env_int("TAKER_FEE_BPS_OKX", 2)
    poll_interval = _env_int("POLL_INTERVAL_SEC", 5)
    min_spread_bps = _env_float("MIN_SPREAD_BPS", 0.0)
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
    guards = _init_guards(loaded)
    metrics = MetricsState()
    derivatives = bootstrap_derivatives(loaded, safe_mode=safe_mode)
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
    return RuntimeState(
        config=loaded,
        guards=guards,
        control=control,
        metrics=metrics,
        incidents=[],
        derivatives=derivatives,
        dryrun=dryrun_state,
        loop=LoopState(),
        loop_config=LoopConfigState(
            pair=control.loop_pair,
            venues=list(control.loop_venues),
            notional_usdt=control.order_notional_usdt,
        ),
        open_orders=[],
        risk=risk_state,
    )


_STATE = _bootstrap_runtime()


def get_state() -> RuntimeState:
    return _STATE


def ensure_dryrun_state() -> DryRunState:
    if _STATE.dryrun is None:
        _STATE.dryrun = DryRunState()
    return _STATE.dryrun


def get_loop_state() -> LoopState:
    return _STATE.loop


def set_loop_config(*, pair: str | None, venues: List[str], notional_usdt: float) -> LoopState:
    state = get_state()
    state.control.loop_pair = pair.upper() if pair else None
    state.control.loop_venues = [str(entry) for entry in venues]
    state.control.order_notional_usdt = float(notional_usdt)
    loop_config = state.loop_config
    loop_config.pair = state.control.loop_pair
    loop_config.venues = list(state.control.loop_venues)
    loop_config.notional_usdt = state.control.order_notional_usdt
    loop_state = get_loop_state()
    loop_state.pair = state.control.loop_pair
    loop_state.venues = list(state.control.loop_venues)
    loop_state.notional_usdt = state.control.order_notional_usdt
    return loop_state


def update_loop_summary(summary: Dict[str, object]) -> None:
    loop_state = get_loop_state()
    loop_state.last_summary = dict(summary)


def get_loop_config() -> LoopConfigState:
    return _STATE.loop_config


def get_open_orders() -> List[Dict[str, object]]:
    return list(_STATE.open_orders)


def set_open_orders(orders: List[Dict[str, object]]) -> List[Dict[str, object]]:
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
    _STATE.control.preflight_passed = ok
    _STATE.control.last_preflight_ts = _ts()


def register_approval(actor: str, value: str) -> None:
    _STATE.control.approvals[actor] = value


def bump_counter(name: str, delta: float = 1.0) -> float:
    _STATE.metrics.counters[name] = _STATE.metrics.counters.get(name, 0.0) + delta
    return _STATE.metrics.counters[name]


def set_mode(mode: str) -> None:
    normalised = mode.upper()
    if normalised not in {"RUN", "HOLD"}:
        raise ValueError(f"unsupported mode {mode}")
    _STATE.control.mode = normalised


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
    global _STATE
    _STATE = _bootstrap_runtime()
