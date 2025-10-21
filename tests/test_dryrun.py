from __future__ import annotations

from app.services import arbitrage
from app.services.dryrun import compute_metrics
from app.services.runtime import get_state, reset_for_tests


def test_compute_metrics_calculations() -> None:
    plan = arbitrage.Plan(
        symbol="BTCUSDT",
        notional=100.0,
        used_slippage_bps=2,
        used_fees_bps={"binance": 2, "okx": 2},
        viable=True,
        legs=[
            arbitrage.PlanLeg(
                exchange="okx",
                side="buy",
                price=20000.0,
                qty=0.005,
                fee_usdt=0.2,
            ),
            arbitrage.PlanLeg(
                exchange="binance",
                side="sell",
                price=20020.0,
                qty=0.005,
                fee_usdt=0.2,
            ),
        ],
        est_pnl_usdt=0.1,
        est_pnl_bps=10.0,
    )
    metrics = compute_metrics(plan)
    assert metrics.total_fees_usdt == 0.4
    assert metrics.total_fees_bps == 40.0
    assert metrics.spread_usdt == 0.5
    assert metrics.spread_bps == 50.0
    assert metrics.est_pnl_usdt == 0.1
    assert metrics.est_pnl_bps == 10.0
    assert metrics.direction == "okx->binance"


def test_engine_dry_run_skips_orders(monkeypatch) -> None:
    reset_for_tests()
    arbitrage._ENGINE = None
    engine = arbitrage.get_engine()
    pair_cfg = next(iter(engine._pair_index.values()))
    pair_id = engine._pair_id(pair_cfg)
    engine._last_edges = [
        {
            "pair_id": pair_id,
            "net_edge_bps": 12.0,
            "tradable_size": 0.25,
        }
    ]

    long_rt = engine.runtime.venues[pair_cfg.long.venue]
    short_rt = engine.runtime.venues[pair_cfg.short.venue]

    def _raise_long(**_: object) -> None:
        raise AssertionError("should not place long order")

    def _raise_short(**_: object) -> None:
        raise AssertionError("should not place short order")

    monkeypatch.setattr(long_rt.client, "place_order", _raise_long)
    monkeypatch.setattr(short_rt.client, "place_order", _raise_short)

    result = engine.execute(pair_id, 0.1, dry_run=True)
    assert result["ok"] is True
    assert result["executed"] is False
    assert result["plan"]["dry_run"] is True

    state = get_state()
    assert state.control.safe_mode is True
    assert state.control.dry_run is True
