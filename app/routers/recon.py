from __future__ import annotations

from fastapi import APIRouter

from ..services.operator_dashboard import build_reconciliation_summary

router = APIRouter()


@router.get("/api/ui/recon_status", tags=["ui"])
def recon_status() -> dict[str, object]:
    """Return the reconciliation runtime snapshot.

    Example response::

        {
            "status": "OK",
            "mismatches_count": 0,
            "auto_hold": false,
            "last_run_iso": "2024-05-04T12:34:56+00:00"
        }
    """

    return build_reconciliation_summary()
