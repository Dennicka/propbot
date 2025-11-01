from __future__ import annotations

import pytest


async def _sample_payload() -> dict[str, object]:
    return {
        "generated_at": "2024-01-01T00:00:00+00:00",
        "by_strategy": {
            "alpha": {
                "realized": 10.0,
                "unrealized": 1.0,
                "fees": 0.2,
                "rebates": 0.05,
                "funding": 0.1,
                "net": 10.95,
            }
        },
        "by_venue": {
            "binance": {
                "realized": 9.0,
                "unrealized": 0.5,
                "fees": 0.1,
                "rebates": 0.02,
                "funding": 0.0,
                "net": 9.42,
            }
        },
        "totals": {
            "realized": 10.0,
            "unrealized": 1.0,
            "fees": 0.2,
            "rebates": 0.05,
            "funding": 0.1,
            "net": 10.95,
        },
        "meta": {"exclude_simulated": True},
        "simulated_excluded": True,
    }


def test_pnl_attrib_requires_token(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret")
    response = client.get("/api/ui/pnl_attrib")
    assert response.status_code == 401


def test_pnl_attrib_returns_payload(monkeypatch: pytest.MonkeyPatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "secret")
    monkeypatch.setattr("app.routers.ui_pnl_attrib.build_pnl_attribution", _sample_payload)

    response = client.get("/api/ui/pnl_attrib", headers={"Authorization": "Bearer secret"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["totals"]["realized"] == pytest.approx(10.0)
    assert payload["by_strategy"]["alpha"]["fees"] == pytest.approx(0.2)


@pytest.mark.asyncio
async def test_build_pnl_attribution_respects_exclude_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import pnl_attribution as service

    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "true")
    monkeypatch.setattr(service.runtime, "get_state", lambda: object())
    monkeypatch.setattr(service, "list_positions", lambda: [])

    async def fake_snapshot(_state, _positions):
        return {"positions": []}

    monkeypatch.setattr(service, "build_positions_snapshot", fake_snapshot)

    base_trades = [
        {
            "strategy": "alpha",
            "venue": "binance",
            "realized": 50.0,
            "unrealized": 4.0,
            "notional": 0.0,
            "liquidity": "taker",
            "simulated": False,
        },
        {
            "strategy": "alpha",
            "venue": "binance",
            "realized": 20.0,
            "unrealized": 1.0,
            "notional": 0.0,
            "liquidity": "maker",
            "simulated": True,
        },
    ]

    def fake_build_trade_events(_positions):
        return [dict(item) for item in base_trades]

    monkeypatch.setattr(service, "_build_trade_events", fake_build_trade_events)

    funding_entries = [
        {"strategy": "alpha", "venue": "binance", "amount": 3.0, "simulated": False},
        {"strategy": "alpha", "venue": "binance", "amount": -1.0, "simulated": True},
    ]

    monkeypatch.setattr(
        service,
        "_load_funding_events",
        lambda limit=200: [dict(item) for item in funding_entries],
    )

    class DummyTracker:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def snapshot(self, *, exclude_simulated: bool | None = None):
            self.calls.append(exclude_simulated)
            return {"alpha": {"realized_today": 50.0}}

    tracker = DummyTracker()
    monkeypatch.setattr(service, "get_strategy_pnl_tracker", lambda: tracker)
    monkeypatch.setattr(service, "snapshot_strategy_pnl", lambda: {})

    result = await service.build_pnl_attribution()

    assert tracker.calls == [True]
    assert result["simulated_excluded"] is True

    totals = result["totals"]
    assert totals["realized"] == pytest.approx(50.0)
    assert totals["funding"] == pytest.approx(3.0)

    alpha_bucket = result["by_strategy"]["alpha"]
    assert alpha_bucket["realized"] == pytest.approx(50.0)
    assert alpha_bucket["funding"] == pytest.approx(3.0)
    assert "tracker-adjustment" not in result["by_venue"]
    assert result["meta"]["trades_count"] == 1
    assert result["meta"]["funding_events_count"] == 1


@pytest.mark.asyncio
async def test_build_pnl_attribution_includes_simulated_when_flag_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import pnl_attribution as service

    monkeypatch.setenv("EXCLUDE_DRY_RUN_FROM_PNL", "false")
    monkeypatch.setattr(service.runtime, "get_state", lambda: object())
    monkeypatch.setattr(service, "list_positions", lambda: [])

    async def fake_snapshot(_state, _positions):
        return {"positions": []}

    monkeypatch.setattr(service, "build_positions_snapshot", fake_snapshot)

    base_trades = [
        {
            "strategy": "alpha",
            "venue": "binance",
            "realized": 50.0,
            "unrealized": 4.0,
            "notional": 0.0,
            "liquidity": "taker",
            "simulated": False,
        },
        {
            "strategy": "alpha",
            "venue": "binance",
            "realized": 20.0,
            "unrealized": 1.0,
            "notional": 0.0,
            "liquidity": "maker",
            "simulated": True,
        },
    ]

    def fake_build_trade_events(_positions):
        return [dict(item) for item in base_trades]

    monkeypatch.setattr(service, "_build_trade_events", fake_build_trade_events)

    funding_entries = [
        {"strategy": "alpha", "venue": "binance", "amount": 3.0, "simulated": False},
        {"strategy": "alpha", "venue": "binance", "amount": -1.0, "simulated": True},
    ]

    monkeypatch.setattr(
        service,
        "_load_funding_events",
        lambda limit=200: [dict(item) for item in funding_entries],
    )

    class DummyTracker:
        def __init__(self) -> None:
            self.calls: list[object] = []

        def snapshot(self, *, exclude_simulated: bool | None = None):
            self.calls.append(exclude_simulated)
            return {"alpha": {"realized_today": 70.0}}

    tracker = DummyTracker()
    monkeypatch.setattr(service, "get_strategy_pnl_tracker", lambda: tracker)
    monkeypatch.setattr(service, "snapshot_strategy_pnl", lambda: {})

    result = await service.build_pnl_attribution()

    assert tracker.calls == [False]
    assert result["simulated_excluded"] is False

    totals = result["totals"]
    assert totals["realized"] == pytest.approx(70.0)
    assert totals["funding"] == pytest.approx(2.0)

    alpha_bucket = result["by_strategy"]["alpha"]
    assert alpha_bucket["realized"] == pytest.approx(70.0)
    assert alpha_bucket["funding"] == pytest.approx(2.0)
    assert result["meta"]["trades_count"] == 2
    assert result["meta"]["funding_events_count"] == 2
