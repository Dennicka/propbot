from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Dict, List, Tuple

from .. import ledger
from ..services.runtime import get_state
from .pnl import Fill, Position, compute_realized_pnl, compute_unrealized_pnl

LOGGER = logging.getLogger(__name__)


async def snapshot(since: datetime | None = None) -> Tuple[List[Dict[str, object]], Dict[str, float]]:
    state = get_state()
    environment = str(state.control.environment or "paper").lower()
    if environment == "testnet":
        exposures, fills = await _collect_testnet_snapshot(since)
    else:
        exposures = await asyncio.to_thread(_paper_exposures)
        fills = await asyncio.to_thread(ledger.fetch_fills_since, since)
    marks = await _resolve_mark_prices(state, exposures)
    realized = compute_realized_pnl([Fill.from_mapping(row) for row in fills])
    unrealized = compute_unrealized_pnl([Position.from_mapping(row) for row in exposures], marks)
    total = realized + unrealized
    return exposures, {"realized": realized, "unrealized": unrealized, "total": total}


def _paper_exposures() -> List[Dict[str, object]]:
    rows = ledger.fetch_positions()
    exposures: List[Dict[str, object]] = []
    for row in rows:
        qty = float(row.get("base_qty", 0.0))
        if abs(qty) <= 1e-12:
            continue
        avg_price = float(row.get("avg_price", 0.0))
        exposures.append(
            {
                "venue": row.get("venue"),
                "symbol": row.get("symbol"),
                "qty": qty,
                "avg_entry": avg_price,
                "notional": abs(qty) * avg_price,
            }
        )
    exposures.sort(key=lambda entry: (str(entry.get("venue", "")), str(entry.get("symbol", ""))))
    return exposures


async def _collect_testnet_snapshot(since: datetime | None) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
    state = get_state()
    runtime = state.derivatives
    if not runtime or not runtime.venues:
        exposures = await asyncio.to_thread(_paper_exposures)
        fills = await asyncio.to_thread(ledger.fetch_fills_since, since)
        return exposures, fills
    from ..broker.router import ExecutionRouter

    router = ExecutionRouter()
    brokers: List[Tuple[str, object]] = []
    for venue_id in runtime.venues.keys():
        venue = venue_id.replace("_", "-")
        broker = router.broker_for_venue(venue)
        brokers.append((venue, broker))
    position_tasks = [asyncio.create_task(broker.get_positions()) for _, broker in brokers]
    fills_tasks = [asyncio.create_task(broker.get_fills(since=since)) for _, broker in brokers]
    exposures: List[Dict[str, object]] = []
    fills: List[Dict[str, object]] = []
    for venue_info, result in zip(brokers, await asyncio.gather(*position_tasks, return_exceptions=True)):
        venue, _ = venue_info
        if isinstance(result, Exception):  # pragma: no cover - defensive logging
            LOGGER.warning("failed to aggregate positions", extra={"venue": venue, "error": str(result)})
            continue
        exposures.extend(result)
    for venue_info, result in zip(brokers, await asyncio.gather(*fills_tasks, return_exceptions=True)):
        venue, _ = venue_info
        if isinstance(result, Exception):  # pragma: no cover - defensive logging
            LOGGER.warning("failed to aggregate fills", extra={"venue": venue, "error": str(result)})
            continue
        fills.extend(result)
    if not exposures:
        exposures = await asyncio.to_thread(_paper_exposures)
    else:
        exposures.sort(key=lambda entry: (str(entry.get("venue", "")), str(entry.get("symbol", ""))))
    if not fills:
        fills = await asyncio.to_thread(ledger.fetch_fills_since, since)
    return exposures, fills


async def _resolve_mark_prices(state, exposures: List[Dict[str, object]]) -> Dict[str, float]:
    runtime = state.derivatives
    marks: Dict[str, float] = {}
    if not runtime or not runtime.venues:
        for exposure in exposures:
            symbol = str(exposure.get("symbol") or "").upper()
            if not symbol:
                continue
            marks[symbol] = float(exposure.get("avg_entry") or 0.0)
        return marks
    tasks = []
    metadata: List[Tuple[str, float]] = []
    for exposure in exposures:
        symbol = str(exposure.get("symbol") or "").upper()
        venue = str(exposure.get("venue") or "")
        if not symbol or symbol in marks:
            continue
        runtime_id = venue.replace("-", "_")
        venue_runtime = runtime.venues.get(runtime_id)
        if not venue_runtime:
            marks[symbol] = float(exposure.get("avg_entry") or 0.0)
            continue
        fallback = float(exposure.get("avg_entry") or 0.0)
        tasks.append(asyncio.to_thread(_mark_price_for_symbol, venue_runtime.client, symbol))
        metadata.append((symbol, fallback))
    results = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    for (symbol, fallback), result in zip(metadata, results):
        price = fallback
        if not isinstance(result, Exception) and result is not None:
            try:
                price = float(result)
            except (TypeError, ValueError):
                price = fallback
        marks[symbol] = price
    for exposure in exposures:
        symbol = str(exposure.get("symbol") or "").upper()
        if symbol and symbol not in marks:
            marks[symbol] = float(exposure.get("avg_entry") or 0.0)
    return marks


def _mark_price_for_symbol(client, symbol: str) -> float | None:
    for candidate in _symbol_candidates(symbol):
        try:
            data = client.get_mark_price(candidate)
        except Exception:  # pragma: no cover - defensive logging
            continue
        price = None
        if isinstance(data, dict):
            price = data.get("price") or data.get("markPrice") or data.get("last")
        if price is None:
            continue
        try:
            return float(price)
        except (TypeError, ValueError):
            continue
    return None


def _symbol_candidates(symbol: str) -> List[str]:
    base = str(symbol or "").upper()
    candidates = [base]
    if "-" not in base and base.endswith("USDT"):
        prefix = base[:-4]
        candidates.append(f"{prefix}-USDT-SWAP")
    return candidates
