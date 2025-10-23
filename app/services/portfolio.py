from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, Iterable, List, Mapping, Tuple

from .. import ledger
from ..services.runtime import get_state
from .pnl import Fill, Position, compute_realized_pnl, compute_unrealized_pnl

LOGGER = logging.getLogger(__name__)


def _env_flag(name: str) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return False
    return str(raw).lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class PortfolioPosition:
    venue: str
    symbol: str
    qty: float
    notional: float
    entry_px: float
    mark_px: float
    upnl: float
    rpnl: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "qty": self.qty,
            "notional": self.notional,
            "entry_px": self.entry_px,
            "mark_px": self.mark_px,
            "upnl": self.upnl,
            "rpnl": self.rpnl,
        }

    def as_legacy_exposure(self) -> Dict[str, object]:
        return {
            "venue": self.venue,
            "symbol": self.symbol,
            "qty": self.qty,
            "avg_entry": self.entry_px,
            "avg_price": self.entry_px,
            "notional": self.notional,
        }


@dataclass(frozen=True)
class PortfolioBalance:
    venue: str
    asset: str
    free: float
    total: float

    def as_dict(self) -> Dict[str, object]:
        return {
            "venue": self.venue,
            "asset": self.asset,
            "free": self.free,
            "total": self.total,
        }


@dataclass(frozen=True)
class PortfolioSnapshot:
    positions: List[PortfolioPosition] = field(default_factory=list)
    balances: List[PortfolioBalance] = field(default_factory=list)
    pnl_totals: Dict[str, float] = field(default_factory=dict)
    notional_total: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "positions": [position.as_dict() for position in self.positions],
            "balances": [balance.as_dict() for balance in self.balances],
            "pnl_totals": dict(self.pnl_totals),
            "notional_total": self.notional_total,
        }

    def exposures(self) -> List[Dict[str, object]]:
        return [position.as_legacy_exposure() for position in self.positions]


async def snapshot(since: datetime | None = None) -> PortfolioSnapshot:
    state = get_state()
    environment = str(state.control.environment or "paper").lower()
    safe_mode = bool(state.control.safe_mode)
    dry_run = bool(state.control.dry_run)
    enable_testnet_orders = _env_flag("ENABLE_PLACE_TEST_ORDERS")

    use_testnet_brokers = (
        environment == "testnet" and enable_testnet_orders and not safe_mode and not dry_run
    )

    if use_testnet_brokers:
        exposures, fills = await _collect_testnet_snapshot(state, since)
        balances = await _collect_testnet_balances(state)
    else:
        exposures = await asyncio.to_thread(_paper_exposures)
        fills = await asyncio.to_thread(ledger.fetch_fills_since, since)
        balances = await asyncio.to_thread(_paper_balances)

    fills_payload = [Fill.from_mapping(row) for row in fills]
    marks = await _resolve_mark_prices(state, exposures)
    unrealized_total = compute_unrealized_pnl(
        [Position.from_mapping(row) for row in exposures], marks
    )
    realized_total = compute_realized_pnl(fills_payload)

    realized_by_symbol: Dict[str, float] = {}
    fills_by_symbol: Dict[str, List[Fill]] = defaultdict(list)
    for fill in fills_payload:
        if fill.symbol:
            fills_by_symbol[fill.symbol].append(fill)
    for symbol, symbol_fills in fills_by_symbol.items():
        realized_by_symbol[symbol] = compute_realized_pnl(symbol_fills)

    positions = _build_positions(exposures, marks, realized_by_symbol)
    balances_payload = _normalise_balances(balances)
    notional_total = sum(position.notional for position in positions)
    pnl_totals = {
        "realized": realized_total,
        "unrealized": unrealized_total,
        "total": realized_total + unrealized_total,
    }

    return PortfolioSnapshot(
        positions=positions,
        balances=balances_payload,
        pnl_totals=pnl_totals,
        notional_total=notional_total,
    )


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
                "avg_price": avg_price,
                "notional": abs(qty) * avg_price,
            }
        )
    exposures.sort(key=lambda entry: (str(entry.get("venue", "")), str(entry.get("symbol", ""))))
    return exposures


