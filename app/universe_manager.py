from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Sequence

from .services.runtime import get_state


class UniverseManager:
    """Aggregate simple metrics for candidate trading pairs across venues.

    The manager does **not** place orders – it only evaluates symbols and
    provides a ranked shortlist for operators to review manually.
    """

    CANDIDATES: tuple[str, ...] = (
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
        "BNBUSDT",
    )

    _MAJOR_SYMBOLS: frozenset[str] = frozenset({"BTCUSDT", "ETHUSDT"})

    _VENUE_ALIASES: Mapping[str, Mapping[str, str]] = {
        "okx_perp": {
            "BTCUSDT": "BTC-USDT-SWAP",
            "ETHUSDT": "ETH-USDT-SWAP",
            "SOLUSDT": "SOL-USDT-SWAP",
            "BNBUSDT": "BNB-USDT-SWAP",
        }
    }

    _VENUE_DISPLAY: Mapping[str, str] = {
        "binance_um": "binance-um",
        "okx_perp": "okx-perp",
    }

    def __init__(self) -> None:
        state = get_state()
        self._derivatives = getattr(state, "derivatives", None)

    # ------------------------------------------------------------------
    # Data collection helpers

    @staticmethod
    def _empty_metrics(symbol: str) -> Dict[str, Any]:
        return {
            "symbol": symbol,
            "best_bid": None,
            "best_ask": None,
            "spread_bps": None,
            "mark_price": None,
            "index_price": None,
            "depth": {"bid_qty": None, "ask_qty": None},  # TODO: integrate depth snapshots
            "filters": {
                "min_qty": None,
                "max_qty": None,
                "min_notional": None,
                "step_size": None,
            },
            "supported": False,
        }

    def _symbol_for_venue(self, venue: str, symbol: str) -> str | None:
        aliases = self._VENUE_ALIASES.get(venue, {})
        return aliases.get(symbol, symbol)

    @staticmethod
    def _normalise_supported(symbols: Iterable[str]) -> MutableMapping[str, str]:
        return {entry.upper(): entry for entry in symbols}

    def _is_symbol_supported(self, venue: str, symbol: str | None) -> bool:
        if not symbol or not self._derivatives:
            return False
        runtime = self._derivatives.venues.get(venue)
        if not runtime:
            return False
        supported = self._normalise_supported(runtime.config.symbols)
        return symbol.upper() in supported

    @staticmethod
    def _iter_config_symbols(symbols: object) -> Iterable[str]:
        if isinstance(symbols, Mapping):
            for value in symbols.values():
                text = str(value or "").strip()
                if text:
                    yield text.upper()
            return
        if isinstance(symbols, Sequence) and not isinstance(symbols, (str, bytes)):
            for entry in symbols:
                text = str(entry or "").strip()
                if text:
                    yield text.upper()
            return
        if symbols:
            text = str(symbols).strip()
            if text:
                yield text.upper()

    def allowed_pairs(self) -> set[str]:
        """Return the set of currently tradeable pairs across venues."""

        allowed: set[str] = set()
        if not self._derivatives:
            return allowed
        venues = getattr(self._derivatives, "venues", {})
        if not isinstance(venues, Mapping):
            return allowed
        for venue_id, runtime in venues.items():
            config = getattr(runtime, "config", None)
            symbols = getattr(config, "symbols", []) if config else []
            for symbol in self._iter_config_symbols(symbols):
                venue_symbol = self._symbol_for_venue(venue_id, symbol)
                if self._is_symbol_supported(venue_id, venue_symbol):
                    allowed.add(symbol.upper())
        return allowed

    def collect_metrics(self, venue: str) -> Dict[str, Dict[str, Any]]:
        """Collect lightweight metrics for each candidate symbol at a venue."""

        results: Dict[str, Dict[str, Any]] = {}
        client = None
        runtime = None
        if self._derivatives:
            runtime = self._derivatives.venues.get(venue)
            if runtime:
                client = runtime.client
        for symbol in self.CANDIDATES:
            entry = self._empty_metrics(symbol)
            venue_symbol = self._symbol_for_venue(venue, symbol)
            if not client or not runtime or not self._is_symbol_supported(venue, venue_symbol):
                results[symbol] = entry
                continue
            entry["supported"] = True
            try:
                book = client.get_orderbook_top(venue_symbol)
                bid = float(book.get("bid")) if book.get("bid") is not None else None
                ask = float(book.get("ask")) if book.get("ask") is not None else None
                entry["best_bid"] = bid
                entry["best_ask"] = ask
                if bid is not None and ask is not None and bid > 0 and ask > 0:
                    mid = (bid + ask) / 2.0
                    if mid > 0:
                        entry["spread_bps"] = abs(ask - bid) / mid * 10_000
            except Exception:
                # leave placeholders as None if the venue cannot provide book data
                pass
            try:
                mark = client.get_mark_price(venue_symbol)
                if isinstance(mark, Mapping):
                    price = mark.get("price")
                    entry["mark_price"] = float(price) if price is not None else None
                    # TODO: expose explicit index price once client surfaces it
                    entry["index_price"] = mark.get("index_price")
            except Exception:
                pass
            try:
                filters = client.get_filters(venue_symbol)
                entry["filters"] = {
                    "min_qty": filters.get("min_qty"),
                    "max_qty": filters.get("max_qty"),
                    "min_notional": filters.get("min_notional"),
                    "step_size": filters.get("step_size"),
                }
            except Exception:
                # keep filters as placeholder if unavailable
                pass
            results[symbol] = entry
        return results

    # ------------------------------------------------------------------
    # Scoring + ranking

    def score_pair(self, data_for_symbol: Mapping[str, Mapping[str, Any]]) -> float:
        """Return a coarse score for a symbol using venue metrics.

        TODO: Replace the heuristic with a data-driven score once depth &
        volatility metrics are available.
        """

        symbol = next(
            (
                value.get("symbol")
                for value in data_for_symbol.values()
                if isinstance(value, Mapping) and value.get("symbol")
            ),
            None,
        )
        score = 0.0
        has_spread = False
        for venue_data in data_for_symbol.values():
            if not isinstance(venue_data, Mapping):
                continue
            spread = venue_data.get("spread_bps")
            if spread is not None:
                has_spread = True
                # narrow spreads receive higher marks (up to +5 per venue)
                score += max(0.0, 5.0 - float(spread))
            if venue_data.get("mark_price") is not None:
                score += 0.5
            if venue_data.get("supported"):
                score += 0.5
            else:
                score -= 1.0
        if not has_spread:
            score *= 0.5
        if symbol and symbol.upper() in self._MAJOR_SYMBOLS:
            score += 2.5
        return max(score, 0.0)

    def _display_name(self, venue: str) -> str:
        return self._VENUE_DISPLAY.get(venue, venue.replace("_", "-"))

    def top_pairs(self, n: int = 3) -> list[dict[str, Any]]:
        venues = []
        if self._derivatives:
            venues = list(self._derivatives.venues.keys())
        per_venue: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for venue in venues:
            per_venue[venue] = self.collect_metrics(venue)
        aggregated: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for symbol in self.CANDIDATES:
            aggregated[symbol] = {}
            for venue, metrics in per_venue.items():
                aggregated[symbol][venue] = metrics.get(symbol, self._empty_metrics(symbol))
        ranked = []
        for symbol, data in aggregated.items():
            score = self.score_pair(data)
            venues_payload = {
                self._display_name(venue): metrics
                for venue, metrics in data.items()
            }
            ranked.append({
                "symbol": symbol,
                "score": round(float(score), 6),
                "venues": venues_payload,
            })
        ranked.sort(key=lambda item: item["score"], reverse=True)
        return ranked[: max(0, int(n))]

    @staticmethod
    def current_timestamp() -> str:
        return datetime.now(timezone.utc).isoformat()


__all__ = ["UniverseManager"]
