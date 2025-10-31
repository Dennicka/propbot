"""Binance USDⓈ-M perpetual futures client."""

from __future__ import annotations

import hashlib
import hmac
import os
import time
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict

import requests

from app.utils.chaos import apply_order_delay, maybe_raise_rest_timeout

from app.secrets_store import get_secrets_store

from .base import FuturesExchangeClient


_DEFAULT_TIMEOUT = 10.0
_DEFAULT_RECV_WINDOW = 5000


def _binance_store_credentials() -> tuple[str | None, str | None]:
    try:
        store = get_secrets_store()
    except (FileNotFoundError, ValueError):
        return None, None
    except Exception:
        return None, None
    credentials = store.get_exchange_credentials("binance")
    return credentials.get("key"), credentials.get("secret")


class BinanceFuturesClient(FuturesExchangeClient):
    """Binance REST client for USDⓈ-M perpetual futures."""

    api_key: str | None
    api_secret: str | None
    api_url: str

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        api_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        store_key, store_secret = _binance_store_credentials()
        env_key = None if store_key else os.getenv("BINANCE_API_KEY")
        env_secret = None if store_secret else os.getenv("BINANCE_API_SECRET")

        self.api_key = api_key or store_key or env_key
        self.api_secret = api_secret or store_secret or env_secret
        self.api_url = api_url or os.getenv(
            "BINANCE_FUTURES_API_URL", "https://fapi.binance.com"
        )
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _require_credentials(self) -> None:
        if not self.api_key or not self.api_secret:
            raise RuntimeError("Binance credentials missing")

    def _timestamp(self) -> int:
        return int(time.time() * 1000)

    def _sign(self, params: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(params)
        payload.setdefault("timestamp", self._timestamp())
        payload.setdefault("recvWindow", _DEFAULT_RECV_WINDOW)
        # Binance requires parameters sorted alphabetically when computing the signature.
        encoded = "&".join(
            f"{key}={value}"
            for key, value in sorted(payload.items())
            if value is not None
        )
        signature = hmac.new(
            (self.api_secret or "").encode("utf-8"),
            encoded.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        payload["signature"] = signature
        return payload

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        url = f"{self.api_url}{path}"
        request_params = dict(params or {})
        headers: Dict[str, str] = {}
        if signed:
            self._require_credentials()
            request_params = self._sign(request_params)
            headers["X-MBX-APIKEY"] = str(self.api_key)
        elif self.api_key:
            headers["X-MBX-APIKEY"] = str(self.api_key)

        try:
            maybe_raise_rest_timeout(context="binance_futures.request")
            if method.upper() in {"POST", "PUT"}:
                response = self._session.request(
                    method.upper(),
                    url,
                    data=request_params,
                    headers=headers,
                    timeout=_DEFAULT_TIMEOUT,
                )
            else:
                response = self._session.request(
                    method.upper(),
                    url,
                    params=request_params,
                    headers=headers,
                    timeout=_DEFAULT_TIMEOUT,
                )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"Binance request failed: {exc}") from exc

        payload = response.json()
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            message = payload.get("msg") or payload.get("message") or "unknown error"
            raise RuntimeError(f"Binance error: {message}")
        return payload

    def _normalise_symbol(self, symbol: str) -> str:
        return str(symbol).upper()

    def _format_quantity(self, symbol: str, notional_usdt: float, price: float) -> str:
        if price <= 0:
            raise ValueError("price must be positive to compute quantity")
        qty = Decimal(str(notional_usdt)) / Decimal(str(price))
        normalised = qty.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if normalised <= 0:
            raise ValueError("computed quantity is zero")
        return format(normalised.normalize(), "f")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_mark_price(self, symbol: str) -> Dict[str, Any]:
        normalized = self._normalise_symbol(symbol)
        payload = self._request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": normalized},
        )
        mark_price = 0.0
        if isinstance(payload, dict):
            mark_price = float(payload.get("markPrice", 0.0) or 0.0)
        return {"symbol": normalized, "mark_price": mark_price, "raw": payload}

    def get_position(self, symbol: str) -> Dict[str, Any]:
        normalized = self._normalise_symbol(symbol)
        payload = self._request(
            "GET",
            "/fapi/v2/positionRisk",
            params={"symbol": normalized},
            signed=True,
        )
        entry: Dict[str, Any] | None = None
        if isinstance(payload, list) and payload:
            entry = payload[0]
        elif isinstance(payload, dict):
            entry = payload
        if not entry:
            return {
                "symbol": normalized,
                "size": 0.0,
                "side": "flat",
                "entry_price": 0.0,
                "leverage": 0.0,
                "raw": payload,
            }
        position_amt = float(entry.get("positionAmt", 0.0) or 0.0)
        side = "long" if position_amt > 0 else "short" if position_amt < 0 else "flat"
        return {
            "symbol": normalized,
            "size": abs(position_amt),
            "side": side,
            "entry_price": float(entry.get("entryPrice", 0.0) or 0.0),
            "leverage": float(entry.get("leverage", 0.0) or 0.0),
            "raw": entry,
        }

    def place_order(self, symbol: str, side: str, notional_usdt: float, leverage: float) -> Dict[str, Any]:
        apply_order_delay()
        normalized = self._normalise_symbol(symbol)
        side_lower = str(side).lower()
        if side_lower not in {"long", "short", "buy", "sell"}:
            raise ValueError("side must be long/short or buy/sell")
        binance_side = "BUY" if side_lower in {"long", "buy"} else "SELL"
        mark = self.get_mark_price(normalized)
        mark_price = float(mark.get("mark_price") or 0.0) or 1.0
        quantity = self._format_quantity(normalized, notional_usdt, mark_price)

        self._request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": normalized, "leverage": int(float(leverage))},
            signed=True,
        )

        order_response = self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": normalized,
                "side": binance_side,
                "type": "MARKET",
                "quantity": quantity,
                "newOrderRespType": "RESULT",
            },
            signed=True,
        )
        avg_price = float(
            order_response.get("avgPrice")
            or order_response.get("price")
            or mark_price
            or 0.0
        )
        filled_qty = float(order_response.get("executedQty") or quantity)
        status = str(order_response.get("status") or "FILLED").lower()
        return {
            "exchange": "binance",
            "symbol": normalized,
            "order_id": order_response.get("orderId"),
            "status": status,
            "side": "long" if binance_side == "BUY" else "short",
            "avg_price": avg_price,
            "filled_qty": filled_qty,
            "notional_usdt": float(notional_usdt),
            "leverage": float(leverage),
            "raw": order_response,
        }

    def cancel_all(self, symbol: str) -> Dict[str, Any]:
        normalized = self._normalise_symbol(symbol)
        apply_order_delay()
        payload = self._request(
            "DELETE",
            "/fapi/v1/allOpenOrders",
            params={"symbol": normalized},
            signed=True,
        )
        return {"exchange": "binance", "symbol": normalized, "raw": payload}

    def get_account_limits(self) -> Dict[str, Any]:
        payload = self._request("GET", "/fapi/v2/balance", signed=True)
        available = 0.0
        wallet = 0.0
        asset = "USDT"
        if isinstance(payload, list):
            for entry in payload:
                if str(entry.get("asset")) == asset:
                    available = float(entry.get("availableBalance", 0.0) or 0.0)
                    wallet = float(entry.get("balance", 0.0) or 0.0)
                    break
        return {
            "asset": asset,
            "available_balance": available,
            "total_balance": wallet,
            "raw": payload,
        }
