from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Mapping, Optional, Tuple

from app.alerts.events import evt_pnl_cap
from app.alerts.levels import AlertLevel
from app.alerts.manager import notify as alert_notify
from app.alerts.pipeline import OpsAlertsPipeline, PNL_CAP_BREACHED
from app.ops.hooks import ops_alert


def _today_key(t: float, tz: str) -> str:
    if tz.upper() == "UTC":
        dt = datetime.fromtimestamp(t, tz=timezone.utc)
    else:
        dt = datetime.fromtimestamp(t)
    return dt.strftime("%Y-%m-%d")


@dataclass
class FillEvent:
    t: float
    strategy: str
    symbol: str
    realized_pnl_usd: Decimal


@dataclass
class DayStats:
    realized_total: Decimal = Decimal("0")
    peak: Decimal = Decimal("0")
    last: Decimal = Decimal("0")
    cooloff_until: float = 0.0


def _current_profile() -> str | None:
    raw = os.getenv("EXEC_PROFILE")
    if raw is None:
        return None
    value = raw.strip()
    return value or None


class PnLAggregator:
    """Хранит дневные кривые PnL: глобально и по стратегиям."""

    def __init__(self, tz: str = "UTC"):
        self._tz = tz
        self._global: Dict[str, DayStats] = {}
        self._by_strat: Dict[tuple[str, str], DayStats] = {}

    def _get(self, day_key: str, strategy: Optional[str]) -> DayStats:
        if strategy is None:
            return self._global.setdefault(day_key, DayStats())
        return self._by_strat.setdefault((day_key, strategy), DayStats())

    def on_fill(self, ev: FillEvent) -> None:
        day = _today_key(ev.t, self._tz)
        global_stats = self._get(day, None)
        global_stats.last += ev.realized_pnl_usd
        if global_stats.last > global_stats.peak:
            global_stats.peak = global_stats.last
        strat_stats = self._get(day, ev.strategy)
        strat_stats.last += ev.realized_pnl_usd
        if strat_stats.last > strat_stats.peak:
            strat_stats.peak = strat_stats.last

    def snapshot(self, now: Optional[float] = None) -> Dict[str, object]:
        t = now or time.time()
        day = _today_key(float(t), self._tz)
        global_stats = self._global.get(day, DayStats())
        per = {key[1]: value for key, value in self._by_strat.items() if key[0] == day}
        return {"day": day, "global": global_stats, "per_strat": per}


class CapsPolicy:
    def __init__(self) -> None:
        self.enabled = os.environ.get("FF_DAILY_LOSS_CAP", "0") == "1"
        self.tz = os.environ.get("PNL_TZ", "UTC")
        self.cap_global = Decimal(os.environ.get("DAILY_LOSS_CAP_USD_GLOBAL", "0") or "0")
        self.dd_global = Decimal(os.environ.get("INTRADAY_DRAWDOWN_CAP_USD_GLOBAL", "0") or "0")
        self.cap_per = self._json_decimal_map(
            os.environ.get("DAILY_LOSS_CAP_USD_PER_STRAT", "") or "{}"
        )
        self.dd_per = self._json_decimal_map(
            os.environ.get("INTRADAY_DRAWDOWN_CAP_USD_PER_STRAT", "") or "{}"
        )
        self.cooloff_min = int(os.environ.get("PNL_CAPS_COOLOFF_MIN", "30"))
        self.report_every = int(os.environ.get("PNL_CAPS_REPORT_EVERY_SEC", "5"))
        self._last_report_ts = 0.0

    def _json_decimal_map(self, raw: str) -> Dict[str, Decimal]:
        try:
            obj = json.loads(raw) if raw else {}
            return {key: Decimal(str(value)) for key, value in obj.items()}
        except (ArithmeticError, TypeError, ValueError):
            return {}


