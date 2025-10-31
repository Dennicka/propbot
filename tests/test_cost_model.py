import math

from app.tca.cost_model import (
    FeeTable,
    ImpactModel,
    TierTable,
    effective_cost,
    funding_bps_per_hour,
)


def test_effective_cost_prefers_maker_with_rebate():
    fee_table = FeeTable.from_mapping(
        {"venue-a": {"maker_bps": 1.0, "taker_bps": 3.0, "vip_rebate_bps": 0.5}}
    )
    result = effective_cost(
        "buy",
        qty=1.0,
        px=100.0,
        horizon_min=0.0,
        is_maker_possible=True,
        venue_meta={"venue": "venue-a", "fee_table": fee_table, "funding_bps_per_hour": 0.0},
    )
    assert math.isclose(result["bps"], 0.5, rel_tol=1e-9)
    execution = result["breakdown"]["execution"]
    assert execution["mode"] == "maker"
    assert math.isclose(execution["bps"], 0.5, rel_tol=1e-9)


def test_effective_cost_uses_taker_when_maker_disabled():
    result = effective_cost(
        "sell",
        qty=2.0,
        px=50.0,
        horizon_min=0.0,
        is_maker_possible=False,
        venue_meta={
            "fees": {"maker_bps": 1.0, "taker_bps": 4.2, "vip_rebate_bps": 0.5},
            "funding_bps_per_hour": 0.0,
        },
    )
    assert math.isclose(result["bps"], 4.2, rel_tol=1e-9)
    execution = result["breakdown"]["execution"]
    assert execution["mode"] == "taker"
    assert math.isclose(execution["usdt"], 4.2 * 100.0 / 10_000.0, rel_tol=1e-9)


def test_effective_cost_funding_directional():
    per_hour = funding_bps_per_hour(0.0008, interval_hours=8.0)
    long_result = effective_cost(
        "long",
        qty=1.0,
        px=100.0,
        horizon_min=120.0,
        is_maker_possible=False,
        venue_meta={"fees": {"maker_bps": 0.0, "taker_bps": 2.0}, "funding_bps_per_hour": per_hour},
    )
    short_result = effective_cost(
        "short",
        qty=1.0,
        px=100.0,
        horizon_min=120.0,
        is_maker_possible=False,
        venue_meta={"fees": {"maker_bps": 0.0, "taker_bps": 2.0}, "funding_bps_per_hour": per_hour},
    )
    assert long_result["bps"] > short_result["bps"]
    funding_long = long_result["breakdown"]["funding"]["bps"]
    funding_short = short_result["breakdown"]["funding"]["bps"]
    assert math.isclose(funding_long, -funding_short, rel_tol=1e-9)
    assert math.isclose(funding_long, per_hour * (120.0 / 60.0), rel_tol=1e-9)


def test_effective_cost_uses_vip0_tier_for_taker():
    tiers = TierTable.from_mapping(
        {
            "venue-tier": [
                {
                    "tier": "VIP0",
                    "maker_bps": 1.5,
                    "taker_bps": 4.0,
                    "rebate_bps": 0.0,
                    "notional_from": 0,
                },
                {
                    "tier": "VIP5",
                    "maker_bps": 0.7,
                    "taker_bps": 3.2,
                    "rebate_bps": 0.2,
                    "notional_from": 1_000_000,
                },
            ]
        }
    )
    result = effective_cost(
        "sell",
        qty=1.0,
        px=100.0,
        horizon_min=0.0,
        is_maker_possible=False,
        venue_meta={"venue": "venue-tier"},
        tier_table=tiers,
        rolling_30d_notional=50_000.0,
    )
    assert math.isclose(result["bps"], 4.0, rel_tol=1e-9)
    breakdown = result["breakdown"]
    assert breakdown["tier"] == "VIP0"
    assert breakdown["execution"]["mode"] == "taker"


def test_effective_cost_prefers_vip5_maker_with_rebate():
    tiers = TierTable.from_mapping(
        {
            "venue-tier": [
                {
                    "tier": "VIP0",
                    "maker_bps": 1.2,
                    "taker_bps": 4.5,
                    "rebate_bps": 0.0,
                    "notional_from": 0,
                },
                {
                    "tier": "VIP5",
                    "maker_bps": 0.8,
                    "taker_bps": 3.5,
                    "rebate_bps": 0.25,
                    "notional_from": 2_000_000,
                },
            ]
        }
    )
    result = effective_cost(
        "buy",
        qty=1.0,
        px=200.0,
        horizon_min=0.0,
        is_maker_possible=True,
        venue_meta={"venue": "venue-tier"},
        tier_table=tiers,
        rolling_30d_notional=5_000_000.0,
    )
    execution = result["breakdown"]["execution"]
    assert execution["mode"] == "maker"
    assert result["breakdown"]["tier"] == "VIP5"
    expected_bps = 0.8 - 0.25
    assert math.isclose(result["bps"], expected_bps, rel_tol=1e-9)
    assert math.isclose(execution["bps"], expected_bps, rel_tol=1e-9)


def test_effective_cost_includes_impact_for_low_liquidity():
    model = ImpactModel(k=50.0)
    result = effective_cost(
        "buy",
        qty=2.0,
        px=150.0,
        horizon_min=0.0,
        is_maker_possible=False,
        venue_meta={"fees": {"maker_bps": 0.0, "taker_bps": 2.0, "vip_rebate_bps": 0.0}},
        impact_model=model,
        book_liquidity_usdt=200.0,
    )
    impact_bps = result["breakdown"]["impact_bps"]
    assert impact_bps > 0.0
    assert result["bps"] > 2.0
    assert result["breakdown"]["impact"]["usdt"] > 0.0
