"""OKX USDT-margined perpetual futures client."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict

import requests

from .base import FuturesExchangeClient


_DEFAULT_TIMEOUT = 10.0


def _iso_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class OKXFuturesClient(FuturesExchangeClient):
    """OKX REST client for USDT perpetual swaps."""

    api_key: str | None
    api_secret: str | None
    passphrase: str | None
    api_url: str

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        passphrase: str | None = None,
        api_url: str | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("OKX_API_KEY")
        self.api_secret = api_secret or os.getenv("OKX_API_SECRET")
        self.passphrase = passphrase or os.getenv("OKX_API_PASSPHRASE")
        self.api_url = api_url or os.getenv("OKX_FUTURES_API_URL", "https://www.okx.com")
        self._session = session or requests.Session()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _require_credentials(self) -> None:
        if not self.api_key or not self.api_secret or not self.passphrase:
            raise RuntimeError("OKX credentials missing")

    def _sign(self, timestamp: str, method: str, path: str, body: str) -> str:
        message = f"{timestamp}{method.upper()}{path}{body}".encode("utf-8")
        signature = hmac.new(
            (self.api_secret or "").encode("utf-8"),
            message,
            hashlib.sha256,
        ).digest()
        return base64.b64encode(signature).decode("utf-8")

    def _headers(self, method: str, path: str, body: str, signed: bool) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if not signed:
            return headers
        self._require_credentials()
        timestamp = _iso_timestamp()
        headers.update(
            {
                "OK-ACCESS-KEY": str(self.api_key),
                "OK-ACCESS-PASSPHRASE": str(self.passphrase),
                "OK-ACCESS-TIMESTAMP": timestamp,
                "OK-ACCESS-SIGN": self._sign(timestamp, method, path, body),
            }
        )
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Dict[str, Any] | None = None,
        body: Dict[str, Any] | None = None,
        signed: bool = False,
    ) -> Any:
        query = ""
        if params:
            query = "?" + "&".join(
                f"{key}={value}" for key, value in params.items() if value is not None
            )
        full_path = f"{path}{query}"
        url = f"{self.api_url}{full_path}"
        body_payload = json.dumps(body or {}) if body is not None else ""
        headers = self._headers(method, path + query, body_payload, signed)
        try:
            response = self._session.request(
                method.upper(),
                url,
                headers=headers,
                data=body_payload if method.upper() in {"POST", "PUT"} else None,
                timeout=_DEFAULT_TIMEOUT,
            )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - defensive
            raise RuntimeError(f"OKX request failed: {exc}") from exc
        payload = response.json()
        if isinstance(payload, dict) and payload.get("code") not in (None, "0", 0):
            message = payload.get("msg") or payload.get("message") or "unknown error"
            raise RuntimeError(f"OKX error: {message}")
        return payload.get("data") if isinstance(payload, dict) else payload

    def _instrument_id(self, symbol: str) -> str:
        symbol_upper = str(symbol).upper()
        if symbol_upper.endswith("USDT"):
            base = symbol_upper[:-4]
            quote = "USDT"
        else:
            raise ValueError("symbol must end with USDT for OKX swaps")
        return f"{base}-{quote}-SWAP"

    def _format_size(self, notional_usdt: float, price: float) -> str:
        if price <= 0:
            raise ValueError("price must be positive to compute size")
        size = Decimal(str(notional_usdt)) / Decimal(str(price))
        # OKX swaps accept up to 6 decimals for sz.
        size_normalised = size.quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if size_normalised <= 0:
            raise ValueError("computed order size is zero")
        return format(size_normalised.normalize(), "f")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_mark_price(self, symbol: str) -> Dict[str, Any]:
        inst_id = self._instrument_id(symbol)
        data = self._request(
            "GET",
            "/api/v5/public/mark-price",
            params={"instType": "SWAP", "instId": inst_id},
        )
        entry = data[0] if isinstance(data, list) and data else {}
        mark_price = float(entry.get("markPx", 0.0) or 0.0)
        return {"symbol": symbol.upper(), "inst_id": inst_id, "mark_price": mark_price, "raw": entry}

    def get_position(self, symbol: str) -> Dict[str, Any]:
        inst_id = self._instrument_id(symbol)
        data = self._request(
            "GET",
            "/api/v5/account/positions",
            params={"instType": "SWAP", "instId": inst_id},
            signed=True,
        )
        entry = data[0] if isinstance(data, list) and data else {}
        pos = float(entry.get("pos", 0.0) or 0.0)
        side = entry.get("posSide") or ("long" if pos > 0 else "short" if pos < 0 else "flat")
        return {
            "symbol": symbol.upper(),
            "inst_id": inst_id,
            "size": abs(pos),
            "side": side,
            "entry_price": float(entry.get("avgPx", 0.0) or 0.0),
            "leverage": float(entry.get("lever", 0.0) or 0.0),
            "raw": entry,
        }

    def place_order(self, symbol: str, side: str, notional_usdt: float, leverage: float) -> Dict[str, Any]:
        inst_id = self._instrument_id(symbol)
        side_lower = str(side).lower()
        if side_lower not in {"long", "short", "buy", "sell"}:
            raise ValueError("side must be long/short or buy/sell")
        trade_side = "buy" if side_lower in {"long", "buy"} else "sell"
        pos_side = "long" if trade_side == "buy" else "short"
        mark = self.get_mark_price(symbol)
        mark_price = float(mark.get("mark_price") or 0.0) or 1.0
        size = self._format_size(notional_usdt, mark_price)

        self._request(
            "POST",
            "/api/v5/account/set-leverage",
            body={
                "instId": inst_id,
                "lever": str(leverage),
                "mgnMode": "cross",
                "posSide": pos_side,
            },
            signed=True,
        )

        order_body = {
            "instId": inst_id,
            "tdMode": "cross",
            "side": trade_side,
            "ordType": "market",
            "posSide": pos_side,
            "sz": size,
        }
        order_data = self._request(
            "POST",
            "/api/v5/trade/order",
            body=order_body,
            signed=True,
        )
        order_entry = order_data[0] if isinstance(order_data, list) and order_data else {}
        avg_price = float(order_entry.get("avgPx", mark_price) or mark_price)
        filled_size = float(order_entry.get("fillSz", size) or size)
        status = order_entry.get("state") or "filled"
        return {
            "exchange": "okx",
            "symbol": symbol.upper(),
            "inst_id": inst_id,
            "order_id": order_entry.get("ordId"),
            "status": str(status).lower(),
            "side": "long" if trade_side == "buy" else "short",
            "avg_price": avg_price,
            "filled_qty": filled_size,
            "notional_usdt": float(notional_usdt),
            "leverage": float(leverage),
            "raw": order_entry,
        }

    def cancel_all(self, symbol: str) -> Dict[str, Any]:
        inst_id = self._instrument_id(symbol)
        payload = self._request(
            "POST",
            "/api/v5/trade/cancel-all",
            body={"instType": "SWAP", "instId": inst_id},
            signed=True,
        )
        return {"exchange": "okx", "symbol": symbol.upper(), "inst_id": inst_id, "raw": payload}

    def get_account_limits(self) -> Dict[str, Any]:
        data = self._request("GET", "/api/v5/account/balance", signed=True)
        entry = data[0] if isinstance(data, list) and data else {}
        details = entry.get("details") if isinstance(entry, dict) else None
        available = 0.0
        total = float(entry.get("totalEq", 0.0) or 0.0)
        if isinstance(details, list):
            for item in details:
                if str(item.get("ccy")) == "USDT":
                    available = float(item.get("availBal", 0.0) or 0.0)
                    break
        return {
            "asset": "USDT",
            "available_balance": available,
            "total_equity": total,
            "raw": entry,
        }
