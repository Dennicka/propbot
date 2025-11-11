"""Utilities for tracking and enforcing the bot-wide daily loss cap."""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict

from prometheus_client import Gauge

from ..metrics import slo
from ..metrics.runtime import record_risk_breach, set_daily_loss_breach
from .core import FeatureFlags

__all__ = [
    "DailyLossCap",
    "get_daily_loss_cap",
    "reset_daily_loss_cap_for_tests",
    "get_daily_loss_cap_state",
    "is_daily_loss_cap_breached",
]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _current_utc_day(timestamp: datetime | None = None) -> datetime.date:
    ts = timestamp or _utc_now()
    return ts.date()


_DAILY_LOSS_REALIZED_GAUGE = Gauge(
    "bot_daily_loss_realized_usdt",
    "Realised bot-wide daily PnL in USDT",
)

_DAILY_LOSS_CAP_GAUGE = Gauge(
    "bot_daily_loss_cap_usdt",
    "Configured bot-wide daily loss cap in USDT",
)


@dataclass(slots=True)
class DailyLossSnapshot:
    utc_day: str
    realized_pnl_today_usdt: float
    max_daily_loss_usdt: float
    percentage_used: float
    losses_usdt: float
    remaining_usdt: float | None
    breached: bool
    enabled: bool
    blocking: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "utc_day": self.utc_day,
            "realized_pnl_today_usdt": self.realized_pnl_today_usdt,
            "max_daily_loss_usdt": self.max_daily_loss_usdt,
            "percentage_used": self.percentage_used,
            "losses_usdt": self.losses_usdt,
            "remaining_usdt": self.remaining_usdt,
            "breached": self.breached,
            "enabled": self.enabled,
            "blocking": self.blocking,
            "cap_usdt": self.max_daily_loss_usdt,
            "realized_today_usdt": self.realized_pnl_today_usdt,
        }


class DailyLossCap:
    """Track bot-wide realised PnL and evaluate it against a loss limit."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._realized_today = 0.0
        self._utc_day: datetime.date | None = None
        self._breach_active = False

    @staticmethod
    def _read_cap_from_env() -> float:
        raw = os.getenv("DAILY_LOSS_CAP_USDT", "0")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return 0.0
        if value <= 0:
            return 0.0
        return float(value)

    @property
    def max_daily_loss_usdt(self) -> float:
        return self._read_cap_from_env()

    @property
    def enabled(self) -> bool:
        return FeatureFlags.enforce_daily_loss_cap()

    def _ensure_current_day_locked(self, *, timestamp: datetime | None = None) -> None:
        day = _current_utc_day(timestamp)
        if self._utc_day is None:
            self._utc_day = day
            return
        if self._utc_day != day:
            self._utc_day = day
            self._realized_today = 0.0

    def maybe_reset(self) -> None:
        with self._lock:
            self._ensure_current_day_locked()
            limit = self.max_daily_loss_usdt
            _DAILY_LOSS_CAP_GAUGE.set(limit)
            _DAILY_LOSS_REALIZED_GAUGE.set(self._realized_today)
            losses = self._losses_today_locked()
            breached = limit > 0 and losses >= limit - 1e-6
            self._update_breach_state_locked(breached)

    def record_realized(self, pnl_delta: float, *, timestamp: datetime | None = None) -> None:
        with self._lock:
            self._ensure_current_day_locked(timestamp=timestamp)
            self._realized_today += float(pnl_delta or 0.0)
            limit = self.max_daily_loss_usdt
            _DAILY_LOSS_CAP_GAUGE.set(limit)
            _DAILY_LOSS_REALIZED_GAUGE.set(self._realized_today)
            losses = self._losses_today_locked()
            breached = limit > 0 and losses >= limit - 1e-6
            self._update_breach_state_locked(breached)

    def _losses_today_locked(self) -> float:
        return max(-self._realized_today, 0.0)

    def _update_breach_state_locked(self, breached: bool) -> None:
        set_daily_loss_breach(breached)
        slo.set_daily_loss_breached(breached)
        if breached:
            if not self._breach_active:
                record_risk_breach("daily_loss")
        else:
            self._breach_active = False
            return
        self._breach_active = breached

    def is_breached(self) -> bool:
        with self._lock:
            self._ensure_current_day_locked()
            limit = self.max_daily_loss_usdt
            if limit <= 0:
                self._update_breach_state_locked(False)
                return False
            tolerance = 1e-6
            breached = self._losses_today_locked() >= limit - tolerance
            self._update_breach_state_locked(breached)
            return breached

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._ensure_current_day_locked()
            limit = self.max_daily_loss_usdt
            losses = self._losses_today_locked()
            breached = False
            percentage_used = 0.0
            remaining: float | None = None
            if limit > 0:
                percentage_used = min((losses / limit) * 100.0, 1_000.0)
                breached = losses >= limit - 1e-6
                remaining = limit - losses
            self._update_breach_state_locked(breached)
            snapshot = DailyLossSnapshot(
                utc_day=(self._utc_day or _current_utc_day()).isoformat(),
                realized_pnl_today_usdt=self._realized_today,
                max_daily_loss_usdt=limit,
                percentage_used=percentage_used,
                losses_usdt=losses,
                remaining_usdt=remaining,
                breached=breached,
                enabled=self.enabled,
                blocking=self.enabled and limit > 0,
            )
            _DAILY_LOSS_CAP_GAUGE.set(limit)
            _DAILY_LOSS_REALIZED_GAUGE.set(self._realized_today)
            return snapshot.as_dict()

    def reset_for_tests(self) -> None:
        with self._lock:
            self._utc_day = _current_utc_day()
            self._realized_today = 0.0
            limit = self.max_daily_loss_usdt
            _DAILY_LOSS_CAP_GAUGE.set(limit)
            _DAILY_LOSS_REALIZED_GAUGE.set(self._realized_today)
            slo.set_daily_loss_breached(False)
            set_daily_loss_breach(False)
            self._breach_active = False


_SINGLETON: DailyLossCap | None = None
_SINGLETON_LOCK = threading.RLock()


def get_daily_loss_cap() -> DailyLossCap:
    global _SINGLETON
    if _SINGLETON is not None:
        return _SINGLETON
    with _SINGLETON_LOCK:
        if _SINGLETON is None:
            _SINGLETON = DailyLossCap()
    return _SINGLETON


def reset_daily_loss_cap_for_tests() -> None:
    with _SINGLETON_LOCK:
        global _SINGLETON
        if _SINGLETON is not None:
            _SINGLETON.reset_for_tests()


def get_daily_loss_cap_state() -> Dict[str, Any]:
    return get_daily_loss_cap().snapshot()


def is_daily_loss_cap_breached() -> bool:
    return get_daily_loss_cap().is_breached()
