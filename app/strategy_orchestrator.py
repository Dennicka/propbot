"""In-memory strategy orchestrator state tracking."""

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, Optional, Set

from .audit_log import log_operator_action


@dataclass
class _OrchestratorState:
    enabled_strategies: Set[str] = field(default_factory=set)
    autopilot_active: bool = False
    last_decision_ts: str | None = None


class StrategyOrchestrator:
    """Manage strategy enablement decisions and related audit trail."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._state = _OrchestratorState()

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _normalise_name(self, name: str) -> str:
        normalised = name.strip()
        if not normalised:
            raise ValueError("strategy name must be non-empty")
        return normalised

    def enable_strategy(
        self,
        name: str,
        reason: str,
        *,
        operator: str = "unknown",
        role: str = "unknown",
    ) -> None:
        strategy = self._normalise_name(name)
        with self._lock:
            self._state.enabled_strategies.add(strategy)
            self._state.last_decision_ts = self._timestamp()
        log_operator_action(
            operator,
            role,
            action=f"enable_strategy:{strategy}",
            channel="orchestrator",
            details={"strategy": strategy, "reason": reason},
        )

    def disable_strategy(
        self,
        name: str,
        reason: str,
        *,
        operator: str = "unknown",
        role: str = "unknown",
    ) -> None:
        strategy = self._normalise_name(name)
        with self._lock:
            self._state.enabled_strategies.discard(strategy)
            self._state.last_decision_ts = self._timestamp()
        log_operator_action(
            operator,
            role,
            action=f"disable_strategy:{strategy}",
            channel="orchestrator",
            details={"strategy": strategy, "reason": reason},
        )

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            enabled = sorted(self._state.enabled_strategies)
            return {
                "enabled_strategies": enabled,
                "autopilot_active": self._state.autopilot_active,
                "last_decision_ts": self._state.last_decision_ts,
            }

    def set_autopilot_active(self, active: bool) -> None:
        with self._lock:
            self._state.autopilot_active = bool(active)
            self._state.last_decision_ts = self._timestamp()

    def bulk_enable(
        self,
        strategies: Iterable[str],
        *,
        operator: str = "unknown",
        role: str = "unknown",
        reason: str = "bulk_enable",
    ) -> None:
        names = [self._normalise_name(name) for name in strategies]
        with self._lock:
            self._state.enabled_strategies.update(names)
            self._state.last_decision_ts = self._timestamp()
        for name in names:
            log_operator_action(
                operator,
                role,
                action=f"enable_strategy:{name}",
                channel="orchestrator",
                details={"strategy": name, "reason": reason},
            )


_GLOBAL_ORCHESTRATOR: Optional[StrategyOrchestrator] = None


def get_strategy_orchestrator() -> StrategyOrchestrator:
    global _GLOBAL_ORCHESTRATOR
    if _GLOBAL_ORCHESTRATOR is None:
        _GLOBAL_ORCHESTRATOR = StrategyOrchestrator()
    return _GLOBAL_ORCHESTRATOR


def reset_strategy_orchestrator(
    orchestrator: Optional[StrategyOrchestrator] = None,
) -> StrategyOrchestrator:
    global _GLOBAL_ORCHESTRATOR
    if orchestrator is None:
        orchestrator = StrategyOrchestrator()
    _GLOBAL_ORCHESTRATOR = orchestrator
    return orchestrator


__all__ = [
    "StrategyOrchestrator",
    "get_strategy_orchestrator",
    "reset_strategy_orchestrator",
]
