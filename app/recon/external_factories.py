from __future__ import annotations

import asyncio
import inspect
import logging
import os
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from app.config.loader import load_app_config
from app.recon.external_client import ExchangeAccountClient
from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
    VenueId,
)

LOGGER = logging.getLogger(__name__)

_ConfigType = Any


async def _invoke_loader(loader: Callable[[], Any]) -> Any:
    if inspect.iscoroutinefunction(loader):
        return await loader()
    result = await asyncio.to_thread(loader)
    if inspect.isawaitable(result):  # pragma: no cover - defensive
        return await result
    return result


def _to_decimal(value: object, *, default: Decimal | None = Decimal("0")) -> Decimal | None:
    if value is None:
        return default
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):  # pragma: no cover - defensive
        return default


def _first_decimal(
    entry: Mapping[str, object], *keys: str, default: Decimal | None = Decimal("0")
) -> Decimal | None:
    for key in keys:
        if key not in entry:
            continue
        result = _to_decimal(entry.get(key), default=None)
        if result is not None:
            return result
    return default


def _normalise_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if not text:
        return ""
    if text.endswith("-SWAP"):
        text = text[:-5]
    return text.replace("-", "").replace("_", "")


def _normalise_side(value: object) -> str:
    token = str(value or "").strip().lower()
    if token in {"buy", "bid", "long"}:
        return "buy"
    if token in {"sell", "ask", "short"}:
        return "sell"
    return "buy"


def _iter_entries(payload: object) -> Sequence[object]:
    if isinstance(payload, Sequence) and not isinstance(payload, (str, bytes, bytearray)):
        return payload
    if isinstance(payload, Mapping):
        return list(payload.values())
    return []


def _parse_balances(venue: VenueId, payload: object) -> list[ExchangeBalanceSnapshot]:
    entries = _iter_entries(payload) if payload is not None else []
    if not entries and isinstance(payload, Mapping):
        entries = [payload]
    snapshots: list[ExchangeBalanceSnapshot] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        asset_raw = (
            item.get("asset")
            or item.get("currency")
            or item.get("symbol")
            or item.get("ccy")
            or "USDT"
        )
        asset = str(asset_raw or "").upper()
        total = _first_decimal(
            item,
            "total",
            "balance",
            "walletBalance",
            "total_balance",
            "equity",
            "totalEquity",
            "netValue",
            default=Decimal("0"),
        ) or Decimal("0")
        available = (
            _first_decimal(
                item,
                "available",
                "free",
                "available_balance",
                "availableBalance",
                "freeCollateral",
                "availableMargin",
                "availBal",
                "cashBal",
                default=total,
            )
            or total
        )
        snapshots.append(
            ExchangeBalanceSnapshot(
                venue_id=str(venue),
                asset=asset,
                total=total,
                available=available,
            )
        )
    return snapshots


def _parse_positions(venue: VenueId, payload: object) -> list[ExchangePositionSnapshot]:
    entries = _iter_entries(payload)
    snapshots: list[ExchangePositionSnapshot] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        symbol_raw = item.get("symbol") or item.get("instId") or item.get("instrument")
        symbol = _normalise_symbol(symbol_raw)
        if not symbol:
            continue
        qty = _first_decimal(
            item,
            "position_amt",
            "positionAmt",
            "pos",
            "qty",
            "size",
        )
        if qty is None:
            long_qty = _first_decimal(item, "long", "long_qty", "longQty", default=None) or Decimal(
                "0"
            )
            short_qty = _first_decimal(
                item, "short", "short_qty", "shortQty", default=None
            ) or Decimal("0")
            qty = long_qty - short_qty
        qty = qty or Decimal("0")
        entry_price = _first_decimal(
            item,
            "entry_price",
            "entryPrice",
            "avgPx",
            "avg_price",
            "avgPrice",
            "price",
            default=None,
        )
        notional = qty.copy_abs() * entry_price if entry_price is not None else Decimal("0")
        snapshots.append(
            ExchangePositionSnapshot(
                venue_id=str(venue),
                symbol=symbol,
                qty=qty,
                entry_price=entry_price,
                notional=notional,
            )
        )
    return snapshots


