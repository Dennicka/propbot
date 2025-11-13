"""Prometheus metrics focused on reconciliation monitoring."""

from __future__ import annotations

import os
from typing import Mapping

from .core import (
    DEFAULT_METRICS_PATH,
    counter,
    gauge,
    write_metrics,
)

__all__ = [
    "RECON_DIFF_NOTIONAL_GAUGE",
    "RECON_STATUS_GAUGE",
    "RECON_AUTO_HOLD_COUNTER",
    "RECON_ISSUES_TOTAL",
    "RECON_DRIFT_TOTAL",
    "RECON_LAST_RUN_TS",
    "RECON_LAST_STATUS",
    "RECON_LAST_SEVERITY",
    "PNL_LEDGER_REALIZED_TODAY",
    "export_recon_metrics",
]

RECON_ISSUES_TOTAL = counter(
    "propbot_recon_issues_total",
    labels=("kind", "code", "severity"),
)

RECON_DRIFT_TOTAL = counter(
    "propbot_recon_drift_total",
    labels=("kind", "severity"),
)

RECON_LAST_RUN_TS = gauge("propbot_recon_last_run_ts")

RECON_LAST_STATUS = gauge(
    "propbot_recon_last_status",
    labels=("status",),
)

RECON_LAST_SEVERITY = gauge("propbot_recon_last_severity")

RECON_DIFF_NOTIONAL_GAUGE = gauge(
    "propbot_recon_diff_notional_usd",
    labels=("venue", "symbol", "status"),
)

RECON_STATUS_GAUGE = gauge(
    "propbot_recon_status",
    labels=("venue", "status"),
)

RECON_AUTO_HOLD_COUNTER = counter("propbot_recon_auto_hold_triggered_total")

PNL_LEDGER_REALIZED_TODAY = gauge("pnl_ledger_realized_today_usd")

ORDERS_OPEN_GAUGE = gauge("propbot_orders_open", labels=("venue",))
ORDERS_FINAL_GAUGE = gauge("propbot_orders_final_total")
ANOMALIES_GAUGE = gauge("propbot_anomaly_total", labels=("type",))
MD_STALENESS_GAUGE = gauge("propbot_md_staleness_p95_ms", labels=("venue",))
RECON_REPORTS_TOTAL = counter("propbot_recon_reports_total")

for venue in ("unknown",):  # pre-warm default labels for exporters
    for status in ("OK", "WARN", "CRITICAL"):
        RECON_STATUS_GAUGE.labels(venue=venue, status=status).set(1.0 if status == "OK" else 0.0)
        RECON_DIFF_NOTIONAL_GAUGE.labels(venue=venue, symbol="UNKNOWN", status=status).set(0.0)

PNL_LEDGER_REALIZED_TODAY.set(0.0)
RECON_LAST_RUN_TS.set(0.0)
for status in ("OK", "WARN", "CRITICAL"):
    RECON_LAST_STATUS.labels(status=status).set(1.0 if status == "OK" else 0.0)
RECON_LAST_SEVERITY.set(0.0)


def export_recon_metrics(
    *,
    orders_open: Mapping[str, int],
    orders_final: int,
    anomalies: Mapping[str, int],
    md_staleness_p95_ms: Mapping[str, int],
    path: str | os.PathLike[str] | None = None,
) -> None:
    """Export reconciliation integrity metrics via the shared registry."""

    total_open = 0
    for venue, value in sorted(orders_open.items()):
        total_open += int(value)
        ORDERS_OPEN_GAUGE.labels(venue=str(venue)).set(float(value))
    ORDERS_OPEN_GAUGE.labels(venue="all").set(float(total_open))

    ORDERS_FINAL_GAUGE.set(float(orders_final))

    for kind, value in sorted(anomalies.items()):
        ANOMALIES_GAUGE.labels(type=str(kind)).set(float(value))

    for venue, value in sorted(md_staleness_p95_ms.items()):
        MD_STALENESS_GAUGE.labels(venue=str(venue)).set(float(value))

    RECON_REPORTS_TOTAL.inc()

    target_path = path or os.getenv("METRICS_PATH", DEFAULT_METRICS_PATH)
    write_metrics(target_path)
