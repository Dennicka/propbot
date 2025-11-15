from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from ..services import runtime
from app.recon.service import ReconService

router = APIRouter()


class UiReconIssue(BaseModel):
    severity: Literal["info", "warning", "error"]
    kind: str
    venue_id: str
    symbol: str | None = None
    asset: str | None = None
    message: str
    internal_value: str | None = None
    external_value: str | None = None


class UiReconSnapshot(BaseModel):
    venue_id: str
    issues: list[UiReconIssue]
    issues_count: int
    errors_count: int
    warnings_count: int


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


@router.get("/snapshot", response_model=UiReconSnapshot)
async def get_recon_snapshot(
    venue_id: str = Query(..., description="Venue to reconcile"),
) -> UiReconSnapshot:
    """Run reconciliation for a single venue and return aggregated issues."""

    service = ReconService()
    snapshot = await service.run_for_venue(venue_id)

    issues = snapshot.issues
    errors = sum(1 for issue in issues if issue.severity == "error")
    warnings = sum(1 for issue in issues if issue.severity == "warning")

    ui_issues = [
        UiReconIssue(
            severity=issue.severity,
            kind=issue.kind,
            venue_id=issue.venue_id,
            symbol=issue.symbol,
            asset=issue.asset,
            message=issue.message,
            internal_value=issue.internal_value,
            external_value=issue.external_value,
        )
        for issue in issues
    ]

    return UiReconSnapshot(
        venue_id=snapshot.venue_id,
        issues=ui_issues,
        issues_count=len(issues),
        errors_count=errors,
        warnings_count=warnings,
    )
