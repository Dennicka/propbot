from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Iterable

import pytest

from tests.acceptance.conftest import RouterFlowResult, load_golden_fixture


def _assert_metrics(metrics_path: Path, expected_lines: Iterable[str]) -> None:
    content = metrics_path.read_text(encoding="utf-8") if metrics_path.exists() else ""
    lines = content.splitlines()
    for needle in expected_lines:
        assert any(line.startswith(needle) for line in lines), f"missing metric prefix: {needle}"


def test_happy_fill_matches_golden(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FF_PRETRADE_STRICT", "1")
    monkeypatch.setenv("FF_RISK_LIMITS", "0")
    monkeypatch.setenv("SAFE_MODE", "0")

    intent = make_intent(qty=Decimal("2"), price=Decimal("25000"))
    result: RouterFlowResult = run_router_flow(
        intent,
        events=[
            "ACK",
            {"event": "PARTIALLY_FILLED", "quantity": Decimal("1.0")},
            "FILLED",
        ],
    )

    golden = load_golden_fixture("happy_fill.json")
    assert result.final_state() == golden["final_state"]
    assert result.blocked_reason == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])


def test_reject_matches_golden(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FF_PRETRADE_STRICT", "1")
    monkeypatch.setenv("FF_RISK_LIMITS", "0")

    intent = make_intent(side="sell", qty=Decimal("1"))
    result: RouterFlowResult = run_router_flow(intent, events=["REJECTED"])

    golden = load_golden_fixture("reject.json")
    assert result.final_state() == golden["final_state"]
    assert result.blocked_reason == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])


def test_cancel_matches_golden(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FF_PRETRADE_STRICT", "1")
    monkeypatch.setenv("FF_RISK_LIMITS", "0")

    intent = make_intent(side="buy", qty=Decimal("1.5"))
    result: RouterFlowResult = run_router_flow(intent, events=["ACK", "CANCELED"])

    golden = load_golden_fixture("cancel.json")
    assert result.final_state() == golden["final_state"]
    assert result.blocked_reason == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])
