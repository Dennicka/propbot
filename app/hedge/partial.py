"""Partial hedge planner for residual delta management."""

from __future__ import annotations

import math
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable, Mapping, MutableMapping

from .. import ledger


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_KNOWN_QUOTES = (
    "USDT",
    "USDC",
    "BUSD",
    "USD",
    "EUR",
    "GBP",
    "BTC",
    "ETH",
    "JPY",
    "AUD",
    "CAD",
    "SGD",
    "TRY",
    "BRL",
)


@dataclass
class _VenueQuote:
    total_notional: float = 0.0
    taker_fee_sum: float = 0.0
    maker_fee_sum: float = 0.0
    funding_apr_sum: float = 0.0
    entries: int = 0

    def add(self, *, notional: float, taker_fee_bps: float, maker_fee_bps: float, funding_apr: float) -> None:
        self.total_notional += max(notional, 0.0)
        self.taker_fee_sum += taker_fee_bps * max(notional, 0.0)
        self.maker_fee_sum += maker_fee_bps * max(notional, 0.0)
        self.funding_apr_sum += funding_apr * max(notional, 0.0)
        self.entries += 1

    def expected_cost_bps(self, *, funding_horizon_hours: float) -> float:
        if self.total_notional <= 0.0:
            return float("inf")
        taker = self.taker_fee_sum / self.total_notional
        funding_apr = self.funding_apr_sum / self.total_notional
        if not math.isfinite(funding_apr):
            funding_apr = 0.0
        funding_component = (funding_apr / 8760.0) * funding_horizon_hours * 10_000.0
        return taker + max(funding_component, 0.0)


