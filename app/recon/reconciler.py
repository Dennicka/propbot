from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Mapping, MutableMapping, Tuple

from .. import ledger
from ..services import runtime

LOGGER = logging.getLogger(__name__)

RECON_QTY_TOL = 1e-6
RECON_NOTIONAL_TOL_USDT = 25.0

_PositionKey = Tuple[str, str]
_LedgerEntry = Dict[str, float]


def _normalise_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text.endswith("-SWAP"):
        text = text[:-5]
    text = text.replace("-", "").replace("_", "")
    return text


def _normalise_venue(value: object) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text.replace("_", "-")


def _coerce_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _extract_price(payload: Mapping[str, object] | None) -> float | None:
    if not isinstance(payload, Mapping):
        return None
    for key in ("price", "markPrice", "mark_price", "last", "px", "avgPrice"):
        if key in payload:
            value = payload[key]
            try:
                price = float(value)
            except (TypeError, ValueError):
                continue
            if price > 0:
                return price
    return None


def _symbol_candidates(symbol: str) -> list[str]:
    base = (symbol or "").upper()
    candidates = [base]
    if "-" not in base and base.endswith("USDT"):
        prefix = base[:-4]
        candidates.append(f"{prefix}-USDT-SWAP")
    return candidates


def _extract_quantity(entry: Mapping[str, object]) -> float | None:
    for key in ("position_amt", "positionAmt", "pos", "qty", "base_qty", "size"):
        if key in entry:
            qty = _coerce_float(entry.get(key))
            return qty
    long_qty = _coerce_float(entry.get("long")) + _coerce_float(entry.get("long_qty"))
    short_qty = _coerce_float(entry.get("short")) + _coerce_float(entry.get("short_qty"))
    if long_qty or short_qty:
        return long_qty - short_qty
    return None


@dataclass(slots=True)
class _RuntimeAdapters:
    get_state: Callable[[], object]
    fetch_ledger_positions: Callable[[], Iterable[Mapping[str, object]]]


