from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, MutableMapping
from urllib.parse import urlencode

import httpx

from .base import Broker
from .. import ledger


LOGGER = logging.getLogger(__name__)

_DEFAULT_TESTNET_BASE_URL = "https://testnet.binancefuture.com"
_DEFAULT_LIVE_BASE_URL = "https://fapi.binance.com"
_DEFAULT_RECV_WINDOW = 5_000
_HTTP_TIMEOUT = 10.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp_ms() -> int:
    return int(time.time() * 1000)


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _abs_nonzero(value: Any) -> float:
    numeric = _float(value, default=0.0)
    if abs(numeric) <= 1e-12:
        return 0.0
    return numeric


def _iso_ts_from_ms(value: Any) -> str:
    try:
        numeric = int(float(value))
    except (TypeError, ValueError):
        return _now_iso()
    seconds = numeric / 1000.0
    return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class _Credentials:
    api_key: str
    api_secret: str


class _BaseBinanceBroker(Broker):
    def __init__(
        self,
        *,
        venue: str,
        safe_mode: bool,
        dry_run: bool,
        base_url: str | None,
        credentials: _Credentials | None,
        api_key_env: str,
        api_secret_env: str,
        base_url_env: str,
        default_base_url: str,
        venue_type: str,
    ) -> None:
        self.venue = venue
        self.venue_type = venue_type
        self.safe_mode = safe_mode
        self.dry_run = dry_run
        env_base_url = os.getenv(base_url_env)
        self.base_url = base_url or env_base_url or default_base_url
        if credentials is None:
            api_key = os.getenv(api_key_env)
            api_secret = os.getenv(api_secret_env)
            if api_key and api_secret:
                credentials = _Credentials(api_key=api_key, api_secret=api_secret)
        self.credentials = credentials
        self._recent_symbol_limit = 25
        self._symbol_cache: set[str] = set()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------
    def _headers(self) -> Dict[str, str]:
        if not self.credentials:
            return {}
        return {"X-MBX-APIKEY": self.credentials.api_key}

    def _sign(self, params: MutableMapping[str, Any]) -> None:
        if not self.credentials:
            raise RuntimeError("Binance credentials are not configured")
        items: List[tuple[str, str]] = []
        for key, value in list(params.items()):
            if isinstance(value, bool):
                normalised = "true" if value else "false"
            elif value is None:
                continue
            else:
                normalised = str(value)
            params[key] = normalised
            items.append((key, normalised))
        query = urlencode(items, doseq=True)
        signature = hmac.new(
            self.credentials.api_secret.encode("utf-8"),
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        params["signature"] = signature

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: MutableMapping[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        params = dict(params or {})
        if signed:
            params.setdefault("recvWindow", _DEFAULT_RECV_WINDOW)
            params["timestamp"] = _timestamp_ms()
            self._sign(params)
        async with httpx.AsyncClient(base_url=self.base_url, timeout=_HTTP_TIMEOUT) as client:
            response = await client.request(
                method,
                path,
                params=params,
                headers=self._headers(),
            )
        response.raise_for_status()
        return response.json()

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------
    @property
    def _allow_trading(self) -> bool:
        return not (self.safe_mode or self.dry_run)

    def _credentials_ready(self) -> bool:
        return self.credentials is not None

    async def get_account_state(self) -> Dict[str, Any]:
        if not self._credentials_ready():
            LOGGER.info("binance broker missing credentials; returning empty account state")
            return {"balances": [], "positions": []}
        try:
            payload = await self._request("GET", "/fapi/v2/account", signed=True)
        except httpx.HTTPError as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("failed to fetch binance account", extra={"error": str(exc)})
            return {"balances": [], "positions": []}
        balances = self._normalise_balances(payload)
        positions = self._normalise_positions(payload)
        return {"balances": balances, "positions": positions}

    # ------------------------------------------------------------------
    # Normalisers
    # ------------------------------------------------------------------
    def _normalise_balances(self, payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
        assets = payload.get("assets")
        if not isinstance(assets, Iterable):
            return []
        balances: List[Dict[str, Any]] = []
        for item in assets:
            if not isinstance(item, Mapping):
                continue
            asset = str(item.get("asset") or "").upper()
            if not asset:
                continue
            total = _float(item.get("walletBalance"), 0.0)
            free = _float(item.get("availableBalance"), total)
            if abs(total) <= 1e-12 and abs(free) <= 1e-12:
                continue
            balances.append(
                {
                    "venue": self.venue,
                    "asset": asset,
                    "free": free,
                    "total": total,
                }
            )
        return balances

    def _normalise_positions(self, payload: Mapping[str, Any]) -> List[Dict[str, Any]]:
        positions_payload = payload.get("positions")
        if not isinstance(positions_payload, Iterable):
            return []
        positions: List[Dict[str, Any]] = []
        for row in positions_payload:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not symbol:
                continue
            qty = _abs_nonzero(row.get("positionAmt"))
            if qty == 0.0:
                continue
            entry = _float(row.get("entryPrice"), 0.0)
            mark = _float(row.get("markPrice"), entry)
            notional = abs(qty) * mark if mark else abs(qty) * entry
            positions.append(
                {
                    "venue": self.venue,
                    "venue_type": self.venue_type,
                    "symbol": symbol,
                    "qty": qty,
                    "avg_entry": entry,
                    "mark_price": mark,
                    "notional": notional,
                }
            )
        return positions

    def _order_payload(
        self,
        *,
        order_id: int,
        symbol: str,
        side: str,
        qty: float,
        price: float | None,
        fee: float,
        idemp_key: str,
    ) -> Dict[str, Any]:
        return {
            "order_id": order_id,
            "venue": self.venue,
            "symbol": symbol,
            "side": side,
            "qty": qty,
            "price": price if price is not None else 0.0,
            "fee": fee,
            "type": "LIMIT",
            "post_only": True,
            "reduce_only": False,
            "ts": _now_iso(),
            "idemp_key": idemp_key,
        }

    async def _record_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        price: float | None,
        idemp_key: str,
        status: str,
    ) -> int:
        return await asyncio.to_thread(
            ledger.record_order,
            venue=self.venue,
            symbol=symbol,
            side=side,
            qty=qty,
            price=price,
            status=status,
            client_ts=_now_iso(),
            exchange_ts=None,
            idemp_key=idemp_key,
        )

    # ------------------------------------------------------------------
    # Broker interface
    # ------------------------------------------------------------------
    async def create_order(
        self,
        *,
        venue: str,
        symbol: str,
        side: str,
        qty: float,
        price: float | None = None,
        type: str = "LIMIT",
        post_only: bool = True,
        reduce_only: bool = False,
        fee: float = 0.0,
        idemp_key: str | None = None,
    ) -> Dict[str, Any]:
        symbol_u = symbol.upper()
        side_l = side.lower()
        id_key = idemp_key or f"{self.venue}-{symbol_u}-{_timestamp_ms()}"
        order_id = await self._record_order(
            symbol=symbol_u,
            side=side_l,
            qty=float(qty),
            price=float(price) if price is not None else None,
            idemp_key=id_key,
            status="submitted",
        )
        if not self._allow_trading:
            await asyncio.to_thread(ledger.update_order_status, order_id, "skipped")
            ledger.record_event(
                level="INFO",
                code="order_skipped",
                payload={
                    "venue": self.venue,
                    "symbol": symbol_u,
                    "side": side_l,
                    "reason": "SAFE_MODE",
                },
            )
            return self._order_payload(
                order_id=order_id,
                symbol=symbol_u,
                side=side_l,
                qty=float(qty),
                price=float(price) if price is not None else None,
                fee=float(fee),
                idemp_key=id_key,
            )

        if not self._credentials_ready():
            await asyncio.to_thread(ledger.update_order_status, order_id, "failed")
            raise RuntimeError("Binance credentials missing")

        params: Dict[str, Any] = {
            "symbol": symbol_u,
            "side": side.upper(),
            "type": type.upper(),
            "quantity": qty,
            "newClientOrderId": id_key,
        }
        if price is not None:
            params["price"] = price
        if type.upper() == "LIMIT":
            params["timeInForce"] = "GTX" if post_only else "GTC"
        if reduce_only:
            params["reduceOnly"] = True
        try:
            response = await self._request("POST", "/fapi/v1/order", params=params, signed=True)
        except Exception as exc:  # pragma: no cover - defensive logging
            await asyncio.to_thread(ledger.update_order_status, order_id, "failed")
            ledger.record_event(
                level="ERROR",
                code="binance_order_error",
                payload={"venue": self.venue, "symbol": symbol_u, "error": str(exc)},
            )
            raise

        status = str(response.get("status") or "NEW").lower()
        if status in {"canceled", "rejected"}:
            await asyncio.to_thread(ledger.update_order_status, order_id, "failed")
        elif status in {"filled", "partially_filled"}:
            await asyncio.to_thread(ledger.update_order_status, order_id, "filled")
        else:
            await asyncio.to_thread(ledger.update_order_status, order_id, "open")

        payload = self._order_payload(
            order_id=order_id,
            symbol=symbol_u,
            side=side_l,
            qty=float(qty),
            price=float(price) if price is not None else None,
            fee=float(fee),
            idemp_key=id_key,
        )
        payload["exchange_order_id"] = response.get("orderId")
        payload["status"] = status
        return payload

    async def cancel(self, *, venue: str, order_id: int) -> None:
        order = await asyncio.to_thread(ledger.get_order, order_id)
        symbol = str(order.get("symbol") or "").upper() if order else None
        client_order_id = str(order.get("idemp_key") or "") if order else ""
        if not symbol:
            LOGGER.warning("cancel called without known symbol", extra={"order_id": order_id})
            return
        if not self._allow_trading:
            await asyncio.to_thread(ledger.update_order_status, order_id, "cancelled")
            return
        if not self._credentials_ready():
            await asyncio.to_thread(ledger.update_order_status, order_id, "cancelled")
            ledger.record_event(
                level="WARNING",
                code="binance_cancel_skipped",
                payload={"venue": self.venue, "order_id": order_id, "reason": "missing_credentials"},
            )
            return
        params: Dict[str, Any] = {"symbol": symbol}
        if client_order_id:
            params["origClientOrderId"] = client_order_id
        else:
            params["orderId"] = order_id
        try:
            await self._request("DELETE", "/fapi/v1/order", params=params, signed=True)
            await asyncio.to_thread(ledger.update_order_status, order_id, "cancelled")
        except Exception as exc:  # pragma: no cover - defensive logging
            ledger.record_event(
                level="ERROR",
                code="binance_cancel_error",
                payload={"venue": self.venue, "order_id": order_id, "error": str(exc)},
            )
            raise

    async def cancel_all(self, symbol: str | None = None) -> Dict[str, Any]:
        if not self._allow_trading:
            return {"cancelled": 0, "failed": 0, "skipped": True}
        if not self._credentials_ready():
            return {"cancelled": 0, "failed": 0, "skipped": True, "reason": "missing_credentials"}
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol.upper()
        try:
            await self._request("DELETE", "/fapi/v1/allOpenOrders", params=params, signed=True)
        except Exception as exc:  # pragma: no cover - defensive logging
            ledger.record_event(
                level="ERROR",
                code="binance_cancel_all_error",
                payload={"venue": self.venue, "symbol": symbol, "error": str(exc)},
            )
            raise
        return {"cancelled": "all", "failed": 0}

    async def positions(self, *, venue: str) -> Dict[str, Any]:
        state = await self.get_account_state()
        return {"positions": state.get("positions", [])}

    async def balances(self, *, venue: str) -> Dict[str, Any]:
        state = await self.get_account_state()
        return {"balances": state.get("balances", [])}

    async def get_positions(self) -> List[Dict[str, Any]]:
        state = await self.get_account_state()
        rows = state.get("positions", [])
        exposures: List[Dict[str, Any]] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            qty = _float(row.get("qty"), 0.0)
            if abs(qty) <= 1e-12:
                continue
            entry = _float(row.get("avg_entry"), 0.0)
            mark = _float(row.get("mark_price"), entry)
            exposures.append(
                {
                    "venue": self.venue,
                    "venue_type": self.venue_type,
                    "symbol": symbol,
                    "qty": qty,
                    "avg_entry": entry,
                    "notional": abs(qty) * (mark or entry),
                }
            )
        return exposures

    async def _recent_fill_symbols(self) -> List[str]:
        rows = await asyncio.to_thread(ledger.fetch_recent_fills, self._recent_symbol_limit)
        symbols: List[str] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            symbol = str(row.get("symbol") or "").upper()
            if not symbol or symbol in symbols:
                continue
            symbols.append(symbol)
        return symbols

    async def _symbols_for_fills(self) -> List[str]:
        buckets = [await self._active_symbols(), await self._recent_fill_symbols(), list(self._symbol_cache)]
        symbols: List[str] = []
        seen: set[str] = set()
        for bucket in buckets:
            for symbol in bucket:
                normalised = str(symbol or "").upper()
                if not normalised or normalised in seen:
                    continue
                symbols.append(normalised)
                seen.add(normalised)
        if not symbols:
            symbols = ["BTCUSDT"]
        self._symbol_cache.update(symbols)
        return symbols

    async def get_fills(self, since: datetime | None = None) -> List[Dict[str, Any]]:
        if not self._credentials_ready():
            return []
        params: Dict[str, Any] = {}
        if since is not None:
            params["startTime"] = int(since.timestamp() * 1000)
        try:
            trades: List[Dict[str, Any]] = []
            symbols = await self._symbols_for_fills()
            for symbol in symbols:
                symbol_params = dict(params)
                symbol_params["symbol"] = symbol
                response = await self._request("GET", "/fapi/v1/userTrades", params=symbol_params, signed=True)
                if isinstance(response, list):
                    trades.extend(response)
        except Exception as exc:  # pragma: no cover - defensive logging
            LOGGER.warning("failed to fetch binance fills", extra={"error": str(exc)})
            return []
        fills: List[Dict[str, Any]] = []
        for trade in trades:
            if not isinstance(trade, Mapping):
                continue
            symbol = str(trade.get("symbol") or "").upper()
            qty = _float(trade.get("qty"), 0.0)
            if qty == 0.0:
                continue
            price = _float(trade.get("price"), 0.0)
            fee = _float(trade.get("commission"), 0.0)
            side = "buy" if trade.get("buyer") else "sell"
            ts = _iso_ts_from_ms(trade.get("time"))
            fills.append(
                {
                    "venue": self.venue,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "fee": fee,
                    "ts": ts,
                }
            )
        return fills

    async def _active_symbols(self) -> List[str]:
        state = await self.get_account_state()
        positions = state.get("positions", [])
        symbols = {str(row.get("symbol") or "").upper() for row in positions if row.get("symbol")}
        return [symbol for symbol in symbols if symbol]


class BinanceTestnetBroker(_BaseBinanceBroker):
    """Broker implementation for Binance USDT-M futures testnet."""

    def __init__(
        self,
        *,
        venue: str = "binance-um",
        safe_mode: bool = True,
        dry_run: bool = False,
        base_url: str | None = None,
        credentials: _Credentials | None = None,
    ) -> None:
        super().__init__(
            venue=venue,
            safe_mode=safe_mode,
            dry_run=dry_run,
            base_url=base_url,
            credentials=credentials,
            api_key_env="BINANCE_UM_API_KEY_TESTNET",
            api_secret_env="BINANCE_UM_API_SECRET_TESTNET",
            base_url_env="BINANCE_UM_BASE_TESTNET",
            default_base_url=_DEFAULT_TESTNET_BASE_URL,
            venue_type="binance-testnet",
        )


class BinanceLiveBroker(_BaseBinanceBroker):
    """Broker implementation for live Binance USDT-M futures."""

    def __init__(
        self,
        *,
        venue: str = "binance-um",
        safe_mode: bool = True,
        dry_run: bool = False,
        base_url: str | None = None,
        credentials: _Credentials | None = None,
    ) -> None:
        super().__init__(
            venue=venue,
            safe_mode=safe_mode,
            dry_run=dry_run,
            base_url=base_url,
            credentials=credentials,
            api_key_env="BINANCE_LV_API_KEY",
            api_secret_env="BINANCE_LV_API_SECRET",
            base_url_env="BINANCE_LV_BASE_URL",
            default_base_url=_DEFAULT_LIVE_BASE_URL,
            venue_type="binance-live",
        )

