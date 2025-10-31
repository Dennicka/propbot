from __future__ import annotations

import asyncio
import hashlib
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping, Sequence, Tuple

from .. import ledger
from ..broker.router import ExecutionRouter
from .runtime import get_state

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeInstruction:
    """Normalized representation of an exposure that should be closed."""

    venue: str
    symbol: str
    close_side: str
    qty: float
    notional: float | None = None

    def fingerprint(self) -> Tuple[str, str, str, float]:
        return (
            self.venue.lower(),
            self.symbol.upper(),
            self.close_side,
            round(self.qty, 12),
        )

    def idempotency_key(self) -> str:
        base = f"{self.venue}:{self.symbol}:{self.close_side}:{self.qty:.12f}"
        digest = hashlib.sha256(base.encode("utf-8")).hexdigest()
        return f"close_all:{digest[:24]}"

    def as_dict(self) -> Dict[str, object]:
        payload: Dict[str, object] = {
            "venue": self.venue,
            "symbol": self.symbol,
            "side": self.close_side,
            "qty": self.qty,
        }
        if self.notional is not None:
            payload["notional_usdt"] = self.notional
        return payload


_CLOSE_ALL_LOCK = asyncio.Lock()


async def _fetch_ledger_positions() -> Sequence[Mapping[str, Any]]:
    return await asyncio.to_thread(ledger.fetch_positions)


def _normalise_trade(row: Mapping[str, Any]) -> TradeInstruction | None:
    venue = str(row.get("venue") or "").strip()
    symbol = str(row.get("symbol") or "").strip()
    if not venue or not symbol:
        return None
    try:
        base_qty = float(row.get("base_qty") or 0.0)
    except (TypeError, ValueError):
        return None
    if abs(base_qty) <= 1e-12:
        return None
    close_side = "sell" if base_qty > 0 else "buy"
    qty = abs(base_qty)
    try:
        avg_price = float(row.get("avg_price") or 0.0)
    except (TypeError, ValueError):
        avg_price = 0.0
    notional = qty * avg_price if avg_price else None
    return TradeInstruction(
        venue=venue,
        symbol=symbol.upper(),
        close_side=close_side,
        qty=qty,
        notional=notional,
    )


async def _list_open_trades() -> List[TradeInstruction]:
    rows = await _fetch_ledger_positions()
    trades: List[TradeInstruction] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        trade = _normalise_trade(row)
        if trade is None:
            continue
        trades.append(trade)
    trades.sort(key=lambda item: item.fingerprint())
    return trades


def _fingerprint(trades: Iterable[TradeInstruction]) -> Tuple[Tuple[str, str, str, float], ...]:
    return tuple(trade.fingerprint() for trade in trades)


def _simulate_close(trade: TradeInstruction) -> Dict[str, object]:
    payload = trade.as_dict()
    payload["status"] = "simulated"
    return payload


async def _perform_close(trade: TradeInstruction) -> Dict[str, object]:
    router = ExecutionRouter()
    broker = router.broker_for_venue(trade.venue)
    order = await broker.create_order(
        venue=trade.venue,
        symbol=trade.symbol,
        side=trade.close_side,
        qty=trade.qty,
        price=None,
        type="MARKET",
        post_only=False,
        reduce_only=True,
        fee=0.0,
        idemp_key=trade.idempotency_key(),
    )
    payload = trade.as_dict()
    payload["status"] = "submitted"
    payload["order"] = dict(order) if isinstance(order, Mapping) else order
    return payload


async def close_all_trades(*, dry_run: bool) -> Dict[str, List[Dict[str, object]]]:
    async with _CLOSE_ALL_LOCK:
        trades = await _list_open_trades()
        state = get_state()
        tracker: MutableMapping[str, object] | None = getattr(state, "_close_all_tracker", None)
        fingerprint = _fingerprint(trades)
        if tracker and tracker.get("fingerprint") == fingerprint and fingerprint:
            LOGGER.info("close-all trades request ignored: already processed", extra={"count": len(trades)})
            return {"closed": [], "positions": []}
        if not trades:
            setattr(state, "_close_all_tracker", {"fingerprint": fingerprint})
            return {"closed": [], "positions": []}
        if dry_run:
            closed: List[Dict[str, object]] = [_simulate_close(trade) for trade in trades]
            return {"closed": closed, "positions": []}
        closed = []
        for trade in trades:
            try:
                result = await _perform_close(trade)
            except Exception:
                LOGGER.exception("failed to close trade", extra=trade.as_dict())
                raise
            closed.append(result)
        setattr(state, "_close_all_tracker", {"fingerprint": fingerprint})
        return {"closed": closed, "positions": []}


__all__ = ["TradeInstruction", "close_all_trades"]
