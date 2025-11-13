from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional, Set, Tuple


@dataclass
class StrategyBudget:
    max_notional_usd: Decimal
    max_positions: int
    per_symbol_max_notional_usd: Dict[str, Decimal]


@dataclass
class Reserve:
    order_id: str
    strategy: str
    symbol: str
    notional_usd: Decimal
    ts: float


class BudgetRegistry:
    def __init__(self, ttl_sec: int = 600, max_reservations: int = 50_000) -> None:
        self._ttl = ttl_sec
        self._max = max_reservations
        self._res: Dict[str, Reserve] = {}

    def cleanup(self, now: Optional[float] = None) -> int:
        reference = float(now) if now is not None else time.time()
        removed = 0
        for order_id, reserve in list(self._res.items()):
            if reference - reserve.ts > self._ttl:
                self._res.pop(order_id, None)
                removed += 1
        overflow = len(self._res) - self._max
        if overflow > 0:
            for order_id, reserve in sorted(self._res.items(), key=lambda item: item[1].ts)[
                :overflow
            ]:
                self._res.pop(order_id, None)
                removed += 1
        return removed

    def reserve(
        self,
        order_id: str,
        strategy: str,
        symbol: str,
        notional_usd: Decimal,
        now: Optional[float] = None,
    ) -> None:
        timestamp = float(now) if now is not None else time.time()
        self._res[order_id] = Reserve(order_id, strategy, symbol, notional_usd, timestamp)

    def release(self, order_id: str) -> None:
        self._res.pop(order_id, None)

    def snapshot(self) -> Dict[str, object]:
        total_by_strategy: Dict[str, Decimal] = {}
        symbols_by_strategy: Dict[str, Set[str]] = {}
        per_symbol_by_strategy: Dict[Tuple[str, str], Decimal] = {}
        for reserve in self._res.values():
            total_by_strategy[reserve.strategy] = (
                total_by_strategy.get(reserve.strategy, Decimal("0")) + reserve.notional_usd
            )
            symbols_by_strategy.setdefault(reserve.strategy, set()).add(reserve.symbol)
            key = (reserve.strategy, reserve.symbol)
            per_symbol_by_strategy[key] = (
                per_symbol_by_strategy.get(key, Decimal("0")) + reserve.notional_usd
            )
        return {
            "total_by_strategy": total_by_strategy,
            "symbols_by_strategy": {
                strategy: len(symbols) for strategy, symbols in symbols_by_strategy.items()
            },
            "per_symbol_by_strategy": per_symbol_by_strategy,
        }


class RiskBudgets:
    def __init__(self) -> None:
        self.policies: Dict[str, StrategyBudget] = self._load_policies()
        ttl_seconds = self._safe_int(os.environ.get("RISK_BUDGETS_TTL_SEC"), default=600)
        max_reservations = self._safe_int(
            os.environ.get("RISK_BUDGETS_MAX_RESERVATIONS"), default=50_000
        )
        ttl_seconds = max(ttl_seconds, 0)
        max_reservations = max(max_reservations, 0)
        self.reg = BudgetRegistry(ttl_seconds, max_reservations)

    def _load_policies(self) -> Dict[str, StrategyBudget]:
        raw = os.environ.get("RISK_BUDGETS_JSON", "").strip()
        default: Dict[str, object] = {}
        try:
            payload = json.loads(raw) if raw else default
        except (TypeError, ValueError):
            payload = default
        policies: Dict[str, StrategyBudget] = {}
        for strategy, config in payload.items():
            if not isinstance(config, dict):
                continue
            max_notional = Decimal(str(config.get("max_notional_usd", "0")))
            max_positions = int(config.get("max_positions", 0))
            per_symbol_mapping = {
                str(symbol): Decimal(str(value))
                for symbol, value in config.get("per_symbol_max_notional_usd", {}).items()
            }
            policies[str(strategy)] = StrategyBudget(
                max_notional_usd=max_notional,
                max_positions=max_positions,
                per_symbol_max_notional_usd=per_symbol_mapping,
            )
        return policies

    def can_accept(
        self,
        strategy: str,
        symbol: str,
        add_notional_usd: Decimal,
        now: Optional[float] = None,
    ) -> Tuple[bool, str]:
        self.reg.cleanup(now=now)
        policy = self.policies.get(strategy)
        if policy is None:
            return True, "no-policy"
        snapshot = self.reg.snapshot()
        current_total = snapshot["total_by_strategy"].get(strategy, Decimal("0"))
        total_after = current_total + add_notional_usd
        if policy.max_notional_usd and total_after > policy.max_notional_usd:
            return False, "max_notional_exceeded"
        current_symbol_total = snapshot["per_symbol_by_strategy"].get(
            (strategy, symbol), Decimal("0")
        )
        symbol_limit = policy.per_symbol_max_notional_usd.get(symbol)
        if symbol_limit is not None and current_symbol_total + add_notional_usd > symbol_limit:
            return False, "per_symbol_max_notional_exceeded"
        open_positions = snapshot["symbols_by_strategy"].get(strategy, 0)
        if (
            policy.max_positions
            and current_symbol_total == 0
            and open_positions + 1 > policy.max_positions
        ):
            return False, "max_positions_exceeded"
        return True, "ok"

    @staticmethod
    def _safe_int(raw: Optional[str], *, default: int) -> int:
        if raw is None:
            return default
        token = str(raw).strip()
        if not token:
            return default
        try:
            return int(token)
        except (TypeError, ValueError):
            return default


_RISK_BUDGETS_SINGLETON = RiskBudgets()


def get_risk_budgets() -> RiskBudgets:
    return _RISK_BUDGETS_SINGLETON
