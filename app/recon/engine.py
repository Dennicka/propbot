"""Offline reconciliation pass between ledger and outbox state."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Dict, List

from app.db import ledger
from app.metrics.core import counter as metrics_counter, gauge as metrics_gauge
from app.metrics.recon import RECON_ISSUES_TOTAL

try:  # pragma: no cover - optional dependency guard
    from app.outbox.journal import OutboxJournal
except ImportError:  # pragma: no cover - fallback when outbox is unavailable
    OutboxJournal = None  # type: ignore[assignment]

_RECON_REPORT_PATH_ENV = "RECON_REPORT_PATH"
_RECON_FAIL_AGE_ENV = "RECON_FAIL_AGE_SEC"
_OUTBOX_PATH_ENV = "OUTBOX_PATH"

_RECON_RUNS_TOTAL = metrics_counter("propbot_recon_runs_total")
_RECON_PENDING_STALE = metrics_gauge("propbot_recon_pending_stale")

_FINAL_STATUSES = {
    "FINAL",
    "FILLED",
    "REJECTED",
    "CANCELED",
    "CANCELLED",
    "FAILED",
    "EXPIRED",
}


def _parse_int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _report_path() -> Path:
    raw = os.getenv(_RECON_REPORT_PATH_ENV, "data/recon/last_report.json")
    target = Path(raw)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _write_report(path: Path, payload: Dict[str, object]) -> None:
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(directory), delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
        tmp_name = handle.name
    os.replace(tmp_name, path)


def _status_counts(statuses: Dict[str, str]) -> Dict[str, int]:
    counts = {"PENDING": 0, "ACKED": 0, "FINAL": 0, "FAILED": 0}
    for status in statuses.values():
        key = status.strip().upper()
        if key == "PENDING":
            counts["PENDING"] += 1
        elif key == "ACKED":
            counts["ACKED"] += 1
        elif key == "FAILED":
            counts["FAILED"] += 1
        else:
            counts["FINAL"] += 1
    return counts


def _load_outbox() -> Dict[str, tuple[float, str, str]]:
    if OutboxJournal is None:
        return {}
    outbox_path = os.getenv(_OUTBOX_PATH_ENV, "data/journal/outbox.jsonl")
    journal = OutboxJournal(outbox_path)
    mapping = getattr(journal, "_by_intent", {})
    if not isinstance(mapping, dict):
        return {}
    return {str(key): value for key, value in mapping.items()}


def _ledger_status_for_issue(ledger_status: str) -> str:
    return ledger_status.strip().upper()


def run_recon(now: float) -> Dict[str, object]:
    statuses = ledger.fetch_orders_status()
    counts = _status_counts(statuses)
    stale_age = _parse_int_env(_RECON_FAIL_AGE_ENV, 10)
    stale_orders = ledger.get_stale_pending(now, stale_age)
    issues: List[Dict[str, object]] = []
    for order_id in stale_orders:
        issues.append({"kind": "pending-stale", "order_id": order_id})

    outbox_records = _load_outbox()
    if outbox_records:
        for intent_key, (_, status, order_id) in outbox_records.items():
            ledger_status = statuses.get(order_id)
            if not ledger_status:
                continue
            ledger_status_key = _ledger_status_for_issue(ledger_status)
            outbox_status = status.strip().upper()
            if outbox_status == "FINAL" and ledger_status_key not in _FINAL_STATUSES:
                issues.append(
                    {
                        "kind": "mismatch-final",
                        "intent_key": intent_key,
                        "order_id": order_id,
                        "ledger_status": ledger_status_key,
                    }
                )
            elif outbox_status == "ACKED" and ledger_status_key not in _FINAL_STATUSES.union(
                {"ACKED"}
            ):
                issues.append(
                    {
                        "kind": "mismatch-acked",
                        "intent_key": intent_key,
                        "order_id": order_id,
                        "ledger_status": ledger_status_key,
                    }
                )

    report = {"counts": counts, "issues": issues}

    _RECON_RUNS_TOTAL.inc()
    _RECON_PENDING_STALE.set(float(len(stale_orders)))
    for item in issues:
        kind = str(item.get("kind", "unknown"))
        RECON_ISSUES_TOTAL.labels(kind=kind, code="orders", severity="WARN").inc()

    _write_report(_report_path(), report)
    return report


__all__ = ["run_recon"]