def _parse_orders(venue: VenueId, payload: object) -> list[ExchangeOrderSnapshot]:
    entries = _iter_entries(payload)
    snapshots: list[ExchangeOrderSnapshot] = []
    for item in entries:
        if not isinstance(item, Mapping):
            continue
        symbol_raw = item.get("symbol") or item.get("instId") or item.get("instrument")
        symbol = _normalise_symbol(symbol_raw)
        if not symbol:
            continue
        qty = _first_decimal(
            item, "qty", "quantity", "origQty", "size", default=Decimal("0")
        ) or Decimal("0")
        price = _first_decimal(
            item, "price", "px", "avgPrice", "avgPx", default=Decimal("0")
        ) or Decimal("0")
        side = _normalise_side(item.get("side") or item.get("posSide"))
        status = str(
            item.get("status")
            or item.get("state")
            or item.get("orderStatus")
            or item.get("ordStatus")
            or "unknown"
        ).lower()
        client_order_id_raw = (
            item.get("clientOrderId")
            or item.get("client_order_id")
            or item.get("clOrdId")
            or item.get("idemp_key")
        )
        client_order_id = str(client_order_id_raw) if client_order_id_raw else None
        exchange_order_id_raw = (
            item.get("orderId") or item.get("ordId") or item.get("exchange_order_id")
        )
        exchange_order_id = str(exchange_order_id_raw) if exchange_order_id_raw else None
        snapshots.append(
            ExchangeOrderSnapshot(
                venue_id=str(venue),
                symbol=symbol,
                client_order_id=client_order_id,
                exchange_order_id=exchange_order_id,
                side=side,
                qty=qty,
                price=price,
                status=status,
            )
        )
    return snapshots


class _ExchangeClientAdapter(ExchangeAccountClient):
    def __init__(
        self,
        venue_id: VenueId,
        *,
        balance_loader: Callable[[], Any] | None,
        positions_loader: Callable[[], Any] | None,
        orders_loader: Callable[[], Any] | None,
        owners: Sequence[object] | None = None,
    ) -> None:
        self._venue = venue_id
        self._balance_loader = balance_loader
        self._positions_loader = positions_loader
        self._orders_loader = orders_loader
        self._owners = list(owners or [])  # keep strong refs to underlying clients

    async def load_balances(self, venue_id: VenueId) -> Sequence[ExchangeBalanceSnapshot]:
        loader = self._balance_loader
        if loader is None:
            return []
        try:
            payload = await _invoke_loader(loader)
        except Exception:  # pragma: no cover - defensive logging
            LOGGER.exception("external_balances.fetch_failed", extra={"venue": venue_id})
            return []
        return _parse_balances(venue_id, payload)

    async def load_positions(self, venue_id: VenueId) -> Sequence[ExchangePositionSnapshot]:
        loader = self._positions_loader
        if loader is None:
            return []
        try:
            payload = await _invoke_loader(loader)
        except Exception:  # pragma: no cover - defensive logging
            LOGGER.exception("external_positions.fetch_failed", extra={"venue": venue_id})
            return []
        return _parse_positions(venue_id, payload)

    async def load_open_orders(self, venue_id: VenueId) -> Sequence[ExchangeOrderSnapshot]:
        loader = self._orders_loader
        if loader is None:
            return []
        try:
            payload = await _invoke_loader(loader)
        except Exception:  # pragma: no cover - defensive logging
            LOGGER.exception("external_orders.fetch_failed", extra={"venue": venue_id})
            return []
        return _parse_orders(venue_id, payload)


def _resolve_balance_loader(client: object) -> Callable[[], Any] | None:
    for name in (
        "get_account_limits",
        "account_snapshot",
        "get_account_snapshot",
        "get_account_state",
        "account_state",
        "balances",
        "fetch_balances",
    ):
        attr = getattr(client, name, None)
        if callable(attr):
            return attr  # type: ignore[return-value]
    return None


def _resolve_runtime_client(venue_id: VenueId) -> ExchangeAccountClient | None:
    try:
        from app.services import runtime
    except Exception:  # pragma: no cover - runtime not initialised
        return None
    try:
        state = runtime.get_state()
    except Exception:  # pragma: no cover - runtime state missing
        return None
    derivatives = getattr(state, "derivatives", None)
    venues = getattr(derivatives, "venues", None)
    if not venues:
        return None
    runtime_entry = venues.get(venue_id)
    if runtime_entry is None:
        return None
    client = getattr(runtime_entry, "client", None)
    if client is None:
        return None
    balance_loader = _resolve_balance_loader(client)
    positions_loader = getattr(client, "positions", None)
    orders_loader = getattr(client, "open_orders", None)
    if not callable(positions_loader) or not callable(orders_loader):
        return None
    return _ExchangeClientAdapter(
        venue_id,
        balance_loader=balance_loader,
        positions_loader=positions_loader,
        orders_loader=orders_loader,
        owners=(client,),
    )


_CONFIG_CACHE: _ConfigType | None = None


