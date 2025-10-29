from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from app.capital_manager import CapitalManager, reset_capital_manager


def test_capital_manager_allocation(tmp_path) -> None:
    state_path = tmp_path / "capital.json"
    manager = CapitalManager(
        state_path=state_path,
        initial_state={
            "total_capital_usdt": 100_000,
            "per_strategy_limits": {
                "cross_exchange_arb": {"max_notional": 50_000},
            },
            "current_usage": {
                "cross_exchange_arb": {"open_notional": 10_000},
            },
        },
    )
    assert manager.can_allocate("cross_exchange_arb", 20_000)
    assert not manager.can_allocate("cross_exchange_arb", 45_000)

    manager.register_fill("cross_exchange_arb", 5_000)
    snapshot = manager.snapshot()
    assert snapshot["current_usage"]["cross_exchange_arb"]["open_notional"] == 15_000

    manager.release("cross_exchange_arb", 10_000)
    snapshot = manager.snapshot()
    assert snapshot["current_usage"]["cross_exchange_arb"]["open_notional"] == 5_000

    manager.release("cross_exchange_arb", 10_000)
    snapshot = manager.snapshot()
    assert snapshot["current_usage"]["cross_exchange_arb"]["open_notional"] == 0.0


def test_capital_snapshot_endpoint(client: TestClient, monkeypatch) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret-token")
    state_path = Path(os.environ["CAPITAL_STATE_PATH"])
    state_path.unlink(missing_ok=True)
    reset_capital_manager(
        CapitalManager(
            state_path=state_path,
            initial_state={
                "total_capital_usdt": 200_000,
                "per_strategy_limits": {
                    "cross_exchange_arb": {"max_notional": 50_000},
                    "funding_carry": {"max_notional": None},
                },
                "current_usage": {
                    "cross_exchange_arb": {"open_notional": 20_000},
                },
            }
        )
    )

    response = client.get(
        "/api/ui/capital",
        headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert "headroom" in payload
    cross_headroom = payload["headroom"].get("cross_exchange_arb")
    assert cross_headroom is not None
    assert cross_headroom["headroom_notional"] == 30_000
    assert payload["headroom"].get("funding_carry", {}).get("headroom_notional") is None
