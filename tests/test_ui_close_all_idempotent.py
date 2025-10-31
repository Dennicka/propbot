from __future__ import annotations

from typing import List

from app.services.runtime import get_state
from app.services.trades import TradeInstruction


def _reset_close_all_tracker() -> None:
    state = get_state()
    if hasattr(state, "_close_all_tracker"):
        delattr(state, "_close_all_tracker")


def test_ui_close_all_idempotent(client, monkeypatch):
    _reset_close_all_tracker()
    state = get_state()
    state.control.dry_run_mode = False
    trades: List[TradeInstruction] = [
        TradeInstruction(venue="binance-um", symbol="BTCUSDT", close_side="sell", qty=1.0),
        TradeInstruction(venue="okx-perp", symbol="ETHUSDT", close_side="buy", qty=2.0),
    ]

    async def fake_list():
        return list(trades)

    calls: list[TradeInstruction] = []

    async def fake_perform(trade: TradeInstruction):
        calls.append(trade)
        return {
            "venue": trade.venue,
            "symbol": trade.symbol,
            "side": trade.close_side,
            "qty": trade.qty,
            "status": "submitted",
        }

    monkeypatch.setattr("app.services.trades._list_open_trades", fake_list)
    monkeypatch.setattr("app.services.trades._perform_close", fake_perform)

    first = client.post("/api/ui/trades/close-all")
    assert first.status_code == 200
    assert len(calls) == len(trades)

    second = client.post("/api/ui/trades/close-all")
    assert second.status_code == 200
    assert len(calls) == len(trades)
    assert second.json() == {"closed": [], "positions": []}


def test_ui_close_all_audit_logging(client, monkeypatch):
    _reset_close_all_tracker()

    async def fake_list():
        return []

    monkeypatch.setattr("app.services.trades._list_open_trades", fake_list)

    calls = []

    def capture(operator: str, role: str, action: str, details):
        calls.append((operator, role, action, details))

    monkeypatch.setattr("app.routers.ui_trades.log_operator_action", capture)

    response = client.post("/api/ui/trades/close-all")
    assert response.status_code == 200
    assert calls
    operator, role, action, details = calls[-1]
    assert action == "CLOSE_ALL"
    assert isinstance(details, dict)
    assert "count" in details
    assert "dry_run" in details


def test_ui_close_all_disables_auto_trade(client, monkeypatch):
    _reset_close_all_tracker()
    state = get_state()
    state.control.auto_loop = True

    calls = []

    async def fake_hold():
        calls.append("called")
        state.control.auto_loop = False
        return state.loop

    async def fake_list():
        return []

    monkeypatch.setattr("app.routers.ui_trades.hold_loop", fake_hold)
    monkeypatch.setattr("app.services.trades._list_open_trades", fake_list)

    resp = client.post("/api/ui/trades/close-all")
    assert resp.status_code == 200
    assert state.control.auto_loop is False
    assert len(calls) == 1
