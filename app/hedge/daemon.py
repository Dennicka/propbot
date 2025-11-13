from __future__ import annotations

import os
import time
from time import perf_counter
from typing import Dict, Protocol

from app.hedge.policy import Exposure, HedgeLeg, HedgePolicy, Quote
from app.metrics.core import counter as metrics_counter, histogram as metrics_histogram


class PositionProvider(Protocol):
    def get_exposure_usd(self, symbols: list[str] | None) -> dict[str, Exposure]: ...


class QuoteProvider(Protocol):
    def get_quotes(self, symbol: str) -> dict[str, Quote]: ...


class RouterAdapter(Protocol):
    def submit_hedge_leg(self, leg: HedgeLeg) -> dict: ...


_HEDGE_TICK_TOTAL = metrics_counter("propbot_hedge_tick_total")
_HEDGE_BLOCKED_TOTAL = metrics_counter("propbot_hedge_blocked_total", labels=("reason",))
_HEDGE_SUBMIT_TOTAL = metrics_counter("propbot_hedge_submit_total", labels=("result", "reason"))
_HEDGE_COMPUTE_MS = metrics_histogram("propbot_hedge_compute_ms")


def _env_flag(name: str) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return False
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_symbols(raw: str | None) -> list[str]:
    if not raw:
        return []
    tokens = [token.strip().upper() for token in raw.split(",") if token.strip()]
    return tokens


class AutoHedgeDaemon:
    def __init__(
        self,
        *,
        policy: HedgePolicy,
        pos_provider: PositionProvider,
        quote_provider: QuoteProvider,
        router: RouterAdapter,
        clock=time,
        tick_ms: int | None = None,
        cooldown_sec: float | None = None,
    ) -> None:
        self._policy = policy
        self._positions = pos_provider
        self._quotes = quote_provider
        self._router = router
        self._clock = clock
        self._tick_ms = tick_ms if tick_ms is not None else max(_env_int("HEDGE_TICK_MS", 250), 1)
        cooldown_default = _env_int("HEDGE_COOLDOWN_SEC", 5)
        self._cooldown_sec = cooldown_sec if cooldown_sec is not None else float(cooldown_default)
        self._cooldowns: Dict[str, float] = {}

    def _feature_enabled(self) -> bool:
        return _env_flag("FF_AUTO_HEDGE")

    def _allowed_symbols(self) -> list[str]:
        symbols = _parse_symbols(os.getenv("HEDGE_SYMBOLS"))
        return symbols

    def tick(self) -> dict[str, object]:
        started = perf_counter()
        _HEDGE_TICK_TOTAL.inc()
        summary: dict[str, object] = {"processed": []}
        if not self._feature_enabled():
            summary["status"] = "disabled"
            _HEDGE_COMPUTE_MS.observe((perf_counter() - started) * 1000.0)
            return summary

        symbols = self._allowed_symbols()
        symbol_filter = symbols if symbols else None
        exposures = self._positions.get_exposure_usd(symbol_filter)
        now_sec = float(self._clock.time())

        for symbol, exposure in sorted(exposures.items()):
            cooldown_until = self._cooldowns.get(symbol)
            if cooldown_until is not None and cooldown_until > now_sec:
                _HEDGE_BLOCKED_TOTAL.labels(reason="cooldown").inc()
                summary["processed"].append({"symbol": symbol, "status": "cooldown"})
                continue

            quotes = self._quotes.get_quotes(symbol)
            plan, reason = self._policy.build_plan(exposure, quotes)
            if plan is None:
                _HEDGE_BLOCKED_TOTAL.labels(reason=reason).inc()
                summary["processed"].append(
                    {"symbol": symbol, "status": "blocked", "reason": reason}
                )
                continue

            all_success = True
            leg_results = []
            for leg in plan.legs:
                response = self._router.submit_hedge_leg(leg)
                ok = bool(response.get("ok"))
                reason_value = str(response.get("reason", "ok"))
                result_label = "ok" if ok else "fail"
                _HEDGE_SUBMIT_TOTAL.labels(result=result_label, reason=reason_value).inc()
                leg_results.append({"ok": ok, "reason": reason_value})
                if not ok:
                    all_success = False
            if all_success and plan.legs:
                self._cooldowns[symbol] = now_sec + self._cooldown_sec
                summary["processed"].append(
                    {
                        "symbol": symbol,
                        "status": "submitted",
                        "legs": len(plan.legs),
                        "reason": plan.reason,
                    }
                )
            else:
                summary["processed"].append(
                    {
                        "symbol": symbol,
                        "status": "error",
                        "legs": len(plan.legs),
                        "results": leg_results,
                    }
                )

        _HEDGE_COMPUTE_MS.observe((perf_counter() - started) * 1000.0)
        summary["status"] = "ok"
        return summary
