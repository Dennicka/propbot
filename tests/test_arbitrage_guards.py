from app.exchanges import binance_um, okx_perp
from app.services import arbitrage
from app.services.runtime import get_state


def _mock_books(monkeypatch) -> None:
    monkeypatch.setattr(
        binance_um,
        "get_book",
        lambda symbol: {"bid": 20140.0, "ask": 20150.0, "ts": 1},
    )
    monkeypatch.setattr(
        okx_perp,
        "get_book",
        lambda symbol: {"bid": 20180.0, "ask": 20190.0, "ts": 1},
    )


def test_execute_guarded_by_safe_mode(client, monkeypatch) -> None:
    _mock_books(monkeypatch)
    plan = arbitrage.build_plan("BTCUSDT", 50, 2)
    response = client.post("/api/arb/execute", json=plan.as_dict())
    assert response.status_code == 403
    assert response.json()["detail"] == "SAFE_MODE blocks execution"


def test_execute_simulated_under_dry_run(client, monkeypatch) -> None:
    _mock_books(monkeypatch)
    state = get_state()
    state.control.safe_mode = False
    state.control.dry_run = True
    plan = arbitrage.build_plan("BTCUSDT", 50, 2)
    assert plan.viable is True
    response = client.post("/api/arb/execute", json=plan.as_dict())
    assert response.status_code == 200
    payload = response.json()
    assert payload["simulated"] is True
    assert payload["safe_mode"] is False
    assert payload["dry_run"] is True