def _paper_balances() -> List[Dict[str, object]]:
    return ledger.fetch_balances()


async def _collect_testnet_snapshot(state, since: datetime | None) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
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


async def _collect_testnet_balances(state) -> List[Dict[str, object]]:
    runtime = state.derivatives
    if not runtime or not runtime.venues:
        return await asyncio.to_thread(_paper_balances)
    from ..broker.router import ExecutionRouter

    router = ExecutionRouter()
    brokers: List[Tuple[str, object]] = []
    for venue_id in runtime.venues.keys():
        venue = venue_id.replace("_", "-")
        broker = router.broker_for_venue(venue)
        brokers.append((venue, broker))
    balance_tasks = [asyncio.create_task(broker.balances(venue=venue)) for venue, broker in brokers]
    balances: List[Dict[str, object]] = []
    for venue_info, result in zip(brokers, await asyncio.gather(*balance_tasks, return_exceptions=True)):
        venue, _ = venue_info
        if isinstance(result, Exception):  # pragma: no cover - defensive logging
            LOGGER.warning("failed to aggregate balances", extra={"venue": venue, "error": str(result)})
            continue
        payload = result.get("balances") if isinstance(result, Mapping) else None
        if isinstance(payload, list):
            balances.extend(payload)
    if not balances:
        return await asyncio.to_thread(_paper_balances)
    return balances


def _build_positions(
    exposures: Iterable[Mapping[str, object]],
    marks: Mapping[str, float],
    realized_by_symbol: Mapping[str, float],
) -> List[PortfolioPosition]:
    positions: List[PortfolioPosition] = []
    for row in exposures:
        symbol = str(row.get("symbol") or "").upper()
        venue = str(row.get("venue") or "")
        if not symbol:
            continue
        qty = float(row.get("qty") or row.get("base_qty") or 0.0)
        if abs(qty) <= 1e-12:
            continue
        entry = float(row.get("avg_entry") or row.get("avg_price") or 0.0)
        mark_value = marks.get(symbol)
        if mark_value is None:
            mark_value = entry
        try:
            mark = float(mark_value)
        except (TypeError, ValueError):
            mark = entry
        notional = abs(qty) * mark
        upnl = (mark - entry) * qty
        rpnl = float(realized_by_symbol.get(symbol, 0.0))
        positions.append(
            PortfolioPosition(
                venue=venue,
                symbol=symbol,
                qty=qty,
                notional=notional,
                entry_px=entry,
                mark_px=mark,
                upnl=upnl,
                rpnl=rpnl,
            )
        )
    positions.sort(key=lambda item: (item.venue, item.symbol))
    return positions


def _normalise_balances(rows: Iterable[Mapping[str, object]]) -> List[PortfolioBalance]:
    balances: List[PortfolioBalance] = []
    for row in rows:
        venue = str(row.get("venue") or "")
        asset_raw = row.get("asset") or row.get("currency") or row.get("symbol") or ""
        asset = str(asset_raw).upper()
        if not venue or not asset:
            continue
        free_raw = row.get("free")
        if free_raw is None:
            free_raw = row.get("available")
        if free_raw is None:
            free_raw = row.get("qty")
        if free_raw is None:
            free_raw = row.get("balance")
        total_raw = row.get("total")
        if total_raw is None:
            total_raw = row.get("equity")
        if total_raw is None:
            total_raw = row.get("qty")
        if total_raw is None:
            total_raw = row.get("balance")
        try:
            free = float(free_raw if free_raw is not None else 0.0)
        except (TypeError, ValueError):
            free = 0.0
        source_total = total_raw if total_raw is not None else (free_raw if free_raw is not None else 0.0)
        try:
            total = float(source_total)
        except (TypeError, ValueError):
            total = free
        if abs(free) <= 1e-12 and abs(total) <= 1e-12:
            continue
        balances.append(PortfolioBalance(venue=venue, asset=asset, free=free, total=total))
    balances.sort(key=lambda item: (item.venue, item.asset))
    return balances


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
