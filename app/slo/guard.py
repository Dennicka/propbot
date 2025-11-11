from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Mapping, MutableMapping

from ..services import runtime


LOGGER = logging.getLogger(__name__)

__all__ = ["apply_critical_slo_auto_hold", "build_default_context"]


_DEFAULT_LATENCY_P95_MS = 1000.0
_DEFAULT_MD_STALENESS_S = 5.0
_DEFAULT_ORDER_ERROR_RATE = 0.05


@dataclass
class _GuardState:
    active_reason: str | None = None
    active_metric: str | None = None
    healthy_windows: int = 0
    previous_safe_mode: bool | None = None
    previous_auto_loop: bool | None = None


_STATE = _GuardState()


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _config_lookup(config: Any, key: str) -> Any:
    if config is None:
        return None
    if isinstance(config, Mapping):
        if key in config:
            return config[key]
        slo_cfg = config.get("slo") if isinstance(config, MutableMapping) else None
        if isinstance(slo_cfg, Mapping) and key in slo_cfg:
            return slo_cfg[key]
        thresholds = config.get("thresholds") if isinstance(config, MutableMapping) else None
        if isinstance(thresholds, Mapping):
            return _config_lookup(thresholds, key)
        return None
    if hasattr(config, key):
        return getattr(config, key)
    slo_cfg = getattr(config, "slo", None)
    if slo_cfg is not None:
        candidate = _config_lookup(slo_cfg, key)
        if candidate is not None:
            return candidate
    thresholds = getattr(config, "thresholds", None)
    if thresholds is not None:
        return _config_lookup(thresholds, key)
    return None


def _auto_hold_enabled(config: Any) -> bool:
    override = _config_lookup(config, "auto_hold_on_slo")
    if override is not None:
        if isinstance(override, str):
            return override.strip().lower() in {"1", "true", "yes", "on"}
        return bool(override)
    return _env_flag("AUTO_HOLD_ON_SLO", True)


def _resolve_threshold(config: Any, key: str, env_var: str, default: float) -> float:
    override = _config_lookup(config, key)
    if override is not None:
        try:
            return float(override)
        except (TypeError, ValueError) as exc:
            LOGGER.debug("invalid slo threshold key=%s value=%s error=%s", key, override, exc)
    return _env_float(env_var, default)


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return None
    return numeric


def _extract_latency(snapshot: Mapping[str, Any]) -> float | None:
    candidates: list[float] = []
    direct = _coerce_float(snapshot.get("latency_p95_ms"))
    if direct is not None:
        candidates.append(direct)
    ui = snapshot.get("ui")
    if isinstance(ui, Mapping):
        value = _coerce_float(ui.get("p95_ms"))
        if value is not None:
            candidates.append(value)
    core = snapshot.get("core")
    if isinstance(core, Mapping):
        for entry in core.values():
            if not isinstance(entry, Mapping):
                continue
            value = _coerce_float(entry.get("p95_ms"))
            if value is not None:
                candidates.append(value)
    if not candidates:
        return None
    return max(candidates)


def _extract_md_staleness(
    snapshot: Mapping[str, Any], state: runtime.RuntimeState | None
) -> float | None:
    direct = _coerce_float(snapshot.get("md_staleness_s"))
    if direct is not None:
        return direct
    if state is None:
        return None
    slo_metrics = getattr(state.metrics, "slo", {})
    if isinstance(slo_metrics, Mapping):
        gap_ms = _coerce_float(slo_metrics.get("ws_gap_ms_p95"))
        if gap_ms is not None:
            return gap_ms / 1000.0
    return None


def _extract_error_rate(snapshot: Mapping[str, Any]) -> float | None:
    direct = _coerce_float(snapshot.get("order_error_rate"))
    if direct is not None:
        return direct
    overall = snapshot.get("overall")
    if isinstance(overall, Mapping):
        return _coerce_float(overall.get("error_rate"))
    return None


def _reset_state() -> None:
    _STATE.active_reason = None
    _STATE.active_metric = None
    _STATE.healthy_windows = 0
    _STATE.previous_safe_mode = None
    _STATE.previous_auto_loop = None


