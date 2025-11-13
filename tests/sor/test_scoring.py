from __future__ import annotations

from decimal import Decimal
import time

import pytest

from app.sor.select import Quote, select_best_pair


@pytest.fixture(autouse=True)
def _sor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOR_MIN_EDGE_BPS", "1.0")
    monkeypatch.setenv("SOR_FEES_BPS", '{"binance":2.0,"okx":2.0,"bybit":2.0}')
    monkeypatch.setenv("SOR_MIN_SIZE_USD", "50")
    monkeypatch.setenv("SOR_MAX_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("SOR_QUOTE_TTL_MS", "500")


def test_best_pair_positive_edge() -> None:
    now_ms = int(time.time() * 1000)
    quotes = {
        "binance": Quote(
            venue="binance",
            symbol="BTCUSDT",
            bid=Decimal("100.50"),
            ask=Decimal("100.00"),
            ts_ms=now_ms,
        ),
        "okx": Quote(
            venue="okx",
            symbol="BTCUSDT",
            bid=Decimal("100.60"),
            ask=Decimal("100.10"),
            ts_ms=now_ms,
        ),
        "bybit": Quote(
            venue="bybit",
            symbol="BTCUSDT",
            bid=Decimal("100.40"),
            ask=Decimal("99.95"),
            ts_ms=now_ms,
        ),
    }

    notional = Decimal("500")
    plan, reason = select_best_pair(quotes, "BTCUSDT", notional)

    assert reason == "ok"
    assert plan is not None
    assert plan.edge_bps > 0
    assert plan.legs[0].venue == "bybit"
    assert plan.legs[0].side == "long"
    assert plan.legs[1].venue == "okx"
    assert plan.legs[1].side == "short"
    assert plan.edge_bps >= float(Decimal("1.0"))
