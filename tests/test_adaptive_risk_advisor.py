from services import adaptive_risk_advisor
from app.services import risk_guard


def _snapshot(
    pnl: float, exposure: float, *, open_positions: int = 2, partial_positions: int = 0
) -> dict:
    return {
        "unrealized_pnl_total": pnl,
        "total_exposure_usd_total": exposure,
        "open_positions": open_positions,
        "partial_positions": partial_positions,
    }


def test_adaptive_risk_advisor_loosens_limits(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "100000")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")

    snapshots = [
        _snapshot(1200.0, 45000.0, open_positions=3, partial_positions=0),
        _snapshot(1250.0, 47000.0, open_positions=3, partial_positions=0),
        _snapshot(1300.0, 42000.0, open_positions=3, partial_positions=0),
        _snapshot(1400.0, 43000.0, open_positions=3, partial_positions=0),
        _snapshot(1500.0, 44000.0, open_positions=3, partial_positions=0),
    ]

    advice = adaptive_risk_advisor.generate_risk_advice(snapshots, dry_run_mode=False)

    assert advice["recommendation"] == "loosen"
    assert advice["suggested_max_notional"] > advice["current_max_notional"]
    assert advice["suggested_max_positions"] >= advice["current_max_positions"]
    assert "loosen" in advice["reason"].lower()


def test_adaptive_risk_advisor_tightens_on_risk(monkeypatch):
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "80000")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "4")

    snapshots = [
        _snapshot(200.0, 60000.0, open_positions=2, partial_positions=1),
        _snapshot(-150.0, 70000.0, open_positions=2, partial_positions=2),
        _snapshot(-400.0, 78000.0, open_positions=2, partial_positions=2),
    ]

    hold_info = {"hold_reason": risk_guard.REASON_RUNAWAY_NOTIONAL, "hold_active": True}

    advice = adaptive_risk_advisor.generate_risk_advice(
        snapshots,
        hold_info=hold_info,
        dry_run_mode=True,
    )

    assert advice["recommendation"] == "tighten"
    assert advice["suggested_max_notional"] < advice["current_max_notional"]
    assert advice["suggested_max_positions"] < advice["current_max_positions"]
    assert advice["recommend_dry_run_mode"] is True
    assert "tighten" in advice["reason"].lower()
