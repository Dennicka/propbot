from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

import pytest

from app.analytics import calc_attribution


def _make_service_fixture(
    monkeypatch: pytest.MonkeyPatch,
    *,
    trades: Iterable[dict[str, object]],
    funding_events: Iterable[dict[str, object]],
    tracker_realized: dict[str, float],
) -> None:
    from app.services import pnl_attribution as service

    monkeypatch.setattr(service.runtime, "get_state", lambda: object())
    monkeypatch.setattr(service, "list_positions", lambda: [])

    async def fake_snapshot(_state, _positions):
        return {"positions": []}

    monkeypatch.setattr(service, "build_positions_snapshot", fake_snapshot)
    monkeypatch.setattr(
        service, "_build_trade_events", lambda _positions: [dict(t) for t in trades]
    )
    monkeypatch.setattr(
        service,
        "_load_funding_events",
        lambda limit=200: [dict(event) for event in funding_events],
    )

    class DummyTracker:
        def snapshot(self, *, exclude_simulated: bool | None = None):
            return {name: {"realized_today": value} for name, value in tracker_realized.items()}

    monkeypatch.setattr(service, "get_strategy_pnl_tracker", lambda: DummyTracker())
    monkeypatch.setattr(service, "snapshot_strategy_pnl", lambda: {})


