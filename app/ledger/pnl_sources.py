from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Mapping, Sequence

from app.pnl.ledger import FundingEvent, PnLLedger, TradeFill

LOGGER = logging.getLogger(__name__)

_FUNDING_CODES = {"funding", "funding_payment", "funding_settlement"}


def _exclude_simulated_default() -> bool:
    raw = os.getenv("EXCLUDE_DRY_RUN_FROM_PNL")
    if raw is None:
        return True
    value = raw.strip().lower()
    if not value:
        return True
    return value in {"1", "true", "yes", "on"}


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
    if text in {"sell", "short", "ask"}:
        return "SELL"
    return "BUY"


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


def build_ledger_from_history(
    ctx: object | None,
    since_ts: float | None = None,
    *,
    exclude_simulated: bool | None = None,
) -> PnLLedger:
    if exclude_simulated is None:
        exclude_simulated = _exclude_simulated_default()
    ledger = PnLLedger()

    for row in sorted(_fill_rows(ctx, since_ts), key=lambda item: _coerce_timestamp(item.get("ts"))):
        venue = str(row.get("venue") or row.get("exchange") or "unknown")
        symbol = _normalise_symbol(row.get("symbol"))
        price = _coerce_decimal(row.get("price"))
        qty = _coerce_decimal(row.get("qty"))
        qty = abs(qty)
        ts = _coerce_timestamp(row.get("ts"))
        fee = _coerce_decimal(row.get("fee"))
        fee_asset = str(row.get("fee_asset") or row.get("fee_currency") or "").upper()
        simulated = bool(row.get("simulated"))
        fill = TradeFill(
            venue=venue,
            symbol=symbol,
            side=_normalise_side(row.get("side")),
            qty=qty,
            price=price,
            fee=fee,
            fee_asset=fee_asset,
            ts=ts,
            is_simulated=simulated,
        )
        ledger.apply_fill(fill, exclude_simulated=exclude_simulated)

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
        simulated = bool(payload.get("simulated") or event.get("simulated"))
        if simulated and exclude_simulated:
            continue
        venue = str(payload.get("venue") or event.get("venue") or "unknown")
        symbol = _normalise_symbol(payload.get("symbol") or event.get("symbol"))
        asset = str(payload.get("asset") or payload.get("currency") or event.get("asset") or "").upper()
        ts = _coerce_timestamp(event.get("ts"))
        funding_event = FundingEvent(
            venue=venue,
            symbol=symbol,
            amount=amount,
            asset=asset,
            ts=ts,
        )
        ledger.apply_funding(funding_event)

    return ledger


__all__: Sequence[str] = ["build_ledger_from_history"]
