from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Protocol


class DerivClient(Protocol):
    """Minimal protocol used by the arbitrage engine."""

    def server_time(self) -> float: ...

    def ping(self) -> bool: ...

    def get_filters(self, symbol: str) -> Dict[str, float]: ...

    def get_fees(self, symbol: str) -> Dict[str, float]: ...

    def get_mark_price(self, symbol: str) -> Dict[str, float]: ...

    def get_orderbook_top(self, symbol: str) -> Dict[str, float]: ...

    def get_funding_info(self, symbol: str) -> Dict[str, float]: ...

    def set_position_mode(self, mode: str) -> Dict[str, str]: ...

    def set_margin_type(self, symbol: str, margin_type: str) -> Dict[str, str]: ...

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, str]: ...

    def place_order(self, **kwargs) -> Dict[str, object]: ...

    def cancel_order(self, **kwargs) -> Dict[str, object]: ...

    def open_orders(self, symbol: str | None = None) -> List[Dict[str, object]]: ...

    def positions(self) -> List[Dict[str, object]]: ...


@dataclass
class SymbolState:
    mark_price: float
    bid: float
    ask: float
    lot_size: float
    min_qty: float
    max_qty: float
    min_notional: float
    taker_bps: float
    maker_bps: float
    funding_rate: float = 0.0
    next_funding_ts: float = 0.0


@dataclass
class InMemoryDerivClient:
    """Local deterministic implementation for SAFE_MODE/tests."""

    venue: str
    symbols: Dict[str, SymbolState]
    latency_ms: float = 50.0
    position_mode: str = "hedge"
    margin_type: Dict[str, str] = field(default_factory=dict)
    leverage: Dict[str, int] = field(default_factory=dict)
    positions_data: Dict[str, Dict[str, float]] = field(default_factory=dict)

    def server_time(self) -> float:
        return 0.0

    def ping(self) -> bool:
        return True

    def get_filters(self, symbol: str) -> Dict[str, float]:
        state = self.symbols[symbol]
        return {
            "tick_size": state.lot_size,
            "step_size": state.lot_size,
            "min_qty": state.min_qty,
            "max_qty": state.max_qty,
            "min_notional": state.min_notional,
        }

    def get_fees(self, symbol: str) -> Dict[str, float]:
        s = self.symbols[symbol]
        return {"maker_bps": s.maker_bps, "taker_bps": s.taker_bps}

    def get_mark_price(self, symbol: str) -> Dict[str, float]:
        s = self.symbols[symbol]
        return {"price": s.mark_price, "ts": self.server_time()}

    def get_orderbook_top(self, symbol: str) -> Dict[str, float]:
        s = self.symbols[symbol]
        return {"bid": s.bid, "ask": s.ask, "ts": self.server_time()}

    def get_funding_info(self, symbol: str) -> Dict[str, float]:
        s = self.symbols[symbol]
        return {"rate": s.funding_rate, "next_funding_ts": s.next_funding_ts}

    def set_position_mode(self, mode: str) -> Dict[str, str]:
        self.position_mode = mode
        return {"ok": True, "mode": mode}

    def set_margin_type(self, symbol: str, margin_type: str) -> Dict[str, str]:
        self.margin_type[symbol] = margin_type
        return {"ok": True, "symbol": symbol, "margin_type": margin_type}

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, str]:
        self.leverage[symbol] = leverage
        return {"ok": True, "symbol": symbol, "leverage": leverage}

    def place_order(self, **kwargs) -> Dict[str, object]:
        size = kwargs.get("quantity", 0.0)
        side = kwargs.get("side", "BUY")
        symbol = kwargs.get("symbol")
        if symbol:
            position = self.positions_data.setdefault(symbol, {"long": 0.0, "short": 0.0})
            if side.upper() == "BUY":
                position["long"] += size
            else:
                position["short"] += size
        return {"status": "FILLED", "order_id": f"{self.venue}-{symbol}-dry"}

    def cancel_order(self, **kwargs) -> Dict[str, object]:
        return {"status": "CANCELED", "order_id": kwargs.get("order_id", "n/a")}

    def open_orders(self, symbol: str | None = None) -> List[Dict[str, object]]:
        return []

    def positions(self) -> List[Dict[str, object]]:
        results: List[Dict[str, object]] = []
        for sym, entry in self.positions_data.items():
            results.append({
                "symbol": sym,
                "long": entry.get("long", 0.0),
                "short": entry.get("short", 0.0),
                "margin_type": self.margin_type.get(sym, "isolated"),
                "leverage": self.leverage.get(sym, 1),
            })
        return results


def build_in_memory_client(venue_id: str, symbols: Iterable[str]) -> InMemoryDerivClient:
    states = {
        symbol: SymbolState(
            mark_price=100.0,
            bid=100.4,
            ask=100.1,
            lot_size=0.001,
            min_qty=0.001,
            max_qty=1000.0,
            min_notional=5.0,
            taker_bps=3.0,
            maker_bps=1.5,
        )
        for symbol in symbols
    }
    return InMemoryDerivClient(venue=venue_id, symbols=states)