@pytest.fixture
def tca_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    config_payload = {
        "profile": "paper",
        "derivatives": None,
        "tca": {
            "tiers": {
                "binance-um": [
                    {
                        "tier": "vip0",
                        "maker_bps": 0.8,
                        "taker_bps": 2.0,
                        "rebate_bps": 0.05,
                        "notional_from": 0.0,
                    }
                ],
                "okx-perp": [
                    {
                        "tier": "vip0",
                        "maker_bps": 0.7,
                        "taker_bps": 1.5,
                        "rebate_bps": 0.2,
                        "notional_from": 0.0,
                    }
                ],
            }
        },
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    monkeypatch.setenv("APP_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "true")
    return config_path


def test_calc_attribution_with_fees_rebates_and_funding(tca_config: Path) -> None:
    trades = [
        {
            "strategy": "alpha",
            "venue": "binance-um",
            "realized": 100.0,
            "unrealized": 10.0,
            "notional": 1_000.0,
            "liquidity": "taker",
        },
        {
            "strategy": "alpha",
            "venue": "okx-perp",
            "realized": -20.0,
            "unrealized": -5.0,
            "notional": 500.0,
            "liquidity": "maker",
        },
        {
            "strategy": "beta",
            "venue": "binance-um",
            "realized": 50.0,
            "unrealized": 0.0,
            "notional": 700.0,
            "liquidity": "taker",
            "simulated": True,
        },
    ]
    fees = [{"strategy": "alpha", "venue": "binance-um", "amount": 0.5}]
    rebates = [{"strategy": "alpha", "venue": "okx-perp", "amount": 0.1}]
    funding_events = [
        {"strategy": "alpha", "venue": "binance-um", "amount": 5.0},
        {"strategy": "beta", "venue": "okx-perp", "amount": -3.0, "simulated": True},
    ]

    result = calc_attribution(trades, fees, rebates, funding_events)

    assert result["meta"]["exclude_simulated"] is True
    assert result["meta"]["tier_table_loaded"] is True

    strategy_alpha = result["by_strategy"]["alpha"]
    venue_binance = result["by_venue"]["binance-um"]

    # Fees from TCA: 1000 * 2bps = 0.2; 500 * 0.7bps = 0.035; plus explicit fee 0.5
    expected_fees_value = 0.2 + 0.035 + 0.5
    assert strategy_alpha["fees"] == pytest.approx(expected_fees_value, abs=1e-9)
    # Rebates: maker leg rebate 500 * 0.02bps = 0.01 plus explicit 0.1
    assert strategy_alpha["rebates"] == pytest.approx(0.01 + 0.1, abs=1e-9)
    assert strategy_alpha["realized"] == pytest.approx(80.0, abs=1e-9)
    assert strategy_alpha["unrealized"] == pytest.approx(5.0, abs=1e-9)
    assert strategy_alpha["funding"] == pytest.approx(5.0, abs=1e-9)

    # Venue aggregation should include same data
    assert venue_binance["realized"] == pytest.approx(100.0, abs=1e-9)
    assert venue_binance["unrealized"] == pytest.approx(10.0, abs=1e-9)
    assert venue_binance["funding"] == pytest.approx(5.0, abs=1e-9)
    assert venue_binance["fees"] == pytest.approx(0.2 + 0.5, abs=1e-9)

    totals = result["totals"]
    assert totals["realized"] == pytest.approx(80.0, abs=1e-9)
    assert totals["unrealized"] == pytest.approx(5.0, abs=1e-9)
    assert totals["funding"] == pytest.approx(5.0, abs=1e-9)
    net_expected = 80.0 + 5.0 - expected_fees_value + (0.01 + 0.1) + 5.0
    assert totals["net"] == pytest.approx(net_expected, abs=1e-9)


def test_calc_attribution_includes_simulated_when_flag_false(
    tca_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "false")
    trades = [
        {
            "strategy": "alpha",
            "venue": "binance-um",
            "realized": 100.0,
            "unrealized": 10.0,
            "notional": 1_000.0,
            "liquidity": "taker",
        },
        {
            "strategy": "alpha",
            "venue": "okx-perp",
            "realized": -20.0,
            "unrealized": -5.0,
            "notional": 500.0,
            "liquidity": "maker",
        },
        {
            "strategy": "beta",
            "venue": "binance-um",
            "realized": 50.0,
            "unrealized": 0.0,
            "notional": 700.0,
            "liquidity": "taker",
            "simulated": True,
        },
    ]
    fees = [{"strategy": "alpha", "venue": "binance-um", "amount": 0.5}]
    rebates = [{"strategy": "alpha", "venue": "okx-perp", "amount": 0.1}]
    funding_events = [
        {"strategy": "alpha", "venue": "binance-um", "amount": 5.0},
        {"strategy": "beta", "venue": "okx-perp", "amount": -3.0, "simulated": True},
    ]

    result = calc_attribution(trades, fees, rebates, funding_events)

    totals = result["totals"]
    assert totals["realized"] == pytest.approx(130.0, abs=1e-9)
    assert totals["unrealized"] == pytest.approx(5.0, abs=1e-9)
    assert totals["funding"] == pytest.approx(2.0, abs=1e-9)
    assert totals["fees"] == pytest.approx(0.875, abs=1e-9)
    assert totals["rebates"] == pytest.approx(0.11, abs=1e-9)
    expected_net = 130.0 + 5.0 - 0.875 + 0.11 + 2.0
    assert totals["net"] == pytest.approx(expected_net, abs=1e-9)
    assert result["meta"]["exclude_simulated"] is False


@pytest.mark.asyncio
async def test_build_pnl_attribution_excludes_simulated_everywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import pnl_attribution as service

    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "true")

    trades = [
        {
            "strategy": "alpha",
            "venue": "binance",
            "realized": 40.0,
            "unrealized": 2.0,
            "notional": 0.0,
            "liquidity": "taker",
            "simulated": False,
        },
        {
            "strategy": "alpha",
            "venue": "binance",
            "realized": -5.0,
            "unrealized": -1.0,
            "notional": 0.0,
            "liquidity": "maker",
            "simulated": True,
        },
    ]
    funding = [
        {"strategy": "alpha", "venue": "binance", "amount": 1.5, "simulated": False},
        {"strategy": "alpha", "venue": "binance", "amount": -0.5, "simulated": True},
    ]

    _make_service_fixture(
        monkeypatch,
        trades=trades,
        funding_events=funding,
        tracker_realized={"alpha": 40.0},
    )

    result = await service.build_pnl_attribution()

    assert result["simulated_excluded"] is True
    assert result["meta"]["trades_count"] == 1
    assert result["meta"]["funding_events_count"] == 1
    assert "tracker-adjustment" not in result["by_venue"]

    alpha_bucket = result["by_strategy"]["alpha"]
    assert alpha_bucket["realized"] == pytest.approx(40.0, abs=1e-9)
    assert alpha_bucket["unrealized"] == pytest.approx(2.0, abs=1e-9)
    assert alpha_bucket["funding"] == pytest.approx(1.5, abs=1e-9)

    totals = result["totals"]
    assert totals["realized"] == pytest.approx(40.0, abs=1e-9)
    assert totals["unrealized"] == pytest.approx(2.0, abs=1e-9)
    assert totals["funding"] == pytest.approx(1.5, abs=1e-9)


@pytest.mark.asyncio
async def test_build_pnl_attribution_includes_simulated_when_flag_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import pnl_attribution as service

    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "false")

    trades = [
        {
            "strategy": "alpha",
            "venue": "binance",
            "realized": 40.0,
            "unrealized": 2.0,
            "notional": 0.0,
            "liquidity": "taker",
            "simulated": False,
        },
        {
            "strategy": "alpha",
            "venue": "binance",
            "realized": -5.0,
            "unrealized": -1.0,
            "notional": 0.0,
            "liquidity": "maker",
            "simulated": True,
        },
    ]
    funding = [
        {"strategy": "alpha", "venue": "binance", "amount": 1.5, "simulated": False},
        {"strategy": "alpha", "venue": "binance", "amount": -0.5, "simulated": True},
    ]

    _make_service_fixture(
        monkeypatch,
        trades=trades,
        funding_events=funding,
        tracker_realized={"alpha": 35.0},
    )

    result = await service.build_pnl_attribution()

    assert result["simulated_excluded"] is False
    assert result["meta"]["trades_count"] == 2
    assert result["meta"]["funding_events_count"] == 2
    assert "tracker-adjustment" not in result["by_venue"]

    alpha_bucket = result["by_strategy"]["alpha"]
    assert alpha_bucket["realized"] == pytest.approx(35.0, abs=1e-9)
    assert alpha_bucket["unrealized"] == pytest.approx(1.0, abs=1e-9)
    assert alpha_bucket["funding"] == pytest.approx(1.0, abs=1e-9)

    totals = result["totals"]
    assert totals["realized"] == pytest.approx(35.0, abs=1e-9)
    assert totals["unrealized"] == pytest.approx(1.0, abs=1e-9)
    assert totals["funding"] == pytest.approx(1.0, abs=1e-9)
