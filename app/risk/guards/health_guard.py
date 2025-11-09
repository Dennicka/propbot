"""Account health guard enforcing throttles based on margin health snapshots."""

from __future__ import annotations

import logging
import os
import time
from types import SimpleNamespace
from typing import Callable, Dict, Iterable, Mapping

from ...health.account_health import (
    AccountHealthSnapshot,
    AccountHealthState,
    collect_account_health,
    evaluate_health,
)
from ...metrics.risk_governor import set_throttled as set_risk_throttled
from ...services import runtime as runtime_service
from ...audit_log import log_operator_action
from ...risk.freeze import FreezeRule, get_freeze_registry

try:  # pragma: no cover - import guard for optional bootstrap contexts
    from ...config.schema import HealthConfig
except Exception:  # pragma: no cover - fallback when schema unavailable
    HealthConfig = None  # type: ignore[misc, assignment]

LOGGER = logging.getLogger(__name__)

_STATE_SEVERITY: Dict[AccountHealthState, int] = {"OK": 0, "WARN": 1, "CRITICAL": 2}


class AccountHealthGuard:
    """Apply throttling and HOLD actions based on account health state."""

    WARN_CAUSE = "ACCOUNT_HEALTH_WARN"
    CRITICAL_CAUSE = "ACCOUNT_HEALTH_CRITICAL"
    HOLD_SOURCE = "health_guard"
    CRITICAL_REASON_PREFIX = "ACCOUNT_HEALTH::CRITICAL::"

    def __init__(
        self,
        ctx: Callable[[], object] | object,
        cfg: object | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._ctx_factory: Callable[[], object]
        if callable(ctx):
            self._ctx_factory = ctx  # type: ignore[assignment]
        else:
            self._ctx_factory = lambda: ctx
        self._initial_ctx = self._safe_ctx(log=False)
        self._cfg = cfg
        self._env = env if env is not None else os.environ
        self._runtime_fallback = runtime_service

        self._health_cfg = self._resolve_health_config(cfg, self._initial_ctx)
        self._hysteresis = max(int(getattr(self._health_cfg, "hysteresis_ok_windows", 0) or 0), 0)
        self._enabled = self._resolve_enabled()
        self._auto_freeze = self._env_flag("AUTO_FREEZE_ON_HEALTH", False)

        self._ok_streak = 0
        self._throttle_reason: str | None = None
        self._active_hold_reason: str | None = None
        self._previous_safe_mode: bool | None = None
        self._last_status: AccountHealthState = "OK"

    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    # ------------------------------------------------------------------
    def tick(self) -> tuple[dict[str, AccountHealthState], AccountHealthState]:
        """Collect snapshots, evaluate states and apply guard actions."""

        if not self.enabled:
            return {}, "OK"

        ctx = self._safe_ctx(log=True)
        if ctx is None:
            return {}, "OK"

        runtime = getattr(ctx, "runtime", self._runtime_fallback)
        snapshots = self._collect_snapshots(ctx)
        states, worst_state, worst_exchanges = self._classify_snapshots(snapshots)
        self._apply_state(runtime, worst_state, worst_exchanges)
        self._update_guard(runtime, states, worst_state)
        self._last_status = worst_state
        return states, worst_state

    # ------------------------------------------------------------------
    def _collect_snapshots(self, ctx: object) -> dict[str, AccountHealthSnapshot]:
        try:
            snapshots = collect_account_health(ctx) or {}
        except Exception:  # pragma: no cover - defensive guard
            LOGGER.exception("health guard failed to collect account snapshots")
            return {}
        return snapshots

    # ------------------------------------------------------------------
    def _classify_snapshots(
        self, snapshots: dict[str, AccountHealthSnapshot]
    ) -> tuple[dict[str, AccountHealthState], AccountHealthState, list[str]]:
        states: dict[str, AccountHealthState] = {}
        worst_state: AccountHealthState = "OK"
        worst_exchanges: list[str] = []
        config_scope = self._cfg if self._cfg is not None else self._health_cfg
        for exchange, snapshot in snapshots.items():
            try:
                state = evaluate_health(snapshot, config_scope)
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("health guard failed to evaluate snapshot for %s", exchange)
                state = "CRITICAL"
            states[exchange] = state
            severity = _STATE_SEVERITY.get(state, 0)
            worst_severity = _STATE_SEVERITY.get(worst_state, 0)
            if severity > worst_severity:
                worst_state = state
                worst_exchanges = [exchange]
            elif severity == worst_severity:
                worst_exchanges.append(exchange)
        return states, worst_state, worst_exchanges

    # ------------------------------------------------------------------
    def _apply_state(
        self,
        runtime: object,
        worst_state: AccountHealthState,
        worst_exchanges: Iterable[str],
    ) -> None:
        if worst_state == "CRITICAL":
            self._handle_critical(runtime, list(worst_exchanges))
        elif worst_state == "WARN":
            self._handle_warn(runtime)
        else:
            self._handle_ok(runtime)

    # ------------------------------------------------------------------
    def _handle_warn(self, runtime: object) -> None:
        self._ok_streak = 0
        self._set_throttle(runtime, self.WARN_CAUSE)

    # ------------------------------------------------------------------
    def _handle_critical(self, runtime: object, exchanges: list[str]) -> None:
        self._ok_streak = 0
        reason = self._critical_reason(exchanges)
        self._set_throttle(runtime, self.CRITICAL_CAUSE)
        gate = self._resolve_gate(runtime)
        if gate is not None:
            try:
                gate.set_throttled(self.CRITICAL_CAUSE)
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("health guard failed to throttle pre-trade gate", exc_info=True)
        self._engage_hold(runtime, reason)
        if self._auto_freeze:
            self._apply_freeze_rules(exchanges)

    # ------------------------------------------------------------------
    def _handle_ok(self, runtime: object) -> None:
        self._ok_streak += 1
        threshold = max(self._hysteresis, 1)
        if self._ok_streak < threshold:
            return
        self._ok_streak = 0
        self._clear_throttle(runtime)
        gate = self._resolve_gate(runtime)
        if gate is not None:
            try:
                gate.clear()
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("health guard failed to clear pre-trade gate", exc_info=True)
        if self._active_hold_reason:
            self._clear_hold(runtime)
        if self._auto_freeze:
            get_freeze_registry().clear("HEALTH_CRITICAL::")

    # ------------------------------------------------------------------
    def _set_throttle(self, runtime: object, reason: str) -> None:
        if self._throttle_reason and self._throttle_reason != reason:
            set_risk_throttled(False, self._throttle_reason)
        updater = getattr(runtime, "update_risk_throttle", None)
        if callable(updater):
            try:
                updater(True, reason=reason, source=self.HOLD_SOURCE)
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("health guard failed to update risk throttle", exc_info=True)
        set_risk_throttled(True, reason)
        self._throttle_reason = reason

    # ------------------------------------------------------------------
    def _clear_throttle(self, runtime: object) -> None:
        if not self._throttle_reason:
            return
        reason = self._throttle_reason
        updater = getattr(runtime, "update_risk_throttle", None)
        if callable(updater):
            try:
                updater(False, reason=reason, source=self.HOLD_SOURCE)
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("health guard failed to clear risk throttle", exc_info=True)
        set_risk_throttled(False, reason)
        self._throttle_reason = None

    # ------------------------------------------------------------------
    def _engage_hold(self, runtime: object, reason: str) -> None:
        if not reason:
            return
        if self._active_hold_reason is None:
            state_getter = getattr(runtime, "get_state", None)
            if callable(state_getter):
                try:
                    state = state_getter()
                except Exception:  # pragma: no cover - defensive
                    state = None
                if state is not None:
                    control = getattr(state, "control", None)
                    if control is not None:
                        self._previous_safe_mode = bool(getattr(control, "safe_mode", False))
        engager = getattr(runtime, "engage_safety_hold", None)
        engaged = False
        if callable(engager):
            try:
                engaged = bool(engager(reason, source=self.HOLD_SOURCE))
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("health guard failed to engage safety hold", exc_info=True)
        if engaged:
            self._active_hold_reason = reason

    # ------------------------------------------------------------------
    def _clear_hold(self, runtime: object) -> None:
        resume = getattr(runtime, "autopilot_apply_resume", None)
        safe_mode = bool(self._previous_safe_mode)
        if callable(resume):
            try:
                resume(safe_mode=safe_mode)
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("health guard failed to resume after hold", exc_info=True)
        self._active_hold_reason = None
        self._previous_safe_mode = None

    # ------------------------------------------------------------------
    def _critical_reason(self, exchanges: list[str]) -> str:
        if exchanges:
            exchange = str(exchanges[0] or "UNKNOWN").upper()
        else:
            exchange = "UNKNOWN"
        return f"{self.CRITICAL_REASON_PREFIX}{exchange}"

    # ------------------------------------------------------------------
    def _resolve_gate(self, runtime: object):
        gate = getattr(runtime, "pre_trade_gate", None)
        if gate is not None:
            return gate
        getter = getattr(runtime, "get_pre_trade_gate", None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # pragma: no cover - defensive
                LOGGER.debug("health guard failed to access pre-trade gate", exc_info=True)
        return None

    # ------------------------------------------------------------------
    def _update_guard(
        self,
        runtime: object,
        states: dict[str, AccountHealthState],
        worst_state: AccountHealthState,
    ) -> None:
        updater = getattr(runtime, "update_guard", None)
        if not callable(updater):
            return
        metrics = {
            "exchanges": len(states),
            "ok_streak": self._ok_streak,
            "warn": sum(1 for state in states.values() if state == "WARN"),
            "critical": sum(1 for state in states.values() if state == "CRITICAL"),
        }
        summary_map = {
            "CRITICAL": "account health critical",
            "WARN": "account health degraded",
            "OK": "account health normal",
        }
        try:
            updater("account_health", worst_state, summary_map.get(worst_state, "account health status"), metrics)
        except Exception:  # pragma: no cover - defensive
            LOGGER.debug("health guard failed to update guard snapshot", exc_info=True)

    # ------------------------------------------------------------------
    def _safe_ctx(self, *, log: bool) -> object | None:
        try:
            return self._ctx_factory()
        except Exception:
            if log:
                LOGGER.exception("health guard context factory failed")
            return None

    # ------------------------------------------------------------------
    def _resolve_health_config(self, cfg: object | None, ctx: object | None):
        candidates = [cfg]
        if cfg is not None:
            candidates.append(getattr(cfg, "data", None))
        if ctx is not None:
            candidates.append(getattr(ctx, "config", None))
            candidates.append(getattr(ctx, "state", None))
            config_obj = getattr(ctx, "config", None)
            if config_obj is not None:
                candidates.append(getattr(config_obj, "data", None))
            state_obj = getattr(ctx, "state", None)
            if state_obj is not None:
                state_config = getattr(state_obj, "config", None)
                candidates.append(state_config)
                if state_config is not None:
                    candidates.append(getattr(state_config, "data", None))
        for candidate in candidates:
            if candidate is None:
                continue
            health = getattr(candidate, "health", None)
            if health is not None:
                return health
        if HealthConfig is not None:
            return HealthConfig()
        return SimpleNamespace(
            guard_enabled=False,
            margin_ratio_warn=0.75,
            margin_ratio_critical=0.85,
            free_collateral_warn_usd=100.0,
            free_collateral_critical_usd=10.0,
            hysteresis_ok_windows=2,
        )

    # ------------------------------------------------------------------
    def _resolve_enabled(self) -> bool:
        raw = self._env.get("HEALTH_GUARD_ENABLED")
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered:
                return lowered in {"1", "true", "yes", "on"}
        return bool(getattr(self._health_cfg, "guard_enabled", False))

    # ------------------------------------------------------------------
    def _env_flag(self, name: str, default: bool = False) -> bool:
        raw = self._env.get(name)
        if isinstance(raw, str):
            lowered = raw.strip().lower()
            if lowered:
                return lowered in {"1", "true", "yes", "on"}
        return default

    # ------------------------------------------------------------------
    def _apply_freeze_rules(self, exchanges: Iterable[str]) -> None:
        registry = get_freeze_registry()
        timestamp = time.time()
        for exchange in exchanges:
            token = str(exchange or "").strip()
            if not token:
                continue
            reason = f"HEALTH_CRITICAL::{token}"
            rule = FreezeRule(reason=reason, scope="venue", ts=timestamp)
            if registry.apply(rule):
                log_operator_action(
                    "system",
                    "system",
                    "AUTO_FREEZE_APPLIED",
                    {
                        "source": "health_guard",
                        "reason": reason,
                        "scope": "venue",
                        "exchange": token,
                    },
                )


def build_health_guard_context() -> tuple[Callable[[], object], object | None]:
    """Return a context factory and config scope for the account health guard."""

    def _factory() -> object:
        state = runtime_service.get_state()
        config = getattr(getattr(state, "config", None), "data", None)
        if config is None:
            config = getattr(state, "config", None)
        return SimpleNamespace(runtime=runtime_service, state=state, config=config)

    initial = _factory()
    cfg = getattr(initial, "config", None)
    return _factory, cfg


__all__ = ["AccountHealthGuard", "build_health_guard_context"]
