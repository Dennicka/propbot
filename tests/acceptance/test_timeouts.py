from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Iterable

import pytest

from app.orders.state import OrderState

from tests.acceptance.conftest import RouterFlowResult, load_golden_fixture


def _assert_metrics(metrics_path: Path, expected_lines: Iterable[str]) -> None:
    content = metrics_path.read_text(encoding="utf-8") if metrics_path.exists() else ""
    lines = content.splitlines()
    for needle in expected_lines:
        assert any(line.startswith(needle) for line in lines), f"missing metric prefix: {needle}"


def _state_name(state: object) -> str | None:
    if isinstance(state, OrderState):
        return state.value
    if isinstance(state, str):
        text = state.strip().upper()
        return text or None
    return None


def test_ack_timeout(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    frozen_time,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FF_ORDER_TIMEOUTS", "1")
    monkeypatch.setenv("SUBMIT_ACK_TIMEOUT_SEC", "1")
    monkeypatch.setenv("FILL_TIMEOUT_SEC", "5")

    intent = make_intent(strategy="ack-timeout")
    result: RouterFlowResult = run_router_flow(intent)
    assert result.order_id is not None

    frozen_time.tock(2.0)
    result.router._run_order_timeouts(now=frozen_time.time())

    snapshot = result.router.get_order_snapshot(result.order_id)
    final_state = _state_name(snapshot.get("state"))

    golden = load_golden_fixture("timeouts.json")["ack_timeout"]
    assert final_state == golden["final_state"]
    assert "ack-timeout" == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])


def test_fill_timeout(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    frozen_time,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FF_ORDER_TIMEOUTS", "1")
    monkeypatch.setenv("SUBMIT_ACK_TIMEOUT_SEC", "5")
    monkeypatch.setenv("FILL_TIMEOUT_SEC", "1")

    intent = make_intent(strategy="fill-timeout", qty=Decimal("2"))
    result: RouterFlowResult = run_router_flow(
        intent,
        events=[
            "ACK",
            {"event": "PARTIALLY_FILLED", "quantity": Decimal("1")},
        ],
    )
    assert result.order_id is not None

    frozen_time.tock(2.0)
    result.router._run_order_timeouts(now=frozen_time.time())

    snapshot = result.router.get_order_snapshot(result.order_id)
    final_state = _state_name(snapshot.get("state"))

    golden = load_golden_fixture("timeouts.json")["fill_timeout"]
    assert final_state == golden["final_state"]
    assert "fill-timeout" == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])
