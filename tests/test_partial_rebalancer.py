from datetime import datetime, timezone

import pytest

from app.hedge.rebalancer import PartialHedgeRebalancer
from app.services.runtime import get_state, reset_for_tests
from app.watchdog.exchange_watchdog import reset_exchange_watchdog_for_tests
from positions import create_position, list_positions


class _StubClient:
    def __init__(self) -> None:
        self.orders: list[dict[str, object]] = []

    def get_mark_price(self, symbol: str) -> dict:
        return {"mark_price": 100.0}

    def place_order(self, symbol: str, side: str, notional_usdt: float, leverage: float) -> dict:
        qty = float(notional_usdt) / 100.0
        order = {
            "exchange": "stub",
            "symbol": symbol,
            "side": side,
            "avg_price": 100.0,
            "filled_qty": qty,
            "status": "filled",
            "order_id": f"stub-{symbol}-{side}",
            "notional_usdt": notional_usdt,
            "leverage": leverage,
        }
        self.orders.append(order)
        return order


@pytest.mark.asyncio
async def test_partial_rebalancer_places_additional_leg(monkeypatch):
    reset_for_tests()
    reset_exchange_watchdog_for_tests()
    state = get_state()
    state.control.mode = "RUN"
    state.control.safe_mode = False
    state.control.dry_run = False
    state.safety.hold_active = False
    state.safety.hold_reason = None
    monkeypatch.setenv("FEATURE_REBALANCER", "1")
    timestamp = datetime.now(timezone.utc).isoformat()
    create_position(
        symbol="BTCUSDT",
        long_venue="binance",
        short_venue="okx",
        notional_usdt=1_000.0,
        entry_spread_bps=10.0,
        leverage=2.0,
        entry_long_price=100.0,
        entry_short_price=100.0,
        status="partial",
        legs=[
            {
                "venue": "binance",
                "symbol": "BTCUSDT",
                "side": "long",
                "notional_usdt": 1_000.0,
                "timestamp": timestamp,
                "status": "partial",
                "entry_price": 100.0,
                "base_size": 10.0,
            },
            {
                "venue": "okx",
                "symbol": "BTC-USDT-SWAP",
                "side": "short",
                "notional_usdt": 1_000.0,
                "timestamp": timestamp,
                "status": "missing",
                "entry_price": 100.0,
                "base_size": 0.0,
            },
        ],
    )

    stub = _StubClient()
    monkeypatch.setattr("app.hedge.rebalancer._client_for", lambda venue: stub)
    recorded: list[dict[str, object]] = []
    monkeypatch.setattr("app.hedge.rebalancer.record_order", lambda **kwargs: recorded.append(kwargs) or 1)

    rebalancer = PartialHedgeRebalancer(
        interval=0.1,
        retry_delay=0.0,
        batch_notional=2_000.0,
        max_retry=3,
        client_factory=lambda _venue: stub,
    )
    await rebalancer.run_cycle()

    updated = list_positions()[0]
    assert str(updated.get("status")).lower() in {"open", "partial"}
    rebalancer_meta = updated.get("rebalancer", {}) or {}
    assert int(rebalancer_meta.get("attempts", 0)) >= 1
    assert recorded, "rebalance order should be recorded"
    assert rebalancer_meta.get("status") in {"settled", "rebalancing", "open"}
