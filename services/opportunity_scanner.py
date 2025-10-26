"""Background scanner that records the best hedge opportunity."""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict

from fastapi import FastAPI

from app.services.runtime import get_state, set_last_opportunity_state
from services.cross_exchange_arb import check_spread
from services.risk_manager import can_open_new_position

logger = logging.getLogger(__name__)


def _env_interval() -> float:
    raw = os.getenv("SCAN_INTERVAL_SEC")
    if raw is None:
        return 5.0
    try:
        return max(float(raw), 1.0)
    except ValueError:
        return 5.0


def _env_leverage() -> float:
    raw = os.getenv("SCAN_LEVERAGE_SUGGESTION")
    if raw is None:
        return 1.0
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return 1.0


class OpportunityScanner:
    def __init__(self, interval: float | None = None) -> None:
        self.interval = interval or _env_interval()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if not self._task:
            return
        self._stop.set()
        try:
            await self._task
        finally:
            self._task = None

    async def _run(self) -> None:  # pragma: no cover - loop logic exercised via scan_once tests
        while not self._stop.is_set():
            try:
                await self.scan_once()
            except Exception as exc:  # pragma: no cover - defensive logging
                logger.exception("opportunity scan failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                continue

    async def scan_once(self) -> Dict[str, Any]:
        state = get_state()
        control = state.control
        loop_pair = getattr(control, "loop_pair", None) or getattr(state.loop_config, "pair", None)
        symbol = (loop_pair or "BTCUSDT").upper()
        spread_info = await asyncio.to_thread(check_spread, symbol)
        cheap_exchange = str(spread_info.get("cheap"))
        expensive_exchange = str(spread_info.get("expensive"))
        spread_value = float(spread_info.get("spread", 0.0))
        spread_bps = float(spread_info.get("spread_bps", 0.0))
        candidate: Dict[str, Any] = {
            "id": uuid.uuid4().hex,
            "ts": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "long_venue": cheap_exchange,
            "short_venue": expensive_exchange,
            "spread": spread_value,
            "spread_bps": spread_bps,
            "notional_suggestion": float(control.order_notional_usdt),
            "leverage_suggestion": _env_leverage(),
            "min_spread": spread_value,
        }
        status = "allowed"
        allowed, reason = can_open_new_position(
            float(candidate["notional_suggestion"]),
            float(candidate["leverage_suggestion"]),
        )
        if not allowed:
            candidate["blocked_reason"] = reason
            status = "blocked_by_risk"
        if spread_value <= 0:
            status = "blocked_by_risk"
        set_last_opportunity_state(candidate if spread_value > 0 else None, status)
        return {"candidate": candidate, "status": status}


_scanner = OpportunityScanner()


def get_scanner() -> OpportunityScanner:
    """Return the process-wide opportunity scanner instance."""

    return _scanner


def setup_scanner(app: FastAPI) -> None:
    app.state.opportunity_scanner = _scanner

    @app.on_event("startup")
    async def _start_scanner() -> None:  # pragma: no cover - integration hook
        await _scanner.start()

    @app.on_event("shutdown")
    async def _stop_scanner() -> None:  # pragma: no cover - integration hook
        await _scanner.stop()


__all__ = ["OpportunityScanner", "setup_scanner", "get_scanner"]
