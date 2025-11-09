from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import Any, Dict, Optional, TYPE_CHECKING

import httpx
try:  # pragma: no cover - shim used when requests is unavailable
    import requests
    RequestError = requests.RequestException
except ImportError:  # pragma: no cover
    class _RequestsShim:
        @staticmethod
        def get(url: str, params: Dict[str, str] | None = None, timeout: float | None = None) -> httpx.Response:
            return httpx.get(url, params=params, timeout=timeout)

    requests = _RequestsShim()
    RequestError = Exception

from . import InMemoryDerivClient, build_in_memory_client
from app.utils.chaos import apply_order_delay, maybe_raise_rest_timeout
from app.watchdog.broker_watchdog import get_broker_watchdog

if TYPE_CHECKING:  # pragma: no cover - type checking only
    from ..core.config import DerivVenueConfig


LOGGER = logging.getLogger(__name__)


class BinanceUMClient:
    """Minimal USD-M Futures client targeting Binance testnet."""

    def __init__(self, config: DerivVenueConfig, *, safe_mode: bool = True) -> None:
        self.config = config
        self.safe_mode = safe_mode
        self._fallback = build_in_memory_client(config.id, config.symbols)
        self.position_mode: str = config.position_mode
        self.margin_type: Dict[str, str] = {symbol: config.margin_type for symbol in config.symbols}
        self.leverage: Dict[str, int] = {symbol: config.leverage for symbol in config.symbols}
        self.positions_data = self._fallback.positions_data
        self._filters_cache: Dict[str, Dict[str, float]] = {}
        self._fees_cache: Dict[str, Dict[str, float]] = {}
        self._client: Optional[httpx.Client] = None
        self._api_key = os.getenv("BINANCE_UM_API_KEY_TESTNET")
        self._api_secret = os.getenv("BINANCE_UM_API_SECRET_TESTNET")
        self._watchdog = get_broker_watchdog()

        if not safe_mode:
            if not self._api_key or not self._api_secret:
                raise RuntimeError("Binance UM credentials missing for testnet access")
            self._client = httpx.Client(base_url=config.routing.rest, timeout=10.0)

    # ------------------------------------------------------------------
    # Helpers

    def _ensure_http(self) -> httpx.Client:
        if self.safe_mode:
            raise RuntimeError("HTTP client not available in SAFE_MODE")
        assert self._client is not None  # for type-checkers
        return self._client

    def _timestamp(self) -> int:
        return int(time.time() * 1000)

    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._api_secret:
            raise RuntimeError("Binance UM secret not configured")
        query = httpx.QueryParams(params).to_str()
        signature = hmac.new(
            self._api_secret.encode("utf-8"), query.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        params["signature"] = signature
        return params

    def _signed_request(
        self,
        method: str,
        path: str,
        params: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | list[Any]:
        client = self._ensure_http()
        params = params or {}
        params.setdefault("timestamp", self._timestamp())
        headers = {"X-MBX-APIKEY": self._api_key or ""}
        signed = self._sign(params.copy())
        try:
            maybe_raise_rest_timeout(context="binance_um.signed_request")
            response = client.request(method, path, params=signed, headers=headers)
            response.raise_for_status()
        except httpx.TimeoutException:
            self._watchdog.record_rest_error(self.config.id, "timeout")
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                self._watchdog.record_rest_error(self.config.id, "5xx")
            else:
                self._watchdog.record_rest_error(self.config.id, "error")
            raise
        except TimeoutError:
            self._watchdog.record_rest_error(self.config.id, "timeout")
            raise
        else:
            self._watchdog.record_rest_ok(self.config.id)
            return response.json()

    def _public_get(self, path: str, params: Dict[str, Any] | None = None) -> Dict[str, Any] | list[Any]:
        client = self._ensure_http()
        try:
            maybe_raise_rest_timeout(context="binance_um.public_get")
            response = client.get(path, params=params or {})
            response.raise_for_status()
        except httpx.TimeoutException:
            self._watchdog.record_rest_error(self.config.id, "timeout")
            raise
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                self._watchdog.record_rest_error(self.config.id, "5xx")
            else:
                self._watchdog.record_rest_error(self.config.id, "error")
            raise
        except TimeoutError:
            self._watchdog.record_rest_error(self.config.id, "timeout")
            raise
        else:
            self._watchdog.record_rest_ok(self.config.id)
            return response.json()

    # ------------------------------------------------------------------
    # Public market data

    def server_time(self) -> float:
        if self.safe_mode:
            return self._fallback.server_time()
        payload = self._public_get("/fapi/v1/time")
        return float(payload.get("serverTime", 0))

    def ping(self) -> bool:
        if self.safe_mode:
            return True
        try:
            self._public_get("/fapi/v1/ping")
            return True
        except httpx.HTTPError:
            return False

    def get_filters(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_filters(symbol)
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        data = self._public_get("/fapi/v1/exchangeInfo", params={"symbol": symbol})
        symbols = data.get("symbols", [])
        if not symbols:
            raise RuntimeError(f"symbol {symbol} not found in exchangeInfo")
        entry = symbols[0]
        filters = {flt["filterType"]: flt for flt in entry.get("filters", [])}
        lot = filters.get("LOT_SIZE") or filters.get("MARKET_LOT_SIZE")
        price = filters.get("PRICE_FILTER")
        notional = filters.get("MIN_NOTIONAL", {"notional": 0})
        result = {
            "tick_size": float(price["tickSize"]) if price else 0.0,
            "step_size": float(lot["stepSize"]) if lot else 0.0,
            "min_qty": float(lot["minQty"]) if lot else 0.0,
            "max_qty": float(lot["maxQty"]) if lot else 0.0,
            "min_notional": float(notional.get("notional", 0.0)),
        }
        self._filters_cache[symbol] = result
        return result

    def get_fees(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_fees(symbol)
        if symbol in self._fees_cache:
            return self._fees_cache[symbol]
        data = self._signed_request("GET", "/fapi/v1/commissionRate", {"symbol": symbol})
        result = {
            "maker_bps": float(data["makerCommissionRate"]) * 10_000,
            "taker_bps": float(data["takerCommissionRate"]) * 10_000,
        }
        self._fees_cache[symbol] = result
        return result

    def get_mark_price(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_mark_price(symbol)
        data = self._public_get("/fapi/v1/premiumIndex", params={"symbol": symbol})
        return {"price": float(data["markPrice"]), "ts": float(data["time"])}

    def get_orderbook_top(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_orderbook_top(symbol)
        data = self._public_get("/fapi/v1/depth", params={"symbol": symbol, "limit": 5})
        bids = data.get("bids", [])
        asks = data.get("asks", [])
        if not bids or not asks:
            raise RuntimeError("orderbook empty")
        best_bid = float(bids[0][0])
        best_ask = float(asks[0][0])
        return {"bid": best_bid, "ask": best_ask, "ts": float(data.get("E", 0))}

    def get_symbol_specs(self, symbol: str) -> Dict[str, float]:
        return dict(self.get_filters(symbol))

    def get_funding_info(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_funding_info(symbol)
        data = self._public_get("/fapi/v1/fundingRate", params={"symbol": symbol, "limit": 1})
        if not data:
            return {"rate": 0.0, "next_funding_ts": 0.0}
        entry = data[0]
        return {"rate": float(entry["fundingRate"]), "next_funding_ts": float(entry["fundingTime"])}

    # ------------------------------------------------------------------
    # Account configuration

    def set_position_mode(self, mode: str) -> Dict[str, Any]:
        self.position_mode = mode
        if self.safe_mode:
            return self._fallback.set_position_mode(mode)
        payload = {"dualSidePosition": "true" if mode == "hedge" else "false"}
        return self._signed_request("POST", "/fapi/v1/positionSide/dual", payload)

    def set_margin_type(self, symbol: str, margin_type: str) -> Dict[str, Any]:
        self.margin_type[symbol] = margin_type
        if self.safe_mode:
            return self._fallback.set_margin_type(symbol, margin_type)
        payload = {"symbol": symbol, "marginType": margin_type.upper()}
        return self._signed_request("POST", "/fapi/v1/marginType", payload)

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        self.leverage[symbol] = leverage
        if self.safe_mode:
            return self._fallback.set_leverage(symbol, leverage)
        payload = {"symbol": symbol, "leverage": leverage}
        return self._signed_request("POST", "/fapi/v1/leverage", payload)

    # ------------------------------------------------------------------
    # Trading

    def place_order(self, **kwargs: Any) -> Dict[str, Any]:
        apply_order_delay()
        if self.safe_mode:
            return self._fallback.place_order(**kwargs)
        params: Dict[str, Any] = {
            "symbol": kwargs["symbol"],
            "side": kwargs.get("side", "BUY"),
            "type": kwargs.get("type", "MARKET"),
            "quantity": kwargs.get("quantity"),
        }
        position_side = kwargs.get("position_side")
        if not position_side:
            position_side = "LONG" if params["side"].upper() == "BUY" else "SHORT"
        params["positionSide"] = position_side.upper()

        price = kwargs.get("price")
        if price is not None:
            params["price"] = price
        tif = kwargs.get("time_in_force")
        if tif:
            params["timeInForce"] = tif
        if kwargs.get("post_only"):
            params["timeInForce"] = "GTX"
        if kwargs.get("reduce_only") is not None:
            params["reduceOnly"] = "true" if kwargs.get("reduce_only") else "false"
        if kwargs.get("client_order_id"):
            params["newClientOrderId"] = kwargs["client_order_id"]
        return self._signed_request("POST", "/fapi/v1/order", params)

    def cancel_order(self, **kwargs: Any) -> Dict[str, Any]:
        apply_order_delay()
        if self.safe_mode:
            return self._fallback.cancel_order(**kwargs)
        params: Dict[str, Any] = {"symbol": kwargs["symbol"]}
        if "order_id" in kwargs:
            params["orderId"] = kwargs["order_id"]
        if "client_order_id" in kwargs:
            params["origClientOrderId"] = kwargs["client_order_id"]
        return self._signed_request("DELETE", "/fapi/v1/order", params)

    def open_orders(self, symbol: str | None = None) -> list[Dict[str, Any]]:
        if self.safe_mode:
            return self._fallback.open_orders(symbol)
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = self._signed_request("GET", "/fapi/v1/openOrders", params)
        return data  # type: ignore[return-value]

    def recent_fills(self, symbol: str | None = None, since: float | None = None) -> list[Dict[str, Any]]:
        if self.safe_mode:
            return self._fallback.recent_fills(symbol, since)
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        if since:
            params["startTime"] = int(since)
        data = self._signed_request("GET", "/fapi/v1/userTrades", params)
        return data  # type: ignore[return-value]

    def positions(self) -> list[Dict[str, Any]]:
        if self.safe_mode:
            return self._fallback.positions()
        data = self._signed_request("GET", "/fapi/v2/positionRisk", {})
        results: list[Dict[str, Any]] = []
        for entry in data:  # type: ignore[assignment]
            results.append(
                {
                    "symbol": entry.get("symbol"),
                    "position_amt": float(entry.get("positionAmt", 0.0)),
                    "entry_price": float(entry.get("entryPrice", 0.0)),
                    "unrealized_pnl": float(entry.get("unRealizedProfit", 0.0)),
                    "leverage": int(float(entry.get("leverage", 0))),
                    "margin_type": entry.get("marginType"),
                    "position_side": entry.get("positionSide"),
                }
            )
        return results


def create_client(config: DerivVenueConfig, *, safe_mode: bool = True) -> BinanceUMClient:
    return BinanceUMClient(config, safe_mode=safe_mode)


def get_book(symbol: str) -> Dict[str, float]:
    """Fetch top-of-book quotes from Binance UM public endpoint."""

    url = "https://fapi.binance.com/fapi/v1/ticker/bookTicker"
    symbol_upper = symbol.upper()
    try:
        response = requests.get(url, params={"symbol": symbol_upper}, timeout=2.0)
        response.raise_for_status()
        data = response.json()
        bid = float(data.get("bidPrice", 0.0))
        ask = float(data.get("askPrice", 0.0))
        ts = int(data.get("time") or data.get("E") or time.time() * 1000)
        return {"bid": bid, "ask": ask, "ts": ts}
    except RequestError:
        LOGGER.warning(
            "binance UM public ticker request failed; using in-memory fallback",
            extra={"symbol": symbol_upper},
            exc_info=True,
        )
    except (ValueError, TypeError, KeyError):
        LOGGER.warning(
            "binance UM ticker payload invalid; using in-memory fallback",
            extra={"symbol": symbol_upper},
            exc_info=True,
        )
    client = build_in_memory_client("binance_um", [symbol_upper])
    return client.get_orderbook_top(symbol_upper)