class PartialHedgePlanner:
    """Compute hedge orders to offset residual deltas across venues."""

    def __init__(
        self,
        *,
        min_notional_usdt: float | None = None,
        max_notional_usdt_per_order: float | None = None,
        max_orders: int = 3,
        funding_horizon_hours: float = 1.0,
        positions_fetcher: Callable[[], Iterable[Mapping[str, object]]] | None = None,
        balances_fetcher: Callable[[], Iterable[Mapping[str, object]]] | None = None,
    ) -> None:
        self._min_notional = (
            float(min_notional_usdt)
            if min_notional_usdt is not None
            else max(_env_float("HEDGE_MIN_NOTIONAL_USDT", 50.0), 0.0)
        )
        self._max_notional_per_order = (
            float(max_notional_usdt_per_order)
            if max_notional_usdt_per_order is not None
            else max(_env_float("HEDGE_MAX_NOTIONAL_USDT_PER_ORDER", 5_000.0), 1.0)
        )
        self._max_orders = max(1, int(max_orders))
        self._funding_horizon_hours = max(0.0, float(funding_horizon_hours))
        self._positions_fetcher = positions_fetcher or ledger.fetch_positions
        self._balances_fetcher = balances_fetcher or ledger.fetch_balances
        self._override_positions: list[dict[str, object]] | None = None
        self._override_balances: list[dict[str, object]] | None = None
        self._last_plan: dict[str, object] = {}

    @property
    def last_plan_details(self) -> dict[str, object]:
        return dict(self._last_plan)

    def update_market_snapshot(
        self,
        *,
        positions: Iterable[Mapping[str, object]] | None = None,
        balances: Iterable[Mapping[str, object]] | None = None,
    ) -> None:
        if positions is not None:
            self._override_positions = [dict(entry) for entry in positions]
        if balances is not None:
            self._override_balances = [dict(entry) for entry in balances]

    def _load_positions(self) -> list[dict[str, object]]:
        if self._override_positions is not None:
            return list(self._override_positions)
        return [dict(row) for row in self._positions_fetcher()]

    def _load_balances(self) -> list[dict[str, object]]:
        if self._override_balances is not None:
            return list(self._override_balances)
        return [dict(row) for row in self._balances_fetcher()]

    def _symbol_components(self, symbol: str) -> tuple[str, str]:
        symbol_norm = str(symbol or "").upper()
        for quote in _KNOWN_QUOTES:
            if symbol_norm.endswith(quote) and len(symbol_norm) > len(quote):
                return symbol_norm[: -len(quote)], quote
        if len(symbol_norm) > 4:
            return symbol_norm[:-4], symbol_norm[-4:]
        return symbol_norm, "USDT"

    def _build_balance_index(
        self, balances: Iterable[Mapping[str, object]]
    ) -> dict[str, MutableMapping[str, float]]:
        index: dict[str, MutableMapping[str, float]] = defaultdict(lambda: defaultdict(float))
        for row in balances:
            venue = str(row.get("venue") or "").lower()
            asset = str(row.get("asset") or "").upper()
            try:
                qty = float(row.get("qty"))
            except (TypeError, ValueError):
                qty = 0.0
            index[venue][asset] += qty
        return index

    def plan(self, residuals: list[dict[str, object]]) -> list[dict[str, object]]:
        positions = self._load_positions()
        balances = self._load_balances()
        balance_index = self._build_balance_index(balances)

        symbol_info: dict[str, dict[str, object]] = {}
        for row in positions:
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            info = symbol_info.setdefault(
                symbol,
                {
                    "ledger_qty": 0.0,
                    "residual_qty": 0.0,
                    "venues": defaultdict(_VenueQuote),
                    "avg_price_numer": 0.0,
                    "avg_price_denom": 0.0,
                    "price": 0.0,
                    "strategies": set(),
                },
            )
            try:
                qty = float(row.get("base_qty", 0.0))
            except (TypeError, ValueError):
                qty = 0.0
            info["ledger_qty"] = info.get("ledger_qty", 0.0) + qty
            try:
                price = float(row.get("avg_price", 0.0))
            except (TypeError, ValueError):
                price = 0.0
            if price > 0:
                info["price"] = price

        residual_totals: dict[str, float] = defaultdict(float)
        for entry in residuals:
            symbol = str(entry.get("symbol") or "").upper()
            if not symbol:
                continue
            info = symbol_info.setdefault(
                symbol,
                {
                    "ledger_qty": 0.0,
                    "residual_qty": 0.0,
                    "venues": defaultdict(_VenueQuote),
                    "avg_price_numer": 0.0,
                    "avg_price_denom": 0.0,
                    "price": 0.0,
                    "strategies": set(),
                },
            )
            side = str(entry.get("side") or "").upper()
            try:
                qty = float(entry.get("qty", 0.0))
            except (TypeError, ValueError):
                qty = 0.0
            qty = max(qty, 0.0)
            signed = qty if side in {"LONG", "BUY"} else -qty
            info["residual_qty"] = info.get("residual_qty", 0.0) + signed
            residual_totals[symbol] += signed
            try:
                notional = float(entry.get("notional_usdt", 0.0))
            except (TypeError, ValueError):
                notional = 0.0
            if qty > 0 and notional > 0:
                info["avg_price_numer"] = info.get("avg_price_numer", 0.0) + notional
                info["avg_price_denom"] = info.get("avg_price_denom", 0.0) + qty
            venue = str(entry.get("venue") or "").lower()
            taker_fee = float(entry.get("taker_fee_bps", 0.0) or 0.0)
            maker_fee = float(entry.get("maker_fee_bps", 0.0) or 0.0)
            funding_apr_raw = entry.get("funding_apr")
            try:
                funding_apr = float(funding_apr_raw) if funding_apr_raw is not None else 0.0
            except (TypeError, ValueError):
                funding_apr = 0.0
            info["venues"].setdefault(venue, _VenueQuote()).add(
                notional=abs(notional),
                taker_fee_bps=taker_fee,
                maker_fee_bps=maker_fee,
                funding_apr=funding_apr,
            )
            strategy_name = str(entry.get("strategy") or "").strip()
            if strategy_name:
                info["strategies"].add(strategy_name)

        orders: list[dict[str, object]] = []
        plan_summary: dict[str, object] = {
            "generated_ts": _iso_now(),
            "symbols": {},
            "totals": {"orders": 0, "notional_usdt": 0.0},
            "config": {
                "min_notional_usdt": self._min_notional,
                "max_notional_usdt_per_order": self._max_notional_per_order,
                "max_orders": self._max_orders,
                "funding_horizon_hours": self._funding_horizon_hours,
            },
        }

        for symbol, info in symbol_info.items():
            ledger_qty = float(info.get("ledger_qty", 0.0) or 0.0)
            residual_qty = float(info.get("residual_qty", 0.0) or 0.0)
            delta_qty = ledger_qty if abs(ledger_qty) > 1e-9 else residual_qty
            avg_price = float(info.get("price", 0.0) or 0.0)
            avg_price_denom = float(info.get("avg_price_denom", 0.0) or 0.0)
            avg_price_numer = float(info.get("avg_price_numer", 0.0) or 0.0)
            if avg_price <= 0.0 and avg_price_denom > 0.0:
                avg_price = avg_price_numer / avg_price_denom
            if avg_price <= 0.0:
                avg_price = 0.0
            notional_target = abs(delta_qty) * avg_price
            if notional_target <= 0.0:
                continue
            if notional_target < self._min_notional:
                continue
            venues_map: Mapping[str, _VenueQuote] = info.get("venues") or {}
            if not venues_map:
                continue
            best_venue: str | None = None
            best_cost = float("inf")
            for venue, quote in venues_map.items():
                cost = quote.expected_cost_bps(
                    funding_horizon_hours=self._funding_horizon_hours
                )
                if cost < best_cost:
                    best_cost = cost
                    best_venue = venue
            if not best_venue:
                continue
            base_asset, quote_asset = self._symbol_components(symbol)
            venue_balance = balance_index.get(best_venue, {})
            available_quote = float(venue_balance.get(quote_asset.upper(), 0.0) or 0.0)
            allowed_notional = notional_target
            if available_quote > 0.0:
                allowed_notional = min(notional_target, available_quote)
            if allowed_notional < self._min_notional:
                continue
            per_order_cap = min(self._max_notional_per_order, allowed_notional)
            if per_order_cap <= 0.0 or avg_price <= 0.0:
                continue
            remaining_notional = allowed_notional
            remaining_qty = abs(delta_qty)
            order_side = "SELL" if delta_qty > 0 else "BUY"
            orders_needed = max(1, int(math.ceil(remaining_notional / per_order_cap)))
            orders_planned = min(self._max_orders, orders_needed)
            symbol_summary = {
                "delta_qty": delta_qty,
                "avg_price": avg_price,
                "selected_venue": best_venue,
                "expected_cost_bps": best_cost,
                "planned_notional": allowed_notional,
                "orders": [],
                "strategies": sorted(info.get("strategies", set())),
            }
            for index in range(orders_planned):
                current_cap = min(per_order_cap, remaining_notional)
                order_qty = min(remaining_qty, current_cap / avg_price)
                order_notional = order_qty * avg_price
                if order_qty <= 0 or order_notional <= 0:
                    break
                remaining_notional -= order_notional
                remaining_qty -= order_qty
                reason = (
                    f"hedge {symbol} delta={delta_qty:.6f} via {best_venue}"
                    f" cost_bps={best_cost:.3f} step={index + 1}/{orders_planned}"
                )
                order_payload = {
                    "venue": best_venue,
                    "symbol": symbol,
                    "side": order_side,
                    "qty": order_qty,
                    "reason": reason,
                    "notional_usdt": order_notional,
                }
                orders.append(order_payload)
                symbol_summary["orders"].append(order_payload)
                if remaining_notional <= 1e-9 or remaining_qty <= 1e-9:
                    break
            if symbol_summary["orders"]:
                plan_summary["symbols"][symbol] = symbol_summary
                plan_summary["totals"]["orders"] += len(symbol_summary["orders"])
                plan_summary["totals"]["notional_usdt"] += sum(
                    order["notional_usdt"] for order in symbol_summary["orders"]
                )

        plan_summary["orders"] = [dict(order) for order in orders]
        plan_summary["residual_totals"] = {
            symbol: residual_totals.get(symbol, 0.0) for symbol in symbol_info
        }
        self._last_plan = plan_summary
        return [
            {key: value for key, value in order.items() if key != "notional_usdt"}
            for order in orders
        ]


__all__ = ["PartialHedgePlanner"]
