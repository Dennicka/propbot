from __future__ import annotations

from fastapi import APIRouter

from ..services import runtime

router = APIRouter()


@router.get("/status")
def status() -> dict[str, object]:
    snapshot = runtime.get_reconciliation_status()
    payload = dict(snapshot)
    payload["diffs"] = list(snapshot.get("diffs", []))
    payload["issues"] = list(snapshot.get("issues", []))
    payload["diff_count"] = int(payload.get("diff_count") or len(payload["diffs"]))
    payload["issue_count"] = int(payload.get("issue_count") or len(payload["issues"]))
    payload["auto_hold"] = bool(snapshot.get("auto_hold"))
    payload.setdefault("last_checked", snapshot.get("last_checked"))
    payload.setdefault("desync_detected", bool(snapshot.get("desync_detected")))
    return payload
