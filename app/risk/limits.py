"""In-memory risk limits enforcement."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Dict, Optional, Tuple


LOGGER = logging.getLogger(__name__)


@dataclass
class RiskConfig:
    cap_per_venue: Dict[str, Decimal] = field(default_factory=dict)
    cap_per_symbol: Dict[Tuple[str, str], Decimal] = field(default_factory=dict)
    cap_per_strategy: Dict[str, Decimal] = field(default_factory=dict)
    daily_loss_limit: Optional[Decimal] = None
    daily_cooloff_sec: int = 3600
    max_consecutive_rejects: int = 3
    rejects_cooloff_sec: int = 300


@dataclass
class RiskState:
    day_ymd: str = ""
    realized_pnl: Decimal = Decimal("0")
    in_cooloff_until: int = 0
    rejects: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    key_cooloff_until: Dict[Tuple[str, str, str], int] = field(default_factory=dict)


class RiskGovernor:
    def __init__(self, cfg: RiskConfig):
        self.cfg = cfg
        self.state = RiskState()

    def _today(self, now_s: int) -> str:
        return time.strftime("%Y%m%d", time.gmtime(now_s))

    def _reset_day_if_needed(self, now_s: int) -> None:
        today = self._today(now_s)
        if self.state.day_ymd == today:
            return
        self.state.day_ymd = today
        self.state.realized_pnl = Decimal("0")
        self.state.in_cooloff_until = 0
        self.state.rejects.clear()
        self.state.key_cooloff_until.clear()

    def _check_global_cooloff(self, now_s: int) -> Optional[str]:
        expiry = self.state.in_cooloff_until
        if expiry <= 0:
            return None
        if now_s >= expiry:
            self.state.in_cooloff_until = 0
            return None
        return "daily_cooloff"

    def _check_key_cooloff(self, key: Tuple[str, str, str], now_s: int) -> Optional[str]:
        expiry = self.state.key_cooloff_until.get(key)
        if expiry is None:
            return None
        if now_s >= expiry:
            self.state.key_cooloff_until.pop(key, None)
            self.state.rejects.pop(key, None)
            return None
        return "key_cooloff"

    def _check_notional_caps(
        self,
        venue: str,
        symbol: str,
        strategy: str,
        notional: Decimal,
    ) -> Optional[str]:
        venue_cap = self.cfg.cap_per_venue.get(venue)
        if venue_cap is not None and notional > venue_cap:
            return "venue_cap"
        symbol_cap = self.cfg.cap_per_symbol.get((venue, symbol))
        if symbol_cap is not None and notional > symbol_cap:
            return "symbol_cap"
        strategy_cap = self.cfg.cap_per_strategy.get(strategy)
        if strategy_cap is not None and notional > strategy_cap:
            return "strategy_cap"
        return None

    def allow_order(
        self,
        venue: str,
        symbol: str,
        strategy: str,
        price: Decimal,
        qty: Decimal,
        now_s: Optional[int] = None,
    ) -> Tuple[bool, str]:
        now_value = int(now_s if now_s is not None else time.time())
        self._reset_day_if_needed(now_value)
        reason = self._check_global_cooloff(now_value)
        if reason is not None:
            return False, reason
        key = (venue, symbol, strategy)
        reason = self._check_key_cooloff(key, now_value)
        if reason is not None:
            return False, reason
        qty_abs = qty.copy_abs()
        price_abs = price.copy_abs()
        notional = price_abs * qty_abs
        reason = self._check_notional_caps(venue, symbol, strategy, notional)
        if reason is not None:
            return False, reason
        return True, ""

    def on_reject(
        self,
        venue: str,
        symbol: str,
        strategy: str,
        now_s: Optional[int] = None,
    ) -> None:
        now_value = int(now_s if now_s is not None else time.time())
        self._reset_day_if_needed(now_value)
        if self.cfg.max_consecutive_rejects <= 0:
            return
        key = (venue, symbol, strategy)
        count = self.state.rejects.get(key, 0) + 1
        self.state.rejects[key] = count
        if count >= self.cfg.max_consecutive_rejects:
            until = now_value + max(self.cfg.rejects_cooloff_sec, 0)
            self.state.key_cooloff_until[key] = until
            self.state.rejects[key] = 0
            LOGGER.warning(
                "risk.governor_reject_cooloff",
                extra={
                    "event": "risk_governor_reject_cooloff",
                    "component": "risk_limits",
                    "details": {
                        "venue": venue,
                        "symbol": symbol,
                        "strategy": strategy,
                        "cooloff_until": until,
                    },
                },
            )

    def on_ack(self, venue: str, symbol: str, strategy: str) -> None:
        key = (venue, symbol, strategy)
        self.state.rejects.pop(key, None)
        self.state.key_cooloff_until.pop(key, None)

    def on_filled(
        self,
        venue: str,
        symbol: str,
        strategy: str,
        realized_pnl: Decimal,
        now_s: Optional[int] = None,
    ) -> None:
        now_value = int(now_s if now_s is not None else time.time())
        self._reset_day_if_needed(now_value)
        self.state.realized_pnl += realized_pnl
        limit = self.cfg.daily_loss_limit
        if limit is None or limit <= Decimal("0"):
            return
        if self.state.realized_pnl <= -limit:
            until = now_value + max(self.cfg.daily_cooloff_sec, 0)
            if until > self.state.in_cooloff_until:
                self.state.in_cooloff_until = until
            LOGGER.warning(
                "risk.governor_daily_cooloff",
                extra={
                    "event": "risk_governor_daily_cooloff",
                    "component": "risk_limits",
                    "details": {
                        "venue": venue,
                        "symbol": symbol,
                        "strategy": strategy,
                        "realized_pnl": str(self.state.realized_pnl),
                        "cooloff_until": self.state.in_cooloff_until,
                    },
                },
            )


def load_config_from_env() -> RiskConfig:
    import os

    def _parse_map(s: str, sep_items: str = ",", sep_kv: str = ":") -> Dict[str, Decimal]:
        out: Dict[str, Decimal] = {}
        if not s:
            return out
        for item in filter(None, (x.strip() for x in s.split(sep_items))):
            k, v = item.split(sep_kv, 1)
            out[k.strip()] = Decimal(v.strip())
        return out

    def _parse_map3(s: str) -> Dict[Tuple[str, str], Decimal]:
        out: Dict[Tuple[str, str], Decimal] = {}
        if not s:
            return out
        for item in filter(None, (x.strip() for x in s.split(";"))):
            a, b, v = item.split(":", 2)
            out[(a.strip(), b.strip())] = Decimal(v.strip())
        return out

    cfg = RiskConfig()
    cfg.cap_per_venue = _parse_map(os.getenv("RISK_CAP_VENUE", ""))
    cfg.cap_per_symbol = _parse_map3(os.getenv("RISK_CAP_SYMBOL", ""))
    cfg.cap_per_strategy = _parse_map(os.getenv("RISK_CAP_STRATEGY", ""))
    dloss = os.getenv("RISK_DAILY_LOSS", "").strip()
    if dloss:
        cfg.daily_loss_limit = Decimal(dloss)
    cfg.daily_cooloff_sec = int(os.getenv("RISK_DAILY_COOLOFF_SEC", "3600"))
    cfg.max_consecutive_rejects = int(os.getenv("RISK_MAX_CONSEC_REJECTS", "3"))
    cfg.rejects_cooloff_sec = int(os.getenv("RISK_REJECTS_COOLOFF_SEC", "300"))
    return cfg


__all__ = ["RiskConfig", "RiskState", "RiskGovernor", "load_config_from_env"]
