from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

from . import audit_log

DEFAULT_LIMITS: Dict[str, Dict[str, float | int]] = {
    "cross_exchange_arb": {
        "daily_loss_usdt": 500.0,
        "max_consecutive_failures": 3,
    }
}


@dataclass
class StrategyState:
    realized_pnl_today: float = 0.0
    consecutive_failures: int = 0
    frozen: bool = False
    freeze_reason: Optional[str] = None
    last_update_ts: float = field(default_factory=time.time)


class StrategyRiskManager:
    """In-memory tracker for per-strategy risk counters."""

    def __init__(self, limits: Optional[Mapping[str, Mapping[str, float | int]]] = None) -> None:
        # Using plain dict copies keeps the object mutable but decoupled from callers.
        self.limits: Dict[str, Dict[str, float | int]] = {
            name: dict(spec)
            for name, spec in (limits or DEFAULT_LIMITS).items()
        }
        # TODO: persist state via runtime_state_store or disk-backed storage.
        self.state: Dict[str, StrategyState] = {
            name: StrategyState() for name in self.limits
        }

    def _ensure_strategy(self, strategy_name: str) -> StrategyState:
        if strategy_name not in self.state:
            self.state[strategy_name] = StrategyState()
        if strategy_name not in self.limits:
            self.limits[strategy_name] = {}
        return self.state[strategy_name]

    def record_fill(self, strategy_name: str, pnl_delta_usdt: float) -> None:
        state = self._ensure_strategy(strategy_name)
        state.realized_pnl_today += float(pnl_delta_usdt or 0.0)
        state.last_update_ts = time.time()
        self.check_limits(strategy_name)

    def record_failure(self, strategy_name: str, failure_reason: str) -> None:
        state = self._ensure_strategy(strategy_name)
        state.consecutive_failures += 1
        state.last_update_ts = time.time()
        self._check_limits_and_freeze(strategy_name, state)
        self.check_limits(strategy_name)

    def record_success(self, strategy_name: str) -> None:
        state = self._ensure_strategy(strategy_name)
        state.consecutive_failures = 0
        state.last_update_ts = time.time()
        # successful execution does not unfreeze automatically

    def reset_daily_if_needed(self) -> None:
        """Placeholder for daily reset logic.

        In future revisions we will persist timestamps and zero out daily counters
        when a new UTC day starts.
        """

    def check_limits(self, strategy_name: str) -> Dict[str, object]:
        state = self._ensure_strategy(strategy_name)
        limits = self.limits.get(strategy_name, {})
        breach_reasons: list[str] = []
        breach = False
        freeze_reason: str | None = None

        daily_limit = _coerce_float(limits.get("daily_loss_usdt"))
        if daily_limit is not None:
            pnl_today = state.realized_pnl_today
            if pnl_today < 0 and abs(pnl_today) > daily_limit:
                breach = True
                breach_reasons.append(
                    f"realized_pnl_today={pnl_today:.2f} below -{daily_limit:.2f} limit"
                )
                freeze_reason = freeze_reason or "pnl_limit_breach"

        max_failures = _coerce_int(limits.get("max_consecutive_failures"))
        if max_failures is not None and state.consecutive_failures > max_failures:
            breach = True
            breach_reasons.append(
                f"consecutive_failures={state.consecutive_failures} exceeds {max_failures}"
            )
            freeze_reason = freeze_reason or "too_many_failures"

        if breach and not state.frozen:
            self._freeze_strategy(
                strategy_name,
                freeze_reason or "limit_breach",
                operator_name="system",
                role="system",
            )

        snapshot = {
            "realized_pnl_today": state.realized_pnl_today,
            "consecutive_failures": state.consecutive_failures,
            "frozen": state.frozen,
            "freeze_reason": state.freeze_reason,
            "reason": state.freeze_reason,
            "last_update_ts": state.last_update_ts,
        }

        return {
            "breach": breach,
            "frozen": state.frozen,
            "breach_reasons": breach_reasons,
            "snapshot": snapshot,
            "limits": dict(limits),
        }

    def full_snapshot(self) -> Dict[str, object]:
        strategies: Dict[str, Dict[str, object]] = {}
        all_names = set(self.limits) | set(self.state)
        for name in sorted(all_names):
            result = self.check_limits(name)
            strategies[name] = {
                "limits": result.get("limits", {}),
                "state": result.get("snapshot", {}),
                "breach": bool(result.get("breach")),
                "breach_reasons": list(result.get("breach_reasons", [])),
                "frozen": bool(result.get("frozen")),
            }
        return {
            "timestamp": time.time(),
            "strategies": strategies,
        }

    def is_frozen(self, strategy_name: str) -> bool:
        state = self._ensure_strategy(strategy_name)
        return bool(state.frozen)

    def unfreeze_strategy(
        self,
        strategy_name: str,
        *,
        operator_name: str,
        role: str,
        reason: str,
    ) -> None:
        state = self._ensure_strategy(strategy_name)
        state.frozen = False
        state.freeze_reason = ""
        state.consecutive_failures = 0
        state.last_update_ts = time.time()
        audit_log.log_operator_action(
            operator_name=operator_name,
            role=role,
            action="STRATEGY_UNFREEZE_MANUAL",
            details={"strategy": strategy_name, "reason": reason},
        )

    def _freeze_strategy(
        self,
        strategy_name: str,
        reason: str,
        *,
        operator_name: str,
        role: str,
    ) -> None:
        state = self._ensure_strategy(strategy_name)
        if state.frozen and state.freeze_reason == reason:
            return
        state.frozen = True
        state.freeze_reason = reason
        state.last_update_ts = time.time()
        audit_log.log_operator_action(
            operator_name=operator_name,
            role=role,
            action="STRATEGY_AUTO_FREEZE",
            details={"strategy": strategy_name, "reason": reason},
        )

    def _check_limits_and_freeze(self, strategy_name: str, state: StrategyState) -> None:
        limits = self.limits.get(strategy_name, {})
        max_failures = _coerce_int(limits.get("max_consecutive_failures"))
        if max_failures is None:
            return
        if state.consecutive_failures > max_failures:
            self._freeze_strategy(
                strategy_name,
                "too_many_failures",
                operator_name="system",
                role="system",
            )


def _coerce_float(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: object) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


_STRATEGY_RISK_MANAGER: StrategyRiskManager | None = None


def get_strategy_risk_manager() -> StrategyRiskManager:
    global _STRATEGY_RISK_MANAGER
    if _STRATEGY_RISK_MANAGER is None:
        _STRATEGY_RISK_MANAGER = StrategyRiskManager()
    return _STRATEGY_RISK_MANAGER


def reset_strategy_risk_manager_for_tests() -> None:
    global _STRATEGY_RISK_MANAGER
    _STRATEGY_RISK_MANAGER = StrategyRiskManager()


__all__ = [
    "DEFAULT_LIMITS",
    "StrategyRiskManager",
    "get_strategy_risk_manager",
    "reset_strategy_risk_manager_for_tests",
]
