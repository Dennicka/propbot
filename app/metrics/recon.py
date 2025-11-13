"""Prometheus metrics focused on reconciliation monitoring."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

from prometheus_client import Counter, Gauge

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

RECON_ISSUES_TOTAL = Counter(
    "propbot_recon_issues_total",
    "Count of reconciliation issues grouped by kind and severity.",
    ("kind", "code", "severity"),
)

RECON_DRIFT_TOTAL = Counter(
    "propbot_recon_drift_total",
    "Count of reconciliation drifts by kind and severity.",
    ("kind", "severity"),
)

RECON_LAST_RUN_TS = Gauge(
    "propbot_recon_last_run_ts",
    "Unix timestamp of the last completed reconciliation run.",
)

RECON_LAST_STATUS = Gauge(
    "propbot_recon_last_status",
    "Latest reconciliation status classification.",
    ("status",),
)

RECON_LAST_SEVERITY = Gauge(
    "propbot_recon_last_severity",
    "Numeric encoding of the last reconciliation severity (0=OK,1=WARN,2=CRITICAL).",
)

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

PNL_LEDGER_REALIZED_TODAY = Gauge(
    "pnl_ledger_realized_today_usd",
    "Realised PnL recorded by the ledger for the current UTC day.",
)

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
    """Export reconciliation integrity metrics in Prometheus text format."""

    target = Path(path or "data/metrics/metrics.prom")
    target.parent.mkdir(parents=True, exist_ok=True)

    lines: list[str] = []

    for venue, value in sorted(orders_open.items()):
        lines.append(f'propbot_orders_open{{venue="{venue}"}} {int(value)}')

    lines.append(f"propbot_orders_final_total {int(orders_final)}")

    invalid_transition = int(anomalies.get("invalid_transition", 0))
    lines.append(f'propbot_anomaly_total{{type="invalid_transition"}} {invalid_transition}')

    for venue, value in sorted(md_staleness_p95_ms.items()):
        lines.append(f'propbot_md_staleness_p95_ms{{venue="{venue}"}} {int(value)}')

    payload = "\n".join(lines) + "\n"
    tmp_path = target.with_suffix(target.suffix + ".tmp")
    tmp_path.write_text(payload, encoding="utf-8")
    os.replace(tmp_path, target)
