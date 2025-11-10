"""Prometheus metrics focused on reconciliation monitoring."""

from __future__ import annotations

from prometheus_client import Counter, Gauge

__all__ = [
    "RECON_DIFF_NOTIONAL_GAUGE",
    "RECON_STATUS_GAUGE",
    "RECON_AUTO_HOLD_COUNTER",
]

RECON_DIFF_NOTIONAL_GAUGE = Gauge(
    "propbot_recon_diff_notional_usd",
    "Absolute reconciliation delta in USD (or native units if price unavailable).",
    ("venue", "symbol", "status"),
)

RECON_STATUS_GAUGE = Gauge(
    "propbot_recon_status",
    "Current reconciliation state per venue.",
    ("venue", "status"),
)

RECON_AUTO_HOLD_COUNTER = Counter(
    "propbot_recon_auto_hold_triggered_total",
    "Number of times reconciliation triggered an automatic HOLD.",
)

for venue in ("unknown",):  # pre-warm default labels for exporters
    for status in ("OK", "WARN", "CRITICAL"):
        RECON_STATUS_GAUGE.labels(venue=venue, status=status).set(1.0 if status == "OK" else 0.0)
        RECON_DIFF_NOTIONAL_GAUGE.labels(venue=venue, symbol="UNKNOWN", status=status).set(0.0)

