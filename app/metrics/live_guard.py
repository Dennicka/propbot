from __future__ import annotations

from prometheus_client import Gauge

live_trading_guard_state = Gauge(
    "live_trading_guard_state",
    "Live trading guard state (0=test_only, 1=disabled, 2=enabled)",
    labelnames=("runtime_profile",),
)
