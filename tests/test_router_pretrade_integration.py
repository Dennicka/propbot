from __future__ import annotations

from typing import Any

import pytest

from app.config.schema import GuardrailsConfig, MaintenanceConfig, PretradeConfig
from app.persistence import order_store
from app.services import runtime


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    runtime.reset_for_tests()
    yield


def _set_pretrade_config(*, allow_autofix: bool) -> None:
    state = runtime.get_state()
    state.control.environment = "paper"
    state.config.data = state.config.data.model_copy(
        update={
            "pretrade": PretradeConfig(allow_autofix=allow_autofix, default_tz="UTC"),
            "maintenance": MaintenanceConfig(windows=[]),
            "guardrails": GuardrailsConfig(testnet_block_highrisk=False, blocklist=[]),
        }
    )


def test_router_returns_422_with_reason_on_invalid_pretrade(client):
    _set_pretrade_config(allow_autofix=False)
    payload: dict[str, Any] = {
        "account": "acct",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": 0.15,
        "price": 101.37,
        "type": "LIMIT",
    }

    response = client.post("/api/ui/execution/orders", json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PRETRADE_INVALID"
    assert detail["reason"] == "price_tick"


def test_router_passes_fixed_order_when_autofix_enabled(client):
    _set_pretrade_config(allow_autofix=True)
    payload: dict[str, Any] = {
        "account": "acct",
        "venue": "binance",
        "symbol": "BTCUSDT",
        "side": "buy",
        "qty": 2.2604,
        "price": 101.37,
        "type": "LIMIT",
    }

    response = client.post("/api/ui/execution/orders", json=payload)
    assert response.status_code == 200
    intent_id = response.json()["intent_id"]

    with order_store.session_scope() as session:
        record = order_store.load_intent(session, intent_id)
    assert record is not None
    assert pytest.approx(record.qty) == 2.26
    assert pytest.approx(record.price) == 101.3
