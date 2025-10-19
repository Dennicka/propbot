from __future__ import annotations
from datetime import datetime, timezone
from typing import Any, Dict, List

def _ts() -> str:
    return datetime.now(timezone.utc).isoformat()

def get_status_overview() -> Dict[str, Any]:
    return {
        "ts": _ts(),
        "overall": "OK",
        "scores": {"P0": 1.0, "P1": 0.9, "P2": 0.7, "P3": 0.6},
    }

def get_status_components() -> Dict[str, Any]:
    comps = []
    for cid, title, group in [
        ("journal", "Journal/Outbox", "P0"),
        ("leader", "Leader/Fencing", "P0"),
        ("recon", "Reconciliation", "P0"),
        ("lread", "Live Readiness", "P1"),
        ("config", "Config Pipeline", "P1"),
        ("stream", "UI Stream", "P1"),
        ("qpe", "Queue Position Estimator", "P2"),
        ("ab", "A/B Factory", "P3"),
    ]:
        comps.append({
            "id": cid, "title": title, "group": group,
            "status": "OK", "summary": "mock", "metrics": {"p95": 0}
        })
    return {"ts": _ts(), "components": comps}

def get_status_slo() -> Dict[str, Any]:
    return {
        "ts": _ts(),
        "slo": {
            "ws_gap_ms_p95": 120,
            "order_cycle_ms_p95": 180,
            "reject_rate": 0.1,
            "cancel_fail_rate": 0.1,
            "recon_mismatch": 0,
            "max_day_drawdown_bps": 0,
            "budget_remaining": 1_000_000,
        }
    }
