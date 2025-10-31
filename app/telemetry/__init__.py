"""Runtime telemetry helpers for PropBot."""

from .metrics import (
    CORE_OPERATION_LATENCY,
    ERROR_COUNTER,
    HEDGE_DAEMON_OK_GAUGE,
    UI_LATENCY,
    WATCHDOG_OK_GAUGE,
    SCANNER_OK_GAUGE,
    observe_core_latency,
    observe_ui_latency,
    record_error,
    set_hedge_daemon_ok,
    set_scanner_ok,
    set_watchdog_ok,
    slo_snapshot,
    reset_for_tests,
)
from .slo import SLOEvaluation, SLOMonitor, evaluate, setup_slo_monitor

__all__ = [
    "CORE_OPERATION_LATENCY",
    "ERROR_COUNTER",
    "HEDGE_DAEMON_OK_GAUGE",
    "UI_LATENCY",
    "WATCHDOG_OK_GAUGE",
    "SCANNER_OK_GAUGE",
    "observe_core_latency",
    "observe_ui_latency",
    "record_error",
    "set_hedge_daemon_ok",
    "set_scanner_ok",
    "set_watchdog_ok",
    "slo_snapshot",
    "reset_for_tests",
    "SLOEvaluation",
    "SLOMonitor",
    "evaluate",
    "setup_slo_monitor",
]