def _engage_auto_hold(
    metric: str, state: runtime.RuntimeState, *, source: str = "slo_guard"
) -> str:
    reason = f"SLO_CRITICAL::{metric}"
    if not _STATE.active_reason:
        _STATE.previous_safe_mode = bool(state.control.safe_mode)
        _STATE.previous_auto_loop = bool(state.control.auto_loop)
    _STATE.active_reason = reason
    _STATE.active_metric = metric
    _STATE.healthy_windows = 0
    runtime.engage_safety_hold(reason, source=source)
    return reason


def _clear_auto_hold(state: runtime.RuntimeState) -> None:
    target_safe_mode = bool(_STATE.previous_safe_mode)
    runtime.autopilot_apply_resume(safe_mode=target_safe_mode)
    previous_auto_loop = _STATE.previous_auto_loop
    if previous_auto_loop is not None and previous_auto_loop is not True:
        try:
            lock = runtime._STATE_LOCK  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - defensive
            lock = None
        if lock is not None:
            with lock:
                runtime._STATE.control.auto_loop = bool(previous_auto_loop)  # type: ignore[attr-defined]
        else:
            runtime.get_state().control.auto_loop = bool(previous_auto_loop)
        runtime.set_auto_trade_state(bool(previous_auto_loop))
    _reset_state()


def apply_critical_slo_auto_hold(ctx: Any, slo_snapshot: Mapping[str, Any] | None) -> str | None:
    if not isinstance(slo_snapshot, Mapping):
        _STATE.healthy_windows = 0
        return None

    config = getattr(ctx, "config", None)
    if not _auto_hold_enabled(config):
        _reset_state()
        return None

    state = getattr(ctx, "state", None)
    if state is None or not isinstance(state, runtime.RuntimeState):
        state = runtime.get_state()

    latency_threshold = _resolve_threshold(
        config,
        "latency_p95_critical",
        "SLO_LATENCY_P95_CRITICAL_MS",
        _DEFAULT_LATENCY_P95_MS,
    )
    staleness_threshold = _resolve_threshold(
        config,
        "md_staleness_critical",
        "SLO_MD_STALENESS_CRITICAL_S",
        _DEFAULT_MD_STALENESS_S,
    )
    error_rate_threshold = _resolve_threshold(
        config,
        "order_error_rate_critical",
        "SLO_ORDER_ERROR_RATE_CRITICAL",
        _DEFAULT_ORDER_ERROR_RATE,
    )

    latency_value = _extract_latency(slo_snapshot)
    staleness_value = _extract_md_staleness(slo_snapshot, state)
    error_rate_value = _extract_error_rate(slo_snapshot)

    breaches: list[tuple[str, float | None, float]] = [
        ("LATENCY_P95_MS", latency_value, latency_threshold),
        ("MD_STALENESS_S", staleness_value, staleness_threshold),
        ("ORDER_ERROR_RATE", error_rate_value, error_rate_threshold),
    ]

    for metric_name, value, threshold in breaches:
        if value is None:
            continue
        if value > threshold:
            return _engage_auto_hold(metric_name, state)

    safety = state.safety
    hold_reason = str(getattr(safety, "hold_reason", "") or "")
    if hold_reason.startswith("SLO_CRITICAL::") and getattr(safety, "hold_active", False):
        _STATE.healthy_windows += 1
        if _STATE.healthy_windows >= 2:
            _clear_auto_hold(state)
    else:
        _reset_state()
    return None


def build_default_context() -> SimpleNamespace:
    state = runtime.get_state()
    config = SimpleNamespace(
        auto_hold_on_slo=_env_flag("AUTO_HOLD_ON_SLO", True),
        latency_p95_critical=_env_float("SLO_LATENCY_P95_CRITICAL_MS", _DEFAULT_LATENCY_P95_MS),
        md_staleness_critical=_env_float("SLO_MD_STALENESS_CRITICAL_S", _DEFAULT_MD_STALENESS_S),
        order_error_rate_critical=_env_float(
            "SLO_ORDER_ERROR_RATE_CRITICAL", _DEFAULT_ORDER_ERROR_RATE
        ),
    )
    return SimpleNamespace(runtime=runtime, state=state, config=config)
