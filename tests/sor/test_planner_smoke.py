from __future__ import annotations

from decimal import Decimal
import time

import pytest

from app.sor.select import Quote, select_best_pair


@pytest.fixture(autouse=True)
def _sor_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SOR_MIN_EDGE_BPS", "1.0")
    monkeypatch.setenv("SOR_MIN_SIZE_USD", "10")
    monkeypatch.setenv("SOR_MAX_SLIPPAGE_BPS", "5")
    monkeypatch.setenv("SOR_QUOTE_TTL_MS", "1000")


def test_route_plan_smoke() -> None:
    now_ms = int(time.time() * 1000)
    quotes = {
        "a": Quote(
            venue="a",
            symbol="BTCUSDT",
            bid=Decimal("101"),
            ask=Decimal("100"),
            ts_ms=now_ms,
        ),
        "b": Quote(
            venue="b",
            symbol="BTCUSDT",
            bid=Decimal("102"),
            ask=Decimal("100.50"),
            ts_ms=now_ms,
        ),
    }

    plan, reason = select_best_pair(quotes, "BTCUSDT", Decimal("1000"))

    assert reason == "ok"
    assert plan is not None
    assert len(plan.legs) == 2
    assert plan.legs[0].qty > 0
    assert plan.legs[1].qty == plan.legs[0].qty

    slip = Decimal("0.0005")
    expected_long = (Decimal("100") * (Decimal("1") + slip)).quantize(Decimal("1e-6"))
    expected_short = (Decimal("102") * (Decimal("1") - slip)).quantize(Decimal("1e-6"))
    assert plan.legs[0].px_limit == expected_long
    assert plan.legs[1].px_limit == expected_short
    assert plan.edge_bps >= 1.0
