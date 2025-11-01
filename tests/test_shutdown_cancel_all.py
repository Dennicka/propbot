import signal

import pytest

from app import ledger
from app.services import runtime


class _StubRunner:
    def __init__(self, label: str, actions: list[str]) -> None:
        self._label = label
        self._actions = actions

    async def stop(self) -> None:
        self._actions.append(self._label)


@pytest.mark.asyncio
async def test_shutdown_handles_sigterm(monkeypatch):
    runtime.reset_for_tests()
    ledger.reset()
    state = runtime.get_state()
    state.control.environment = "paper"

    order_one = ledger.record_order(
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=0.1,
        price=25_000.0,
        status="submitted",
        client_ts="2024-01-01T00:00:00Z",
        exchange_ts=None,
        idemp_key="shutdown-test-1",
    )
    order_two = ledger.record_order(
        venue="binance-um",
        symbol="ETHUSDT",
        side="sell",
        qty=0.2,
        price=1_500.0,
        status="submitted",
        client_ts="2024-01-01T00:01:00Z",
        exchange_ts=None,
        idemp_key="shutdown-test-2",
    )

    actions: list[str] = []

    async def fake_stop_loop():
        actions.append("loop")

    monkeypatch.setattr("app.services.loop.stop_loop", fake_stop_loop)

    from app.services import partial_hedge_runner
    from app.services import recon_runner
    from app.services import exchange_watchdog_runner
    from app.services import autopilot_guard
    from app.services import orchestrator_alerts
    import app.auto_hedge_daemon as auto_hedge_daemon
    import services.opportunity_scanner as opportunity_scanner

    monkeypatch.setattr(
        partial_hedge_runner,
        "get_runner",
        lambda: _StubRunner("partial_hedge_runner", actions),
    )
    monkeypatch.setattr(
        recon_runner,
        "get_runner",
        lambda: _StubRunner("recon_runner", actions),
    )
    monkeypatch.setattr(
        exchange_watchdog_runner,
        "get_runner",
        lambda: _StubRunner("exchange_watchdog", actions),
    )

    async def _auto_stop():
        actions.append("auto_hedge_daemon")

    monkeypatch.setattr(auto_hedge_daemon._daemon, "stop", _auto_stop)

    class _GuardStub:
        async def stop(self) -> None:
            actions.append("autopilot_guard")

    monkeypatch.setattr(autopilot_guard, "get_guard", lambda: _GuardStub())

    async def _stop_alerts():
        actions.append("orchestrator_alerts")

    monkeypatch.setattr(orchestrator_alerts._ALERT_LOOP, "stop", _stop_alerts)

    class _ScannerStub:
        async def stop(self) -> None:
            actions.append("opportunity_scanner")

    monkeypatch.setattr(opportunity_scanner, "get_scanner", lambda: _ScannerStub())

    task = runtime.handle_shutdown_signal(signal.SIGTERM)
    assert task is not None
    result = await task

    assert result["reason"] == "SIGTERM"
    assert result["hold_engaged"] is True
    expected_order = [
        "loop",
        "partial_hedge_runner",
        "recon_runner",
        "exchange_watchdog",
        "auto_hedge_daemon",
        "autopilot_guard",
        "orchestrator_alerts",
        "opportunity_scanner",
    ]
    assert actions == expected_order

    batch_id = result["cancel_all"]["batch_id"]
    assert batch_id
    venue_results = result["cancel_all"]["venues"]
    assert venue_results
    assert all(entry.get("batch_id") for entry in venue_results)
    assert result["cancel_all"]["cancelled"] == 2
    assert result["cancel_all"]["failed"] == 0

    first_order = ledger.get_order(order_one)
    second_order = ledger.get_order(order_two)
    assert first_order is not None and first_order["status"] == "cancelled"
    assert second_order is not None and second_order["status"] == "cancelled"

    duplicate = await runtime.on_shutdown(reason="SIGINT")
    assert duplicate == result
    assert actions == expected_order
