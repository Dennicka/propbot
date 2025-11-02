from __future__ import annotations

from typing import Any

import pytest

from datetime import datetime, timezone

from app import ledger
from app.config.schema import (
    ExposureCapsConfig,
    ExposureCapsEntry,
    ExposureSideCapsConfig,
    GuardrailsConfig,
    MaintenanceConfig,
    PretradeConfig,
)
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


def test_pretrade_blocks_on_exposure_caps(client):
    ledger.reset()
    _set_pretrade_config(allow_autofix=True)
    state = runtime.get_state()
    state.config.data = state.config.data.model_copy(
        update={
            "exposure_caps": ExposureCapsConfig(
                default=ExposureCapsEntry(
                    max_abs_usdt=2000,
                    per_side_max_abs_usdt=ExposureSideCapsConfig(LONG=1500, SHORT=1500),
                ),
                per_symbol={},
                per_venue={
                    "okx": {"ETHUSDT": ExposureCapsEntry(max_abs_usdt=1200)},
                },
            )
        }
    )
    ts = datetime.now(timezone.utc).isoformat()
    order_id = ledger.record_order(
        venue="okx",
        symbol="ETHUSDT",
        side="buy",
        qty=1.0,
        price=1900.0,
        status="filled",
        client_ts=ts,
        exchange_ts=ts,
        idemp_key="exposure-seed",
    )
    ledger.record_fill(
        order_id=order_id,
        venue="okx",
        symbol="ETHUSDT",
        side="buy",
        qty=1.0,
        price=1900.0,
        fee=0.0,
        ts=ts,
    )

    payload = {
        "account": "acct",
        "venue": "okx",
        "symbol": "ETHUSDT",
        "side": "buy",
        "qty": 0.2,
        "price": 1000.0,
        "type": "LIMIT",
    }

    response = client.post("/api/ui/execution/orders", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "PRETRADE_INVALID"
    assert detail["reason"] == "EXPOSURE_CAPS::GLOBAL"

    reduce_payload = {
        "account": "acct",
        "venue": "okx",
        "symbol": "ETHUSDT",
        "side": "sell",
        "qty": 0.5,
        "price": 1900.0,
        "type": "LIMIT",
    }

    reduce_response = client.post("/api/ui/execution/orders", json=reduce_payload)
    assert reduce_response.status_code == 200
