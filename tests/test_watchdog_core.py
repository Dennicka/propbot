from __future__ import annotations

from app.watchdog.broker_watchdog import BrokerWatchdog, STATE_DEGRADED, STATE_DOWN, STATE_OK


def _default_thresholds() -> dict[str, dict[str, float]]:
    return {
        "ws_lag_ms_p95": {"degraded": 400.0, "down": 1200.0},
        "ws_disconnects_per_min": {"degraded": 2.0, "down": 6.0},
        "rest_5xx_rate": {"degraded": 0.02, "down": 0.10},
        "rest_timeouts_rate": {"degraded": 0.02, "down": 0.10},
        "order_reject_rate": {"degraded": 0.01, "down": 0.05},
    }


def test_state_transitions_ok_degraded_down() -> None:
    now = [0.0]

    def clock() -> float:
        return now[0]

    watchdog = BrokerWatchdog(
        clock=clock,
        thresholds=_default_thresholds(),
        error_budget_window_s=120.0,
        auto_hold_on_down=False,
    )
    assert watchdog.state_for("binance") == STATE_OK

    watchdog.record_ws_lag("binance", 100.0)
    assert watchdog.state_for("binance") == STATE_OK

    watchdog.record_ws_lag("binance", 600.0)
    assert watchdog.state_for("binance") == STATE_DEGRADED
    snapshot = watchdog.snapshot()
    venue_snapshot = snapshot["per_venue"]["binance"]
    assert venue_snapshot["state"] == STATE_DEGRADED
    assert "ws_lag_ms_p95" in venue_snapshot
    assert snapshot["throttled"] is True

    watchdog.record_ws_disconnect("binance")
    watchdog.record_ws_disconnect("binance")
    watchdog.record_ws_disconnect("binance")
    watchdog.record_ws_disconnect("binance")
    watchdog.record_ws_disconnect("binance")
    watchdog.record_ws_disconnect("binance")
    watchdog.record_ws_disconnect("binance")
    assert watchdog.state_for("binance") == STATE_DOWN
    snapshot = watchdog.snapshot()
    assert snapshot["per_venue"]["binance"]["state"] == STATE_DOWN
    assert snapshot["last_reason"].startswith("binance")


def test_error_budget_triggers_throttle_and_hold() -> None:
    throttle_events: list[tuple[bool, str | None]] = []
    hold_events: list[tuple[str, str, str]] = []
    now = [0.0]

    def clock() -> float:
        return now[0]

    def throttle_cb(active: bool, reason: str | None) -> None:
        throttle_events.append((active, reason))

    def hold_cb(venue: str, state: str, reason: str) -> None:
        hold_events.append((venue, state, reason))

    watchdog = BrokerWatchdog(
        clock=clock,
        thresholds=_default_thresholds(),
        error_budget_window_s=120.0,
        auto_hold_on_down=True,
        on_throttle_change=throttle_cb,
        on_auto_hold=hold_cb,
    )

    for _ in range(7):
        watchdog.record_ws_disconnect("binance")

    assert watchdog.state_for("binance") == STATE_DOWN
    assert throttle_events[-1][0] is True
    assert "binance" in (throttle_events[-1][1] or "")
    assert hold_events == [("binance", STATE_DOWN, "ws_disconnect_spike")]
    assert watchdog.should_block_orders("binance") is True

    now[0] = 200.0
    watchdog.record_ws_lag("binance", 5.0)
    assert watchdog.state_for("binance") == STATE_OK
    assert throttle_events[-1] == (False, None)
    assert watchdog.should_block_orders("binance") is False
