from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, Mapping, Optional

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
    reason: Optional[str] = None
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

    def record_failure(self, strategy_name: str, failure_reason: str) -> None:
        state = self._ensure_strategy(strategy_name)
        state.consecutive_failures += 1
        state.reason = failure_reason or state.reason
        state.last_update_ts = time.time()

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

        daily_limit = _coerce_float(limits.get("daily_loss_usdt"))
        if daily_limit is not None:
            pnl_today = state.realized_pnl_today
            if pnl_today < 0 and abs(pnl_today) > daily_limit:
                breach = True
                breach_reasons.append(
                    f"realized_pnl_today={pnl_today:.2f} below -{daily_limit:.2f} limit"
                )

        max_failures = _coerce_int(limits.get("max_consecutive_failures"))
        if max_failures is not None and state.consecutive_failures > max_failures:
            breach = True
            breach_reasons.append(
                f"consecutive_failures={state.consecutive_failures} exceeds {max_failures}"
            )

        snapshot = {
            "realized_pnl_today": state.realized_pnl_today,
            "consecutive_failures": state.consecutive_failures,
            "frozen": state.frozen,
            "reason": state.reason,
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