def _load_config() -> _ConfigType | None:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    try:
        from app.services.runtime import resolve_profile_config_path
    except Exception:  # pragma: no cover - default profile resolution

        def resolve_profile_config_path(_: str | None) -> str:  # type: ignore[override]
            return "configs/config.paper.yaml"

    profile = (
        os.environ.get("PROFILE")
        or os.environ.get("EXCHANGE_PROFILE")
        or os.environ.get("EXEC_PROFILE")
        or os.environ.get("ENVIRONMENT")
        or os.environ.get("ENV")
        or "paper"
    )
    path = resolve_profile_config_path(profile)
    try:
        _CONFIG_CACHE = load_app_config(path)
    except Exception:  # pragma: no cover - defensive logging
        LOGGER.exception("external_config.load_failed", extra={"path": path})
        return None
    return _CONFIG_CACHE


def _find_deriv_config(venue_id: VenueId) -> Any | None:
    loaded = _load_config()
    if loaded is None:
        return None
    derivatives = getattr(loaded.data, "derivatives", None)
    if not derivatives:
        return None
    for entry in getattr(derivatives, "venues", []):
        if getattr(entry, "id", None) == venue_id:
            return entry
    return None


def _determine_safe_mode() -> bool:
    try:
        from app.services import runtime

        state = runtime.get_state()
        control = getattr(state, "control", None)
        if control is not None:
            return bool(getattr(control, "safe_mode", True))
    except Exception:  # pragma: no cover - default to safe
        LOGGER.debug("external_safe_mode_resolution_failed", exc_info=True)
    raw = os.environ.get("SAFE_MODE")
    if raw is not None:
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return True


def _build_exchange_client(venue_id: VenueId) -> ExchangeAccountClient | None:
    config = _find_deriv_config(venue_id)
    if config is None:
        return None
    safe_mode = _determine_safe_mode()
    owners: list[object] = []
    balance_loader: Callable[[], Any] | None = None
    positions_loader: Callable[[], Any] | None = None
    orders_loader: Callable[[], Any] | None = None

    try:
        if str(venue_id).startswith("binance"):
            from app.exchanges import binance_um

            client = binance_um.create_client(config, safe_mode=safe_mode)
            owners.append(client)
            balance_loader = _resolve_balance_loader(client)
            positions_loader = getattr(client, "positions", None)
            orders_loader = getattr(client, "open_orders", None)
            if balance_loader is None:
                try:
                    from exchanges.binance_futures import BinanceFuturesClient
                except Exception:  # pragma: no cover - optional dependency missing
                    balance_loader = None
                else:
                    api_url = getattr(getattr(config, "routing", None), "rest", None)
                    balance_client = (
                        BinanceFuturesClient(api_url=api_url) if api_url else BinanceFuturesClient()
                    )
                    owners.append(balance_client)
                    balance_loader = getattr(balance_client, "get_account_limits", None)
        elif str(venue_id).startswith("okx"):
            from app.exchanges import okx_perp

            client = okx_perp.create_client(config, safe_mode=safe_mode)
            owners.append(client)
            balance_loader = _resolve_balance_loader(client)
            positions_loader = getattr(client, "positions", None)
            orders_loader = getattr(client, "open_orders", None)
            if balance_loader is None:
                try:
                    from exchanges.okx_futures import OKXFuturesClient
                except Exception:  # pragma: no cover - optional dependency missing
                    balance_loader = None
                else:
                    api_url = getattr(getattr(config, "routing", None), "rest", None)
                    balance_client = (
                        OKXFuturesClient(api_url=api_url) if api_url else OKXFuturesClient()
                    )
                    owners.append(balance_client)
                    balance_loader = getattr(balance_client, "get_account_limits", None)
        elif str(venue_id).startswith("bybit"):
            from app.exchanges import build_in_memory_client

            symbols = getattr(config, "symbols", ()) or ()
            client = build_in_memory_client(venue_id, symbols)
            owners.append(client)
            balance_loader = None
            positions_loader = getattr(client, "positions", None)
            orders_loader = getattr(client, "open_orders", None)
    except Exception:  # pragma: no cover - defensive logging
        LOGGER.exception("external_client.create_failed", extra={"venue": venue_id})
        return None

    if not callable(positions_loader) or not callable(orders_loader):
        return None

    return _ExchangeClientAdapter(
        venue_id,
        balance_loader=balance_loader,
        positions_loader=positions_loader,
        orders_loader=orders_loader,
        owners=owners,
    )


def get_exchange_account_client_for_venue(venue_id: VenueId) -> ExchangeAccountClient | None:
    client = _resolve_runtime_client(venue_id)
    if client is not None:
        return client
    return _build_exchange_client(venue_id)


__all__ = ["get_exchange_account_client_for_venue"]
