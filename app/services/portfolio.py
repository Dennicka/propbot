from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Tuple

from .. import ledger
from ..metrics import pnl as pnl_metrics
from ..risk.core import FeatureFlags
from ..services.runtime import get_state
from .pnl import (
    Fill,
    Position,
    RealizedPnLBreakdown,
    compute_realized_breakdown,
    compute_realized_breakdown_by_symbol,
    compute_unrealized_pnl,
)

LOGGER = logging.getLogger(__name__)


def _env_flag(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if not value:
        return default
    return value in {"1", "true", "yes", "on"}


def _exclude_simulated_entries() -> bool:
    try:
        return FeatureFlags.exclude_dry_run_from_pnl()
    except Exception as exc:
        LOGGER.debug(
            "failed to resolve feature flag for pnl simulation exclusion",
            extra={"error": str(exc)},
        )
        return _env_flag("EXCLUDE_DRY_RUN_FROM_PNL", True)


def _load_funding_events(limit: int = 200) -> List[dict[str, Any]]:
    try:
        events = ledger.fetch_events(limit=limit, order="desc")
    except Exception as exc:
        LOGGER.debug(
            "failed to fetch funding events", extra={"error": str(exc)}, exc_info=True
        )
        return []
    funding_rows: List[dict[str, Any]] = []
    for event in events:
        code = str(event.get("code") or event.get("type") or "").lower()
        if code not in {"funding", "funding_payment", "funding_settlement"}:
            continue
        payload_raw = event.get("payload")
        if isinstance(payload_raw, Mapping):
            payload = dict(payload_raw)
        elif isinstance(payload_raw, str):
            try:
                payload = json.loads(payload_raw)
            except json.JSONDecodeError:
                payload = {}
        else:
            payload = {}
        amount = float(payload.get("amount") or payload.get("pnl") or event.get("amount") or 0.0)
        if amount == 0.0:
            continue
        strategy = str(payload.get("strategy") or payload.get("strategy_name") or "unknown")
        venue = str(
            payload.get("venue") or payload.get("exchange") or event.get("venue") or "unknown"
        )
        simulated = bool(payload.get("simulated") or payload.get("dry_run"))
        symbol_value = (
            payload.get("symbol")
            or payload.get("pair")
            or payload.get("instrument")
            or payload.get("asset")
            or event.get("symbol")
            or "unknown"
        )
        symbol = str(symbol_value).upper() or "UNKNOWN"
        funding_rows.append(
            {
                "strategy": strategy or "unknown",
                "venue": venue or "unknown",
                "symbol": symbol,
                "amount": amount,
                "ts": event.get("ts"),
                "simulated": simulated,
            }
        )
    return funding_rows


def _funding_breakdown(exclude_simulated: bool) -> tuple[dict[str, float], float]:
    funding_events = _load_funding_events()
    totals: Dict[str, float] = defaultdict(float)
    for entry in funding_events:
        if exclude_simulated and entry.get("simulated"):
            continue
        try:
            amount = float(entry.get("amount") or 0.0)
        except (TypeError, ValueError):
            amount = 0.0
        if amount == 0.0:
            continue
        symbol = str(entry.get("symbol") or "UNKNOWN").upper() or "UNKNOWN"
        totals[symbol] += amount
    return dict(sorted(totals.items())), sum(totals.values())


@dataclass(frozen=True)
class PortfolioPosition:
    venue: str
    venue_type: str
    symbol: str
    qty: float
    notional: float
    entry_px: float
    mark_px: float
    upnl: float
    rpnl: float
    fees_paid: float = 0.0
    funding: float = 0.0

    def as_dict(self) -> Dict[str, object]:
        return {
            "venue": self.venue,
            "venue_type": self.venue_type,
            "symbol": self.symbol,
            "qty": self.qty,
            "notional": self.notional,
            "entry_px": self.entry_px,
            "mark_px": self.mark_px,
            "upnl": self.upnl,
            "rpnl": self.rpnl,
            "fees_paid": self.fees_paid,
            "funding": self.funding,
        }

    def as_legacy_exposure(self) -> Dict[str, object]:
        return {
            "venue": self.venue,
            "venue_type": self.venue_type,
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
    profile: str | None = None
    exclude_simulated: bool = True

    def as_dict(self) -> Dict[str, object]:
        return {
            "positions": [position.as_dict() for position in self.positions],
            "balances": [balance.as_dict() for balance in self.balances],
            "pnl_totals": dict(self.pnl_totals),
            "notional_total": self.notional_total,
            "profile": self.profile,
            "exclude_simulated": self.exclude_simulated,
        }

    def exposures(self) -> List[Dict[str, object]]:
        return [position.as_legacy_exposure() for position in self.positions]


async def snapshot(since: datetime | None = None) -> PortfolioSnapshot:
    state = get_state()
    environment = str(state.control.environment or "paper").lower()
    safe_mode = bool(state.control.safe_mode)
    dry_run = bool(state.control.dry_run)
    use_testnet_brokers = environment in {"testnet", "live"}

    exclude_simulated = _exclude_simulated_entries()

    if use_testnet_brokers:
        exposures, fills = await _collect_testnet_snapshot(state, since)
        balances = await _collect_testnet_balances(state)
    else:
        exposures = await asyncio.to_thread(_paper_exposures)
        fills = await asyncio.to_thread(ledger.fetch_fills_since, since)
        balances = await asyncio.to_thread(_paper_balances)

    fills_payload = [Fill.from_mapping(row) for row in fills]
    marks = await _resolve_mark_prices(state, exposures)
    positions_payload = [Position.from_mapping(row) for row in exposures]
    unrealized_total = compute_unrealized_pnl(positions_payload, marks)

    realized_breakdown = compute_realized_breakdown(fills_payload)
    symbol_breakdowns = compute_realized_breakdown_by_symbol(fills_payload)
    realized_total = realized_breakdown.net
    trading_total = realized_breakdown.trading
    fees_total = realized_breakdown.fees

    funding_by_symbol, funding_total = _funding_breakdown(exclude_simulated)

    positions = _build_positions(exposures, marks, symbol_breakdowns, funding_by_symbol)
    balances_payload = _normalise_balances(balances)
    notional_total = sum(position.notional for position in positions)
    pnl_totals = {
        "realized": realized_total,
        "realized_trading": trading_total,
        "unrealized": unrealized_total,
        "fees": -fees_total,
        "funding": funding_total,
        "total": realized_total + unrealized_total,
        "net": realized_total + unrealized_total + funding_total,
    }

    realized_by_symbol_net: Dict[str, float] = {
        symbol: breakdown.net for symbol, breakdown in symbol_breakdowns.items()
    }
    fees_by_symbol: Dict[str, float] = {
        symbol: breakdown.fees for symbol, breakdown in symbol_breakdowns.items()
    }
    if "UNKNOWN" in symbol_breakdowns and "UNKNOWN" not in realized_by_symbol_net:
        realized_by_symbol_net["UNKNOWN"] = 0.0
    unrealized_by_symbol: Dict[str, float] = defaultdict(float)
    for position in positions:
        unrealized_by_symbol[position.symbol] += position.upnl

    try:
        pnl_metrics.update_pnl_metrics(
            profile=environment,
            realized=realized_by_symbol_net,
            unrealized=unrealized_by_symbol,
            fees=fees_by_symbol,
            funding=funding_by_symbol,
            total_realized=realized_total,
            total_unrealized=unrealized_total,
            total_fees=fees_total,
            total_funding=funding_total,
        )
    except Exception:  # pragma: no cover - metrics must not break snapshot
        LOGGER.exception(
            "failed to update pnl metrics",
            extra={"profile": environment, "exclude_simulated": exclude_simulated},
        )

    LOGGER.debug(
        "portfolio.pnl_snapshot",
        extra={
            "profile": environment,
            "exclude_simulated": exclude_simulated,
            "realized": realized_total,
            "realized_trading": trading_total,
            "unrealized": unrealized_total,
            "fees_paid": fees_total,
            "funding": funding_total,
        },
    )

    return PortfolioSnapshot(
        positions=positions,
        balances=balances_payload,
        pnl_totals=pnl_totals,
        notional_total=notional_total,
        profile=environment,
        exclude_simulated=exclude_simulated,
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
                "venue_type": "paper",
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


async def _collect_testnet_snapshot(
    state, since: datetime | None
) -> Tuple[List[Dict[str, object]], List[Dict[str, object]]]:
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
    for venue_info, result in zip(
        brokers, await asyncio.gather(*position_tasks, return_exceptions=True)
    ):
        venue, _ = venue_info
        if isinstance(result, Exception):  # pragma: no cover - defensive logging
            LOGGER.warning(
                "failed to aggregate positions", extra={"venue": venue, "error": str(result)}
            )
            continue
        exposures.extend(result)
    for venue_info, result in zip(
        brokers, await asyncio.gather(*fills_tasks, return_exceptions=True)
    ):
        venue, _ = venue_info
        if isinstance(result, Exception):  # pragma: no cover - defensive logging
            LOGGER.warning(
                "failed to aggregate fills", extra={"venue": venue, "error": str(result)}
            )
            continue
        fills.extend(result)
    if not exposures:
        exposures = await asyncio.to_thread(_paper_exposures)
    else:
        exposures.sort(
            key=lambda entry: (str(entry.get("venue", "")), str(entry.get("symbol", "")))
        )
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
    for venue_info, result in zip(
        brokers, await asyncio.gather(*balance_tasks, return_exceptions=True)
    ):
        venue, _ = venue_info
        if isinstance(result, Exception):  # pragma: no cover - defensive logging
            LOGGER.warning(
                "failed to aggregate balances", extra={"venue": venue, "error": str(result)}
            )
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
    realized_by_symbol: Mapping[str, RealizedPnLBreakdown],
    funding_by_symbol: Mapping[str, float],
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
        breakdown = realized_by_symbol.get(symbol)
        rpnl = float(breakdown.net) if breakdown else 0.0
        fees_paid = float(breakdown.fees) if breakdown else 0.0
        funding = float(funding_by_symbol.get(symbol, 0.0))
        venue_type = str(row.get("venue_type") or venue or "paper")
        positions.append(
            PortfolioPosition(
                venue=venue,
                venue_type=venue_type,
                symbol=symbol,
                qty=qty,
                notional=notional,
                entry_px=entry,
                mark_px=mark,
                upnl=upnl,
                rpnl=rpnl,
                fees_paid=fees_paid,
                funding=funding,
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
        source_total = (
            total_raw if total_raw is not None else (free_raw if free_raw is not None else 0.0)
        )
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
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(
                "portfolio: failed to fetch mark price",
                extra={"symbol": candidate},
                exc_info=exc,
            )
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
