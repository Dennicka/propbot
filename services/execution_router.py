"""Best-venue execution router for hedge legs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Mapping

from app.services.runtime import get_state, is_dry_run_mode
from exchanges import BinanceFuturesClient, FuturesExchangeClient, OKXFuturesClient


@dataclass
class _VenueAdapter:
    name: str
    client: FuturesExchangeClient


def _build_clients() -> Dict[str, _VenueAdapter]:
    return {
        "binance": _VenueAdapter("binance", BinanceFuturesClient()),
        "okx": _VenueAdapter("okx", OKXFuturesClient()),
    }


_CLIENTS: Dict[str, _VenueAdapter] = _build_clients()


def _fee_bps_for(venue: str, control: object) -> int:
    venue_lower = venue.lower()
    if venue_lower == "binance":
        return int(getattr(control, "taker_fee_bps_binance", 0))
    if venue_lower == "okx":
        return int(getattr(control, "taker_fee_bps_okx", 0))
    return 0


def _coerce_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _available_balance(entry: Mapping[str, object] | None) -> float | None:
    if not isinstance(entry, Mapping):
        return None
    for key in ("available_balance", "available", "cash_available"):
        if key in entry:
            return _coerce_float(entry.get(key))
    return None


def _fetch_quote(adapter: _VenueAdapter, symbol: str) -> float:
    payload = adapter.client.get_mark_price(symbol)
    if isinstance(payload, Mapping):
        return _coerce_float(payload.get("mark_price"))
    return 0.0


def _check_liquidity(adapter: _VenueAdapter, symbol: str, notional_usdt: float) -> bool:
    if notional_usdt <= 0:
        return False
    if is_dry_run_mode():
        return True
    try:
        limits = adapter.client.get_account_limits()
    except Exception:  # pragma: no cover - network/credential failures fallback
        return True
    available = _available_balance(limits)
    if available is None:
        return True
    try:
        return float(available) >= float(notional_usdt)
    except (TypeError, ValueError):
        return True


def choose_venue(side: str, symbol: str, size: float) -> Dict[str, object]:
    """Return the venue offering the best effective price for the desired leg."""

    side_lower = str(side or "").lower()
    if side_lower not in {"buy", "sell", "long", "short"}:
        raise ValueError("side must be buy/sell or long/short")

    symbol_normalised = str(symbol or "").upper()
    base_size = max(float(size), 0.0)

    state = get_state()
    control = getattr(state, "control", state)

    candidates: Iterable[_VenueAdapter] = _CLIENTS.values()

    best: Dict[str, object] | None = None
    for adapter in candidates:
        mark_price = _fetch_quote(adapter, symbol_normalised)
        fee_bps = _fee_bps_for(adapter.name, control)
        notional_usdt = base_size * mark_price if mark_price > 0 else 0.0
        fee_multiplier = fee_bps / 10_000.0
        if side_lower in {"buy", "long"}:
            effective_price = mark_price * (1.0 + fee_multiplier)
        else:
            effective_price = mark_price * (1.0 - fee_multiplier)
        liquidity_ok = _check_liquidity(adapter, symbol_normalised, notional_usdt)
        candidate = {
            "venue": adapter.name,
            "expected_fill_px": mark_price,
            "fee_bps": fee_bps,
            "effective_price": effective_price,
            "liquidity_ok": liquidity_ok,
            "size": base_size,
            "expected_notional": notional_usdt,
        }
        if best is None:
            best = candidate
            continue
        best_eff = _coerce_float(best.get("effective_price"))
        cand_eff = _coerce_float(candidate.get("effective_price"))
        best_liquidity = bool(best.get("liquidity_ok"))
        candidate_liquidity = bool(candidate.get("liquidity_ok"))
        if best_liquidity and not candidate_liquidity:
            continue
        if candidate_liquidity and not best_liquidity:
            best = candidate
            continue
        if side_lower in {"buy", "long"}:
            if cand_eff < best_eff:
                best = candidate
        else:
            if cand_eff > best_eff:
                best = candidate

    return best or {
        "venue": "binance",
        "expected_fill_px": 0.0,
        "fee_bps": _fee_bps_for("binance", control),
        "effective_price": 0.0,
        "liquidity_ok": False,
        "size": base_size,
        "expected_notional": 0.0,
    }
