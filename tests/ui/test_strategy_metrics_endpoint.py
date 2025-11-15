from decimal import Decimal

from fastapi.testclient import TestClient

from app.pnl.models import StrategyPerformanceSnapshot
from app.routers import ui_strategy_metrics
from app.server_ws import app

client = TestClient(app)


def test_strategy_metrics_endpoint_smoke(monkeypatch) -> None:
    snapshot = StrategyPerformanceSnapshot(
        strategy_id="alpha",
        trades_count=3,
        winning_trades=2,
        losing_trades=1,
        gross_pnl=Decimal("15"),
        net_pnl=Decimal("12"),
        average_trade_pnl=Decimal("4"),
        winrate=2.0 / 3.0,
        turnover_notional=Decimal("300"),
        max_drawdown=Decimal("5"),
    )

    monkeypatch.setattr(ui_strategy_metrics, "get_recent_trades", lambda: [])
    monkeypatch.setattr(
        ui_strategy_metrics,
        "build_strategy_performance",
        lambda trades: [snapshot],
    )

    response = client.get("/api/ui/strategy-metrics")
    assert response.status_code == 200

    payload = response.json()
    assert isinstance(payload, list)
    assert payload

    item = payload[0]
    assert item["strategy_id"] == "alpha"
    assert "enabled" in item
    assert "mode" in item
    assert "priority" in item
    assert "trades_count" in item
    assert "winrate" in item
    assert "net_pnl" in item
    assert "turnover_notional" in item
