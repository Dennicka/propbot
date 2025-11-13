from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Iterable

import pytest

from app.market.watchdog import watchdog
from app.services.safe_mode import SafeMode

from tests.acceptance.conftest import RouterFlowResult, load_golden_fixture


def _assert_metrics(metrics_path: Path, expected_lines: Iterable[str]) -> None:
    content = metrics_path.read_text(encoding="utf-8") if metrics_path.exists() else ""
    lines = content.splitlines()
    for needle in expected_lines:
        assert any(line.startswith(needle) for line in lines), f"missing metric prefix: {needle}"


def test_safe_mode_block(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SAFE_MODE", "1")
    SafeMode.set(True)

    intent = make_intent()
    result: RouterFlowResult = run_router_flow(intent)

    golden = load_golden_fixture("guardrails.json")["safe_mode"]
    assert result.final_state() == golden["final_state"]
    assert result.blocked_reason == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])


def test_dupe_intent_block_within_window(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    frozen_time,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IDEMPOTENCY_WINDOW_SEC", "5")

    intent = make_intent(strategy="dupe-test", qty=Decimal("1.0"))
    first: RouterFlowResult = run_router_flow(intent, events=["ACK"])

    frozen_time.tock(1.0)
    retry_intent = dict(intent)
    retry_intent["nonce"] = 2
    second: RouterFlowResult = run_router_flow(retry_intent, router=first.router)

    golden = load_golden_fixture("guardrails.json")["dupe_intent"]
    assert second.final_state() == golden["final_state"]
    assert second.blocked_reason == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])


def test_cooldown_block_after_reject(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    frozen_time,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FF_ROUTER_COOLDOWN", "1")
    monkeypatch.setenv("ROUTER_COOLDOWN_REASON_MAP", '{"rate_limit": 8}')

    intent = make_intent(strategy="cooldown", qty=Decimal("1"))
    first: RouterFlowResult = run_router_flow(intent, events=["REJECTED"])

    key = first.router._cooldown_key(intent["venue"], intent["symbol"], intent["strategy"])
    first.router._cooldown_registry.hit(key, seconds=8.0, reason="rate_limit")

    frozen_time.tock(1.0)
    retry_intent = dict(intent)
    retry_intent["nonce"] = 2
    second: RouterFlowResult = run_router_flow(retry_intent, router=first.router)

    golden = load_golden_fixture("guardrails.json")["cooldown"]
    assert second.final_state() == golden["final_state"]
    assert second.blocked_reason == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])


def test_live_guard_blocks_without_confirm(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXEC_PROFILE", "live")

    intent = make_intent(strategy="live-guard")
    result: RouterFlowResult = run_router_flow(intent)

    golden = load_golden_fixture("guardrails.json")["live_guard"]
    assert result.final_state() == golden["final_state"]
    assert result.blocked_reason == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])


def test_stale_p95_gate_blocks(
    isolate_env: Path,
    run_router_flow,
    make_intent,
    frozen_time,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FF_MD_WATCHDOG", "1")
    monkeypatch.setenv("STALE_P95_LIMIT_MS", "100")

    venue = "binance"
    symbol = "BTCUSDT"
    watchdog.beat(venue, symbol, ts=frozen_time.time() - 10.0)
    for _ in range(5):
        watchdog.note_staleness(venue, stale_ms=5000, now=frozen_time.time())

    intent = make_intent(venue=venue, symbol=symbol, strategy="stale")
    result: RouterFlowResult = run_router_flow(intent)

    golden = load_golden_fixture("guardrails.json")["stale_p95"]
    assert result.final_state() == golden["final_state"]
    assert result.blocked_reason == golden["blocked_reason"]
    _assert_metrics(isolate_env, golden["metrics_contains"])
