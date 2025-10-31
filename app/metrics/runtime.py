"""Prometheus metrics covering runtime operations and safety state."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

__all__ = [
    "TRADES_EXECUTED_COUNTER",
    "RISK_BREACHES_COUNTER",
    "AUTO_TRADE_GAUGE",
    "WATCHDOG_STATE_GAUGE",
    "DAILY_LOSS_BREACH_GAUGE",
    "record_trade_execution",
    "record_risk_breach",
    "set_auto_trade_state",
    "set_watchdog_state",
    "set_daily_loss_breach",
]

TRADES_EXECUTED_COUNTER = Counter(
    "propbot_trades_executed_total",
    "Total number of executed trades",
)
TRADES_EXECUTED_COUNTER.inc(0.0)

RISK_BREACHES_COUNTER = Counter(
    "propbot_risk_breaches_total",
    "Risk breaches that forced safety actions",
    ("type",),
)
for breach_type in ("daily_loss", "watchdog"):
    RISK_BREACHES_COUNTER.labels(type=breach_type).inc(0.0)

AUTO_TRADE_GAUGE = Gauge(
    "propbot_auto_trade",
    "Auto trade controller state (1 when enabled)",
    ("state",),
)
AUTO_TRADE_GAUGE.labels(state="on").set(0.0)
AUTO_TRADE_GAUGE.labels(state="off").set(1.0)

WATCHDOG_STATE_GAUGE = Gauge(
    "propbot_watchdog_state",
    "Exchange watchdog state",
    ("exchange", "state"),
)
_WATCHDOG_STATES: tuple[str, ...] = ("OK", "DEGRADED", "AUTO_HOLD")
for status in _WATCHDOG_STATES:
    WATCHDOG_STATE_GAUGE.labels(exchange="unknown", state=status).set(0.0)

DAILY_LOSS_BREACH_GAUGE = Gauge(
    "propbot_daily_loss_breach",
    "Daily loss breach active (1 when breached)",
)
DAILY_LOSS_BREACH_GAUGE.set(0.0)


def record_trade_execution() -> None:
    """Increment the trade execution counter."""

    TRADES_EXECUTED_COUNTER.inc()


def record_risk_breach(kind: str) -> None:
    """Increment the risk breach counter for ``kind`` when recognised."""

    label = str(kind or "").strip().lower()
    if label in {"daily_loss", "watchdog"}:
        RISK_BREACHES_COUNTER.labels(type=label).inc()


def set_auto_trade_state(enabled: bool) -> None:
    """Update auto trade gauge for the current controller state."""

    on_value = 1.0 if enabled else 0.0
    AUTO_TRADE_GAUGE.labels(state="on").set(on_value)
    AUTO_TRADE_GAUGE.labels(state="off").set(1.0 - on_value)


def _normalise_state(value: object) -> str:
    text = str(value or "").strip().upper()
    if text in _WATCHDOG_STATES:
        return text
    return "UNKNOWN"


def set_watchdog_state(exchange: str | None, state: object) -> None:
    """Update the watchdog gauge with the latest ``state`` for ``exchange``."""

    exchange_label = (exchange or "unknown").strip() or "unknown"
    current_state = _normalise_state(state)
    for candidate in _WATCHDOG_STATES:
        WATCHDOG_STATE_GAUGE.labels(exchange=exchange_label, state=candidate).set(
            1.0 if candidate == current_state else 0.0
        )


def set_daily_loss_breach(active: bool) -> None:
    """Expose the current daily loss breach flag via the Prometheus gauge."""

    DAILY_LOSS_BREACH_GAUGE.set(1.0 if active else 0.0)
