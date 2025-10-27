import importlib
import json

import pytest
from fastapi.testclient import TestClient


class _StubClient:
    def __init__(self, venue: str, mark_price: float) -> None:
        self.venue = venue
        self.mark_price = mark_price
        self.placed_orders: list[dict] = []

    def get_mark_price(self, symbol: str) -> dict:
        return {"symbol": symbol, "mark_price": float(self.mark_price)}

    def place_order(self, symbol: str, side: str, notional_usdt: float, leverage: float) -> dict:
        price = float(self.mark_price)
        qty = float(notional_usdt) / price if price else 0.0
        order = {
            "exchange": self.venue,
            "symbol": symbol,
            "side": side,
            "avg_price": price,
            "filled_qty": qty,
            "status": "filled",
            "order_id": f"{self.venue}-order",
            "notional_usdt": float(notional_usdt),
            "leverage": float(leverage),
        }
        self.placed_orders.append(order)
        return order


def test_partial_position_persisted_after_hold(monkeypatch, tmp_path):
    store_path = tmp_path / "hedge_positions.json"
    runtime_path = tmp_path / "runtime_state.json"
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(store_path))
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(runtime_path))
    monkeypatch.setenv("AUTH_ENABLED", "false")

    runtime_module = importlib.import_module("app.services.runtime")
    hold_error_cls = runtime_module.HoldActiveError
    runtime_mod = importlib.reload(runtime_module)
    runtime_mod.HoldActiveError = hold_error_cls
    positions_mod = importlib.reload(importlib.import_module("positions"))
    cross_mod = importlib.reload(importlib.import_module("services.cross_exchange_arb"))

    binance = _StubClient("binance", 100.0)
    okx = _StubClient("okx", 105.0)
    monkeypatch.setattr(
        cross_mod,
        "_clients",
        cross_mod._ExchangeClients(binance=binance, okx=okx),
    )
    monkeypatch.setattr(cross_mod, "is_dry_run_mode", lambda: False)
    monkeypatch.setattr(cross_mod, "append_entry", lambda entry: entry)

    def _fake_register_order_attempt(*_, source: str, **__):
        if source == "cross_exchange_short":
            raise runtime_mod.HoldActiveError("hold_active")

    monkeypatch.setattr(cross_mod, "register_order_attempt", _fake_register_order_attempt)

    result = cross_mod.execute_hedged_trade("BTCUSDT", 1_000.0, 2.0, 1.0)
    assert result["success"] is False
    assert result.get("hold_active") is True

    positions = positions_mod.list_positions()
    assert len(positions) == 1
    partial = positions[0]
    assert partial["status"] == "partial"
    store_leg_status = {leg["side"]: leg["status"] for leg in partial["legs"]}
    assert store_leg_status.get("long") == "partial"
    assert store_leg_status.get("short") in {"missing", "partial"}

    open_positions = positions_mod.list_open_positions()
    assert len(open_positions) == 1
    assert open_positions[0]["id"] == partial["id"]

    with store_path.open("r", encoding="utf-8") as handle:
        persisted = json.load(handle)
    assert persisted and persisted[0]["status"] == "partial"

    main_mod = importlib.reload(importlib.import_module("app.main"))
    client = TestClient(main_mod.app)
    payload = client.get("/api/ui/positions").json()

    api_positions = {entry["id"]: entry for entry in payload["positions"]}
    assert partial["id"] in api_positions
    api_entry = api_positions[partial["id"]]
    assert api_entry["status"] == "partial"
    leg_statuses = {leg["side"]: leg["status"] for leg in api_entry["legs"]}
    assert leg_statuses.get("long") == "partial"
    assert leg_statuses.get("short") in {"missing", "partial"}

    exposure = payload["exposure"]
    assert pytest.approx(exposure["binance"]["long_notional"]) == pytest.approx(1_000.0)
    assert pytest.approx(exposure.get("okx", {}).get("short_notional", 0.0)) == pytest.approx(0.0)
    assert payload["totals"]["unrealized_pnl_usdt"] == pytest.approx(0.0, abs=1e-6)

    runtime_mod = importlib.reload(importlib.import_module("app.services.runtime"))
    runtime_mod.HoldActiveError = hold_error_cls
    positions_mod = importlib.reload(importlib.import_module("positions"))
    main_mod = importlib.reload(importlib.import_module("app.main"))
    client = TestClient(main_mod.app)
    payload = client.get("/api/ui/positions").json()
    ids = {entry["id"] for entry in payload["positions"]}
    assert partial["id"] in ids
    refreshed_entry = next(entry for entry in payload["positions"] if entry["id"] == partial["id"])
    assert refreshed_entry["status"] == "partial"
