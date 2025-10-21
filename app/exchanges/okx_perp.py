from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, TYPE_CHECKING

import httpx
try:  # pragma: no cover - shim used when requests is unavailable
    import requests
except ImportError:  # pragma: no cover
    class _RequestsShim:
        @staticmethod
        def get(url: str, params: Dict[str, str] | None = None, timeout: float | None = None) -> httpx.Response:
            return httpx.get(url, params=params, timeout=timeout)

    requests = _RequestsShim()

from . import InMemoryDerivClient, build_in_memory_client

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..core.config import DerivVenueConfig


class OKXPerpClient:
    """OKX Perpetual swaps client with SAFE_MODE fallback."""

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

        self._api_key = os.getenv("OKX_API_KEY_TESTNET")
        self._api_secret = os.getenv("OKX_API_SECRET_TESTNET")
        self._passphrase = os.getenv("OKX_API_PASSPHRASE_TESTNET")

        if not safe_mode:
            if not (self._api_key and self._api_secret and self._passphrase):
                raise RuntimeError("OKX credentials missing for testnet access")
            self._client = httpx.Client(base_url=config.routing.rest, timeout=10.0)

    # ------------------------------------------------------------------
    def _ensure_http(self) -> httpx.Client:
        if self.safe_mode:
            raise RuntimeError("HTTP client not available in SAFE_MODE")
        assert self._client is not None
        return self._client

    def _timestamp(self) -> str:
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")

    def _sign(self, timestamp: str, method: str, request_path: str, body: str) -> str:
        if not self._api_secret:
            raise RuntimeError("OKX secret not configured")
        message = f"{timestamp}{method.upper()}{request_path}{body}".encode("utf-8")
        digest = hmac.new(self._api_secret.encode("utf-8"), message, hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        body: Dict[str, Any] | None = None,
    ) -> Dict[str, Any] | list[Any]:
        client = self._ensure_http()
        params = params or {}
        body_json = json.dumps(body or {}) if method.upper() in {"POST", "DELETE"} else ""
        timestamp = self._timestamp()
        query = httpx.QueryParams(params).to_str() if params else ""
        request_path = f"{path}?{query}" if query else path
        signature = self._sign(timestamp, method, request_path, body_json)
        headers = {
            "OK-ACCESS-KEY": self._api_key or "",
            "OK-ACCESS-SIGN": signature,
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": self._passphrase or "",
            "Content-Type": "application/json",
        }
        response = client.request(
            method,
            path,
            params=params or None,
            data=body_json if body_json else None,
            headers=headers,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code") not in ("0", 0, None):
            raise RuntimeError(f"OKX error: {payload}")
        return payload.get("data", payload)

    def _public_get(
        self, path: str, params: Dict[str, Any] | None = None
    ) -> Dict[str, Any] | list[Any]:
        if self.safe_mode:
            raise RuntimeError("public requests not expected in SAFE_MODE")
        client = self._ensure_http()
        response = client.get(path, params=params or {})
        response.raise_for_status()
        payload = response.json()
        return payload.get("data", payload)

    # ------------------------------------------------------------------
    # Market data

    def ping(self) -> bool:
        if self.safe_mode:
            return True
        try:
            self._public_get("/api/v5/public/time")
            return True
        except httpx.HTTPError:
            return False

    def server_time(self) -> float:
        if self.safe_mode:
            return self._fallback.server_time()
        data = self._public_get("/api/v5/public/time")
        ts = data[0]["ts"] if isinstance(data, list) else data.get("ts", "0")
        return float(ts)

    def get_filters(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_filters(symbol)
        if symbol in self._filters_cache:
            return self._filters_cache[symbol]
        data = self._public_get(
            "/api/v5/public/instruments",
            params={"instType": "SWAP", "instId": symbol},
        )
        if not data:
            raise RuntimeError(f"instrument {symbol} not found")
        entry = data[0]
        result = {
            "tick_size": float(entry.get("tickSz", 0)),
            "step_size": float(entry.get("lotSz", 0)),
            "min_qty": float(entry.get("minSz", 0)),
            "max_qty": float(entry.get("maxSz", 0) or 0),
            "min_notional": float(entry.get("minSz", 0)) * float(entry.get("ctVal", 1)),
        }
        self._filters_cache[symbol] = result
        return result

    def get_fees(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_fees(symbol)
        if symbol in self._fees_cache:
            return self._fees_cache[symbol]
        data = self._request(
            "GET",
            "/api/v5/account/trade-fee",
            params={"instType": "SWAP", "instId": symbol},
        )
        entry = data[0]
        result = {
            "maker_bps": abs(float(entry.get("maker", 0))) * 10_000,
            "taker_bps": abs(float(entry.get("taker", 0))) * 10_000,
        }
        self._fees_cache[symbol] = result
        return result

    def get_mark_price(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_mark_price(symbol)
        data = self._public_get("/api/v5/market/ticker", params={"instId": symbol})
        entry = data[0]
        return {"price": float(entry.get("last", 0.0)), "ts": float(entry.get("ts", 0.0))}

    def get_orderbook_top(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_orderbook_top(symbol)
        data = self._public_get("/api/v5/market/books", params={"instId": symbol, "sz": 1})
        entry = data[0]
        bids = entry.get("bids", [])
        asks = entry.get("asks", [])
        if not bids or not asks:
            raise RuntimeError("orderbook empty")
        return {"bid": float(bids[0][0]), "ask": float(asks[0][0]), "ts": float(entry.get("ts", 0.0))}

    def get_funding_info(self, symbol: str) -> Dict[str, float]:
        if self.safe_mode:
            return self._fallback.get_funding_info(symbol)
        data = self._public_get("/api/v5/public/funding-rate", params={"instId": symbol})
        entry = data[0]
        return {
            "rate": float(entry.get("fundingRate", 0.0)),
            "next_funding_ts": float(entry.get("nextFundingTime", 0.0)),
        }

    # ------------------------------------------------------------------
    # Account configuration

    def set_position_mode(self, mode: str) -> Dict[str, Any]:
        self.position_mode = mode
        if self.safe_mode:
            return self._fallback.set_position_mode(mode)
        body = {"posMode": "long_short_mode" if mode == "hedge" else "net_mode"}
        return self._request("POST", "/api/v5/account/set-position-mode", body=body)

    def set_margin_type(self, symbol: str, margin_type: str) -> Dict[str, Any]:
        self.margin_type[symbol] = margin_type
        if self.safe_mode:
            return self._fallback.set_margin_type(symbol, margin_type)
        body = {
            "instId": symbol,
            "mgnMode": margin_type,
            "lever": str(self.leverage.get(symbol, self.config.leverage)),
        }
        return self._request("POST", "/api/v5/account/set-leverage", body=body)

    def set_leverage(self, symbol: str, leverage: int) -> Dict[str, Any]:
        self.leverage[symbol] = leverage
        if self.safe_mode:
            return self._fallback.set_leverage(symbol, leverage)
        body = {
            "instId": symbol,
            "lever": str(leverage),
            "mgnMode": self.margin_type.get(symbol, self.config.margin_type),
        }
        return self._request("POST", "/api/v5/account/set-leverage", body=body)

    # ------------------------------------------------------------------
    # Trading

    def place_order(self, **kwargs: Any) -> Dict[str, Any]:
        if self.safe_mode:
            return self._fallback.place_order(**kwargs)
        side = kwargs.get("side", "buy").lower()
        pos_side = kwargs.get("pos_side")
        if not pos_side:
            pos_side = "long" if side == "buy" else "short"
        td_mode = kwargs.get("td_mode") or self.margin_type.get(kwargs["symbol"], self.config.margin_type)
        ord_type = (kwargs.get("ord_type") or kwargs.get("type", "market")).lower()
        if kwargs.get("post_only"):
            ord_type = "post_only"
        tif = str(kwargs.get("time_in_force", "")).lower()
        if tif == "ioc":
            ord_type = "ioc"
        body = {
            "instId": kwargs["symbol"],
            "tdMode": td_mode,
            "side": side,
            "posSide": pos_side,
            "ordType": ord_type,
            "sz": str(kwargs.get("quantity")),
        }
        if kwargs.get("price") is not None:
            body["px"] = str(kwargs["price"])
        if kwargs.get("reduce_only") is not None:
            body["reduceOnly"] = "true" if kwargs.get("reduce_only") else "false"
        return self._request("POST", "/api/v5/trade/order", body=body)

    def cancel_order(self, **kwargs: Any) -> Dict[str, Any]:
        if self.safe_mode:
            return self._fallback.cancel_order(**kwargs)
        body = {"instId": kwargs["symbol"]}
        if kwargs.get("order_id"):
            body["ordId"] = kwargs["order_id"]
        if kwargs.get("client_order_id"):
            body["clOrdId"] = kwargs["client_order_id"]
        return self._request("POST", "/api/v5/trade/cancel-order", body=body)

    def open_orders(self, symbol: str | None = None) -> list[Dict[str, Any]]:
        if self.safe_mode:
            return self._fallback.open_orders(symbol)
        params = {"instType": "SWAP"}
        if symbol:
            params["instId"] = symbol
        data = self._request("GET", "/api/v5/trade/orders-pending", params=params)
        return data  # type: ignore[return-value]

    def positions(self) -> list[Dict[str, Any]]:
        if self.safe_mode:
            return self._fallback.positions()
        params = {"instType": "SWAP"}
        data = self._request("GET", "/api/v5/account/positions", params=params)
        results: list[Dict[str, Any]] = []
        for entry in data:  # type: ignore[assignment]
            results.append(
                {
                    "instId": entry.get("instId"),
                    "pos": float(entry.get("pos", 0.0)),
                    "avgPx": float(entry.get("avgPx", 0.0)),
                    "upl": float(entry.get("upl", 0.0)),
                    "lever": float(entry.get("lever", 0.0)),
                    "posSide": entry.get("posSide"),
                    "mgnMode": entry.get("mgnMode"),
                }
            )
        return results


def create_client(config: DerivVenueConfig, *, safe_mode: bool = True) -> OKXPerpClient:
    return OKXPerpClient(config, safe_mode=safe_mode)


_SYMBOL_MAP = {
    "BTCUSDT": "BTC-USDT-SWAP",
    "ETHUSDT": "ETH-USDT-SWAP",
}


def get_book(symbol: str) -> Dict[str, float]:
    """Fetch best bid/ask for supported instruments from OKX public API."""

    inst_id = _SYMBOL_MAP.get(symbol.upper())
    if not inst_id:
        raise ValueError(f"unsupported symbol {symbol}")
    url = "https://www.okx.com/api/v5/market/ticker"
    response = requests.get(url, params={"instId": inst_id}, timeout=2.0)
    response.raise_for_status()
    payload = response.json()
    data = payload.get("data") or []
    if not data:
        raise RuntimeError("empty ticker data")
    entry = data[0]
    bid = float(entry.get("bidPx") or entry.get("bidPrice") or 0.0)
    ask = float(entry.get("askPx") or entry.get("askPrice") or 0.0)
    ts_raw = entry.get("ts") or entry.get("tsPx") or payload.get("ts")
    ts = int(ts_raw) if ts_raw is not None else 0
    return {"bid": bid, "ask": ask, "ts": ts}