class PnLCapsGuard:
    """Проверяет капы и управляет cool-off окнами."""

    def __init__(
        self,
        policy: CapsPolicy,
        agg: PnLAggregator,
        clock=time,
        *,
        alerts: OpsAlertsPipeline | None = None,
    ) -> None:
        self.p = policy
        self.agg = agg
        self.clock = clock
        self._alerts = alerts

    def _notify(
        self,
        scope: str,
        reason: str,
        *,
        current: Decimal | None = None,
        cap: Decimal | None = None,
        window: str | None = None,
        extra: Mapping[str, object] | None = None,
    ) -> None:
        context: Dict[str, object] = {"scope": scope, "reason": reason}
        if current is not None:
            context["current_pnl"] = current
        if cap is not None:
            context["pnl_cap"] = cap
        if window is not None:
            context["window"] = window
        if extra:
            context.update(dict(extra))
        profile = _current_profile()
        if profile:
            context["profile"] = profile
        if self._alerts is not None:
            self._alerts.notify_event(
                event_type=PNL_CAP_BREACHED,
                message=f"PnL cap breached: {scope}",
                level=AlertLevel.CRITICAL,
                context=context,
            )
        ops_alert(evt_pnl_cap(scope=scope, reason=reason))
        alert_notify(
            AlertLevel.CRITICAL,
            f"PnL cap triggered: {scope}",
            source="pnl-cap",
            scope=scope,
            reason=reason,
        )

    def _cool(self, stats: DayStats) -> None:
        now = self.clock.time()
        if stats.cooloff_until <= now:
            stats.cooloff_until = now + self.p.cooloff_min * 60

    def should_block(self, strategy: str) -> Tuple[bool, str]:
        if not self.p.enabled:
            return (False, "off")
        now = self.clock.time()
        snap = self.agg.snapshot(now=now)
        global_stats: DayStats = snap["global"]  # type: ignore[assignment]
        per = snap["per_strat"]  # type: ignore[assignment]
        strat_stats: DayStats = per.get(strategy, DayStats())

        if global_stats.cooloff_until > now:
            reason = f"cooloff-global-{int(global_stats.cooloff_until - now)}s"
            self._notify(
                "global",
                reason,
                current=global_stats.last,
                cap=self.p.cap_global,
                window="daily",
                extra={
                    "cooloff_until": global_stats.cooloff_until,
                    "remaining_seconds": int(global_stats.cooloff_until - now),
                },
            )
            return (True, reason)
        if strat_stats.cooloff_until > now:
            reason = f"cooloff-{strategy}-{int(strat_stats.cooloff_until - now)}s"
            self._notify(
                strategy,
                reason,
                current=strat_stats.last,
                cap=self.p.cap_per.get(strategy, Decimal("0")),
                window="daily",
                extra={
                    "cooloff_until": strat_stats.cooloff_until,
                    "remaining_seconds": int(strat_stats.cooloff_until - now),
                },
            )
            return (True, reason)

        if self.p.cap_global > 0 and global_stats.last <= -self.p.cap_global:
            self._cool(global_stats)
            reason = "daily-loss-cap-global"
            self._notify(
                "global",
                reason,
                current=global_stats.last,
                cap=self.p.cap_global,
                window="daily",
            )
            return (True, reason)

        cap_strat = self.p.cap_per.get(strategy, Decimal("0"))
        if cap_strat > 0 and strat_stats.last <= -cap_strat:
            self._cool(strat_stats)
            reason = f"daily-loss-cap-{strategy}"
            self._notify(
                strategy,
                reason,
                current=strat_stats.last,
                cap=cap_strat,
                window="daily",
            )
            return (True, reason)

        if self.p.dd_global > 0 and (global_stats.peak - global_stats.last) > self.p.dd_global:
            self._cool(global_stats)
            reason = "drawdown-cap-global"
            self._notify(
                "global",
                reason,
                current=global_stats.last,
                cap=self.p.dd_global,
                window="drawdown",
                extra={"peak": global_stats.peak},
            )
            return (True, reason)

        dd_strat = self.p.dd_per.get(strategy, Decimal("0"))
        if dd_strat > 0 and (strat_stats.peak - strat_stats.last) > dd_strat:
            self._cool(strat_stats)
            reason = f"drawdown-cap-{strategy}"
            self._notify(
                strategy,
                reason,
                current=strat_stats.last,
                cap=dd_strat,
                window="drawdown",
                extra={"peak": strat_stats.peak},
            )
            return (True, reason)

        return (False, "ok")
