"""Prometheus metrics focused on reconciliation monitoring."""

from __future__ import annotations

from prometheus_client import Gauge

__all__ = ["RECON_DIFF_ABS_USD_GAUGE", "RECON_DIFF_STATE_GAUGE"]

RECON_DIFF_ABS_USD_GAUGE = Gauge(
    "propbot_recon_diff_abs_usd",
    "Absolute reconciliation difference expressed in USD where possible.",
    ("venue", "symbol"),
)

RECON_DIFF_STATE_GAUGE = Gauge(
    "propbot_recon_diff_state",
    "Current reconciliation state per venue/symbol combination.",
    ("venue", "symbol", "state"),
)

for venue in ("unknown",):  # pre-warm default labels for exporters
    RECON_DIFF_ABS_USD_GAUGE.labels(venue=venue, symbol="UNKNOWN").set(0.0)
    for state in ("OK", "WARN", "CRIT"):
        RECON_DIFF_STATE_GAUGE.labels(venue=venue, symbol="UNKNOWN", state=state).set(1.0 if state == "OK" else 0.0)

