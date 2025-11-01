import pytest

from app.services import partial_hedge_runner
from app.services.partial_hedge_runner import reset_state_for_tests


@pytest.fixture(autouse=True)
def _reset_partial_state():
    reset_state_for_tests()


def test_get_partial_hedge_plan_endpoint(client, monkeypatch):
    async def fake_refresh():
        return {
            "status": "planned",
            "orders": [{"venue": "binance", "symbol": "BTCUSDT", "side": "SELL", "qty": 1.0, "reason": "test"}],
            "plan": {
                "generated_ts": "2024-01-01T00:00:00Z",
                "totals": {"orders": 1, "notional_usdt": 1_000.0},
                "orders": [{"venue": "binance", "symbol": "BTCUSDT", "side": "SELL", "qty": 1.0, "notional_usdt": 1_000.0, "reason": "test"}],
            },
        }

    monkeypatch.setattr(partial_hedge_runner, "refresh_plan", fake_refresh)
    monkeypatch.setattr(partial_hedge_runner, "get_partial_hedge_status", lambda: {
        "enabled": True,
        "dry_run": True,
        "totals": {"orders": 1, "notional_usdt": 1_000.0},
    })
    monkeypatch.setattr(
        "app.routers.ui_partial_hedge.refresh_plan",
        fake_refresh,
    )
    monkeypatch.setattr(
        "app.routers.ui_partial_hedge.get_partial_hedge_status",
        lambda: {"enabled": True, "dry_run": True, "totals": {"orders": 1, "notional_usdt": 1_000.0}},
    )

    response = client.get("/api/ui/hedge/plan")
    assert response.status_code == 200
    payload = response.json()
    assert payload["orders"][0]["venue"] == "binance"
    assert payload["totals"]["orders"] == 1


def test_execute_partial_hedge_requires_confirmation(client):
    response = client.post("/api/ui/hedge/execute", json={"confirm": False})
    assert response.status_code == 400


def test_execute_partial_hedge_endpoint(client, monkeypatch):
    async def fake_execute(confirm: bool):
        assert confirm is True
        return {"status": "executed"}

    monkeypatch.setattr(partial_hedge_runner, "execute_now", fake_execute)
    monkeypatch.setattr(partial_hedge_runner, "get_partial_hedge_status", lambda: {
        "enabled": True,
        "dry_run": False,
        "totals": {"orders": 1, "notional_usdt": 1_000.0},
    })
    monkeypatch.setattr("app.routers.ui_partial_hedge.execute_now", fake_execute)
    monkeypatch.setattr(
        "app.routers.ui_partial_hedge.get_partial_hedge_status",
        lambda: {"enabled": True, "dry_run": False, "totals": {"orders": 1, "notional_usdt": 1_000.0}},
    )

    response = client.post("/api/ui/hedge/execute", json={"confirm": True})
    assert response.status_code == 200
    payload = response.json()
    assert payload["execution"]["status"] == "executed"
