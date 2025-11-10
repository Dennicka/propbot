from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from .pnl_ledger import PnLLedger

LOGGER = logging.getLogger(__name__)

_FUNDING_CODES = {"funding", "funding_payment", "funding_settlement"}


def _coerce_timestamp(value: object) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return 0.0
        try:
            return float(raw)
        except ValueError:
            cleaned = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
            try:
                parsed = datetime.fromisoformat(cleaned)
            except ValueError:
                LOGGER.warning("pnl_ledger.invalid_ts", extra={"value": value})
                return 0.0
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.timestamp()
    LOGGER.warning("pnl_ledger.unknown_ts_type", extra={"value": value})
    return 0.0


def _coerce_decimal(value: object) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal("0")
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception:
            return Decimal("0")
    return Decimal("0")


def _normalise_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    return text or "UNKNOWN"


def _normalise_side(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in {"buy", "long", "bid"}:
        return "LONG"
    if text in {"sell", "short", "ask"}:
        return "SHORT"
    return "FLAT"


def _signed_qty(row: Mapping[str, object]) -> Decimal:
    qty = _coerce_decimal(row.get("qty"))
    side = str(row.get("side") or "").lower()
    if side in {"sell", "short", "ask"}:
        return -qty
    return qty


def _apply_realized_state(
    state: dict[str, dict[str, Decimal]], symbol: str, qty: Decimal, price: Decimal
) -> Decimal:
    if qty == 0:
        return Decimal("0")
    symbol_state = state.setdefault(symbol, {"qty": Decimal("0"), "avg": Decimal("0")})
    position_qty = symbol_state["qty"]
    avg_price = symbol_state["avg"]
    realized = Decimal("0")
    if position_qty == 0 or position_qty * qty > 0:
        new_qty = position_qty + qty
        if new_qty == 0:
            symbol_state["qty"] = Decimal("0")
            symbol_state["avg"] = Decimal("0")
        else:
            total_cost = avg_price * position_qty + price * qty
            symbol_state["qty"] = new_qty
            symbol_state["avg"] = total_cost / new_qty
    else:
        close_qty = min(abs(position_qty), abs(qty))
        direction = Decimal("1") if position_qty > 0 else Decimal("-1")
        realized = (price - avg_price) * close_qty * direction
        new_qty = position_qty + qty
        if new_qty == 0:
            symbol_state["qty"] = Decimal("0")
            symbol_state["avg"] = Decimal("0")
        elif position_qty * new_qty > 0:
            symbol_state["qty"] = new_qty
            symbol_state["avg"] = avg_price
        else:
            symbol_state["qty"] = new_qty
            symbol_state["avg"] = price
    return realized


def _fill_rows(ctx: object | None, since_ts: float | None) -> Iterable[Mapping[str, object]]:
    fetcher = None
    if ctx is not None:
        fetcher = getattr(ctx, "fetch_fills_since", None)
        if fetcher is None:
            ledger_attr = getattr(ctx, "ledger", None)
            fetcher = getattr(ledger_attr, "fetch_fills_since", None) if ledger_attr else None
    if fetcher is None:
        from . import fetch_fills_since as default_fetch_fills

        fetcher = default_fetch_fills
    since_arg: object | None
    if since_ts is None or since_ts <= 0:
        since_arg = None
    else:
        since_arg = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
    try:
        rows = fetcher(since=since_arg)
    except TypeError:
        rows = fetcher(since_arg)  # type: ignore[misc]
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.error("pnl_ledger.fetch_fills_failed", exc_info=exc)
        return []
    if rows is None:
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _funding_rows(ctx: object | None, since_ts: float | None) -> Iterable[Mapping[str, object]]:
    fetcher = None
    if ctx is not None:
        fetcher = getattr(ctx, "fetch_events", None)
        if fetcher is None:
            ledger_attr = getattr(ctx, "ledger", None)
            fetcher = getattr(ledger_attr, "fetch_events", None) if ledger_attr else None
    if fetcher is None:
        from . import fetch_events as default_fetch_events

        fetcher = default_fetch_events
    params = {"order": "asc", "limit": 500}
    if since_ts is not None and since_ts > 0:
        params["since"] = datetime.fromtimestamp(since_ts, tz=timezone.utc).isoformat()
    try:
        rows = fetcher(**params)
    except Exception as exc:  # pragma: no cover - defensive
        LOGGER.error("pnl_ledger.fetch_events_failed", exc_info=exc)
        return []
    if rows is None:
        return []
    filtered: list[Mapping[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        code = str(row.get("code") or row.get("type") or "").lower()
        if code not in _FUNDING_CODES:
            continue
        filtered.append(row)
    return filtered


def _fee_components(raw_fee: Decimal) -> tuple[Decimal, Decimal]:
    if raw_fee >= 0:
        return raw_fee, Decimal("0")
    return Decimal("0"), -raw_fee


def build_ledger_from_history(ctx: object | None, since_ts: float | None = None) -> PnLLedger:
    ledger = PnLLedger()
    realized_state: dict[str, dict[str, Decimal]] = {}

    for row in sorted(_fill_rows(ctx, since_ts), key=lambda item: _coerce_timestamp(item.get("ts"))):
        symbol = _normalise_symbol(row.get("symbol"))
        price = _coerce_decimal(row.get("price"))
        qty_signed = _signed_qty(row)
        realized = _apply_realized_state(realized_state, symbol, qty_signed, price)
        ts = _coerce_timestamp(row.get("ts"))
        notional = abs(price * qty_signed)
        fee_raw = _coerce_decimal(row.get("fee"))
        fee, rebate = _fee_components(fee_raw)
        ref_id = row.get("id") or row.get("order_id") or row.get("trade_id")
        simulated = bool(row.get("simulated"))
        ledger.record_fill(
            ts=ts,
            symbol=symbol,
            side=_normalise_side(row.get("side")),
            realized_pnl=realized,
            fee=fee,
            rebate=rebate,
            funding=Decimal("0"),
            notional=notional,
            ref_id=str(ref_id) if ref_id is not None else None,
            simulated=simulated,
        )

    for event in _funding_rows(ctx, since_ts):
        payload = event.get("payload") if isinstance(event.get("payload"), Mapping) else {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}
        amount = _coerce_decimal(
            payload.get("amount")
            or payload.get("pnl")
            or event.get("amount")
            or event.get("pnl")
        )
        if amount == 0:
            continue
        ts = _coerce_timestamp(event.get("ts"))
        symbol = _normalise_symbol(payload.get("symbol") or event.get("symbol"))
        ref_id = (
            payload.get("id")
            or payload.get("funding_id")
            or payload.get("reference")
            or event.get("id")
        )
        simulated = bool(payload.get("simulated") or event.get("simulated"))
        ledger.record_funding(
            ts=ts,
            symbol=symbol,
            amount=amount,
            ref_id=str(ref_id) if ref_id is not None else None,
            simulated=simulated,
        )

    return ledger


__all__: Sequence[str] = ["build_ledger_from_history"]
