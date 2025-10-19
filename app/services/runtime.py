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


def _resolve_profile() -> str:
    profile = (
        os.environ.get("MODE")
        or os.environ.get("EXCHANGE_PROFILE")
        or "testnet"
    ).lower()
    if profile not in DEFAULT_CONFIG_PATHS:
        return "testnet"
    return profile


def _resolve_config_path() -> str:
    profile = _resolve_profile()
    return DEFAULT_CONFIG_PATHS.get(profile, DEFAULT_CONFIG_PATHS["testnet"])


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


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
    post_only: bool = True
    reduce_only: bool = True
    two_man_rule: bool = True
    approvals: Dict[str, str] = field(default_factory=dict)
    preflight_passed: bool = False
    last_preflight_ts: str | None = None
    environment: str = "testnet"

    @property
    def flags(self) -> Dict[str, object]:
        """Expose environment execution flags for UI schemas."""
        return {
            "mode": self.environment,
            "safe_mode": self.safe_mode,
            "post_only": self.post_only,
            "reduce_only": self.reduce_only,
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
class RuntimeState:
    config: LoadedConfig
    guards: Dict[str, GuardState]
    control: ControlState
    metrics: MetricsState
    incidents: List[Dict[str, object]] = field(default_factory=list)
    derivatives: DerivativesRuntime | None = None


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
    profile = _resolve_profile()
    config_path = _resolve_config_path()
    loaded = load_app_config(config_path)
    safe_mode = _env_flag("SAFE_MODE", True)
    control = ControlState(
        mode="HOLD" if safe_mode else "RUN",
        safe_mode=safe_mode,
        post_only=_env_flag("POST_ONLY", True),
        reduce_only=_env_flag("REDUCE_ONLY", True),
        environment=profile,
    )
    guards = _init_guards(loaded)
    metrics = MetricsState()
    derivatives = bootstrap_derivatives(loaded, safe_mode=safe_mode)
    return RuntimeState(config=loaded, guards=guards, control=control, metrics=metrics, derivatives=derivatives)


_STATE = _bootstrap_runtime()


def get_state() -> RuntimeState:
    return _STATE


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


def reset_for_tests() -> None:
    """Helper used in tests to reset runtime state."""
    global _STATE
    _STATE = _bootstrap_runtime()
    # Avoid stale arbitrage engines referencing the previous runtime instance.
    from . import arbitrage

    arbitrage.reset_engine()
