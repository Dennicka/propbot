from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from typing import Dict, List

import pytest
import time

from app.router.smart_router import SmartRouter
from app.sor.select import Quote
import app.router.smart_router as smart_router_module


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> SmartRouter:
    monkeypatch.setenv("TEST_ONLY_ROUTER_META", "*")
    monkeypatch.setenv("FF_SOR_V1", "1")
    monkeypatch.setenv("SOR_MIN_EDGE_BPS", "1.0")
    monkeypatch.setenv("SOR_MIN_SIZE_USD", "10")
    monkeypatch.setenv("SOR_MAX_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("SOR_QUOTE_TTL_MS", "1000")
    monkeypatch.setenv("SOR_FEES_BPS", '{"binance":2.0,"okx":2.0,"bybit":2.0}')
    monkeypatch.setenv("SOR_FUNDING_BPS_1H", '{"binance":0,"okx":0,"bybit":0}')
    monkeypatch.setenv("SOR_VENUE_PREFS", '{"binance":1,"okx":1,"bybit":1}')

    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    monkeypatch.setattr("app.router.smart_router.get_profile", lambda: SimpleNamespace(name="test"))
    monkeypatch.setattr("app.router.smart_router.is_live", lambda profile: False)
    monkeypatch.setattr("app.router.smart_router.ff.md_watchdog_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.risk_limits_on", lambda: False)

    state = SimpleNamespace(config=SimpleNamespace(data=None))
    market_data = SimpleNamespace()
    return SmartRouter(state=state, market_data=market_data)


def _quote_map(
    long_bid: float, long_ask: float, short_bid: float, short_ask: float
) -> Dict[str, Quote]:
    ts_ms = int(time.time() * 1000)
    return {
        "bybit": Quote(
            venue="bybit",
            symbol="BTCUSDT",
            bid=Decimal(str(long_bid)),
            ask=Decimal(str(long_ask)),
            ts_ms=ts_ms,
        ),
        "okx": Quote(
            venue="okx",
            symbol="BTCUSDT",
            bid=Decimal(str(short_bid)),
            ask=Decimal(str(short_ask)),
            ts_ms=ts_ms,
        ),
    }


def test_submit_intervenue_arb_executes_two_legs(
    router: SmartRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    quotes = _quote_map(100.0, 99.9, 100.5, 100.2)
    monkeypatch.setattr(router, "_get_sor_quotes", lambda symbol: quotes)

    submissions: List[Dict[str, object]] = []
    original_register = router.register_order

    def _wrapped_register(**kwargs: object) -> Dict[str, object]:
        submissions.append(kwargs)
        return original_register(**kwargs)

    monkeypatch.setattr(router, "register_order", _wrapped_register)

    result = router.submit_intervenue_arb(
        strategy="xarb",
        symbol="BTCUSDT",
        notional_usd=Decimal("1000"),
        ts_ns=123,
        nonce=5,
    )

    assert result["status"] == "ok"
    assert len(submissions) == 2
    assert submissions[0]["venue"] == "bybit"
    assert submissions[1]["venue"] == "okx"
    plan = result["plan"]
    assert plan.kind == "xarb-perp"
    assert plan.edge_bps > 0


def test_submit_intervenue_arb_blocks_small_edge(
    router: SmartRouter, monkeypatch: pytest.MonkeyPatch
) -> None:
    quotes = _quote_map(100.0, 100.0, 100.0, 100.0)
    monkeypatch.setattr(router, "_get_sor_quotes", lambda symbol: quotes)

    submissions: List[Dict[str, object]] = []
    original_register = router.register_order

    def _wrapped_register(**kwargs: object) -> Dict[str, object]:
        submissions.append(kwargs)
        return original_register(**kwargs)

    monkeypatch.setattr(router, "register_order", _wrapped_register)

    before = dict(smart_router_module._SOR_BLOCKS_TOTAL._values)

    result = router.submit_intervenue_arb(
        strategy="xarb",
        symbol="BTCUSDT",
        notional_usd=Decimal("1000"),
        ts_ns=222,
        nonce=1,
    )

    assert result["status"] == "blocked"
    assert result["reason"] == "sor-block:edge-too-small"
    assert submissions == []
    after_value = smart_router_module._SOR_BLOCKS_TOTAL._values.get(("edge-too-small",), 0.0)
    before_value = before.get(("edge-too-small",), 0.0)
    assert pytest.approx(after_value - before_value, rel=0.0, abs=0.0) == 1.0