class Reconciler:
    """Compare exchange positions against the internal ledger and risk snapshot."""

    qty_tolerance: float = RECON_QTY_TOL

    def __init__(
        self,
        *,
        adapters: _RuntimeAdapters | None = None,
    ) -> None:
        if adapters is None:
            adapters = _RuntimeAdapters(
                get_state=runtime.get_state,
                fetch_ledger_positions=ledger.fetch_positions,
            )
        self._adapters = adapters

    def fetch_exchange_positions(self) -> Dict[_PositionKey, float]:
        state = self._adapters.get_state()
        runtime_state = getattr(state, "derivatives", None)
        venues = getattr(runtime_state, "venues", None)
        if not venues:
            return {}
        positions: Dict[_PositionKey, float] = {}
        for venue_id, venue_runtime in venues.items():
            venue_name = _normalise_venue(venue_id)
            client = getattr(venue_runtime, "client", None)
            if client is None:
                continue
            try:
                payload = client.positions()
            except Exception:  # pragma: no cover - defensive
                LOGGER.exception("failed to fetch positions", extra={"venue": venue_name})
                continue
            if not isinstance(payload, Iterable):
                continue
            for entry in payload:
                if not isinstance(entry, Mapping):
                    continue
                symbol_raw = entry.get("symbol") or entry.get("instId") or entry.get("instrument")
                symbol = _normalise_symbol(symbol_raw)
                if not symbol:
                    continue
                qty_value = _extract_quantity(entry)
                if qty_value is None:
                    continue
                qty = float(qty_value)
                if abs(qty) <= self.qty_tolerance:
                    continue
                key = (venue_name, symbol)
                positions[key] = positions.get(key, 0.0) + qty
        return positions

    def fetch_ledger_positions(self) -> Dict[_PositionKey, _LedgerEntry]:
        rows = self._adapters.fetch_ledger_positions()
        entries: Dict[_PositionKey, _LedgerEntry] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            venue = _normalise_venue(row.get("venue"))
            symbol = _normalise_symbol(row.get("symbol"))
            if not venue or not symbol:
                continue
            qty = _coerce_float(row.get("base_qty"))
            if abs(qty) <= self.qty_tolerance:
                continue
            avg_price = _coerce_float(row.get("avg_price"))
            key = (venue, symbol)
            entries[key] = {"qty": qty, "avg_price": avg_price}
        return entries

    def diff(self, tol_qty: float = RECON_QTY_TOL) -> list[dict[str, object]]:
        exchange_positions = self.fetch_exchange_positions()
        ledger_positions = self.fetch_ledger_positions()
        state = self._adapters.get_state()
        safety = getattr(state, "safety", None)
        risk_snapshot = getattr(safety, "risk_snapshot", {}) if safety else {}
        if not isinstance(risk_snapshot, Mapping):
            risk_snapshot = {}
        risk_notional = {}
        exposure_payload = risk_snapshot.get("exposure_by_symbol")
        if isinstance(exposure_payload, Mapping):
            for symbol, value in exposure_payload.items():
                norm_symbol = _normalise_symbol(symbol)
                try:
                    risk_notional[norm_symbol] = float(value)
                except (TypeError, ValueError):
                    continue

        symbol_totals: Dict[str, float] = defaultdict(float)
        symbol_prices: Dict[str, float] = defaultdict(float)
        for (venue, symbol), entry in ledger_positions.items():
            qty = abs(float(entry.get("qty", 0.0)))
            if qty > tol_qty:
                symbol_totals[symbol] += qty
                symbol_prices[symbol] += qty * float(entry.get("avg_price", 0.0))
        exchange_totals: Dict[str, float] = defaultdict(float)
        for (_, symbol), qty in exchange_positions.items():
            exchange_totals[symbol] += abs(qty)

        weighted_ledger_price: Dict[str, float] = {}
        for symbol, total_qty in symbol_totals.items():
            if total_qty > tol_qty:
                weighted_ledger_price[symbol] = symbol_prices[symbol] / total_qty

        keys = set(exchange_positions) | set(ledger_positions)
        symbol_candidates: Dict[str, set[str]] = defaultdict(set)
        for venue, symbol in keys:
            symbol_candidates[symbol].add(venue)
        mark_prices = self._fetch_mark_prices(symbol_candidates)

        results: list[dict[str, object]] = []
        for venue, symbol in sorted(keys):
            exch_qty = exchange_positions.get((venue, symbol), 0.0)
            ledger_entry = ledger_positions.get((venue, symbol))
            ledger_qty = ledger_entry["qty"] if ledger_entry else 0.0
            delta = exch_qty - ledger_qty
            if abs(delta) <= tol_qty:
                continue
            price = self._estimate_price(
                symbol,
                risk_notional,
                weighted_ledger_price,
                symbol_totals,
                exchange_totals,
                mark_prices,
            )
            notional = abs(delta) * price if price > 0 else 0.0
            record: dict[str, object] = {
                "venue": venue,
                "symbol": symbol,
                "exch_qty": exch_qty,
                "ledger_qty": ledger_qty,
                "delta": delta,
            }
            if price > 0:
                record["mark_price"] = price
                record["notional_usd"] = notional
            risk_value = risk_notional.get(symbol)
            if risk_value is not None:
                record["risk_notional_usd"] = risk_value
            results.append(record)
        return results

    def _estimate_price(
        self,
        symbol: str,
        risk_notional: Mapping[str, float],
        weighted_prices: Mapping[str, float],
        ledger_totals: Mapping[str, float],
        exchange_totals: Mapping[str, float],
        mark_prices: Mapping[str, float],
    ) -> float:
        risk_value = float(risk_notional.get(symbol, 0.0))
        ledger_price = float(weighted_prices.get(symbol, 0.0))
        ledger_total = float(ledger_totals.get(symbol, 0.0))
        exchange_total = float(exchange_totals.get(symbol, 0.0))
        if risk_value > 0 and ledger_total > self.qty_tolerance:
            return risk_value / ledger_total
        if risk_value > 0 and exchange_total > self.qty_tolerance:
            return risk_value / exchange_total
        if ledger_price > 0:
            return ledger_price
        mark_price = float(mark_prices.get(symbol, 0.0))
        if mark_price > 0:
            return mark_price
        if risk_value > 0:
            return risk_value
        return 0.0

    def _fetch_mark_prices(
        self,
        symbol_candidates: Mapping[str, Iterable[str]],
    ) -> Dict[str, float]:
        state = self._adapters.get_state()
        runtime_state = getattr(state, "derivatives", None)
        venues: MutableMapping[str, object] | None = getattr(runtime_state, "venues", None)
        if not venues:
            return {}
        resolved: Dict[str, float] = {}
        for symbol, venue_names in symbol_candidates.items():
            candidates = _symbol_candidates(symbol)
            for venue_name in venue_names:
                runtime_key = venue_name.replace("-", "_")
                venue_runtime = venues.get(runtime_key)
                if venue_runtime is None:
                    continue
                client = getattr(venue_runtime, "client", None)
                if client is None:
                    continue
                price = self._mark_price_from_client(client, candidates)
                if price is not None:
                    resolved[symbol] = price
                    break
            if symbol in resolved:
                continue
            for venue_runtime in venues.values():
                client = getattr(venue_runtime, "client", None)
                if client is None:
                    continue
                price = self._mark_price_from_client(client, candidates)
                if price is not None:
                    resolved[symbol] = price
                    break
        return resolved

    @staticmethod
    def _mark_price_from_client(client, candidates: Iterable[str]) -> float | None:
        for candidate in candidates:
            try:
                payload = client.get_mark_price(candidate)
            except Exception:  # pragma: no cover - defensive
                continue
            price = _extract_price(payload)
            if price:
                return price
        return None


__all__ = ["Reconciler", "RECON_QTY_TOL", "RECON_NOTIONAL_TOL_USDT"]
