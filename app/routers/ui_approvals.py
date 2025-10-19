from __future__ import annotations
from fastapi import APIRouter

from ..services.runtime import get_state

router = APIRouter()


@router.get("/approvals")
def approvals_list() -> dict:
    state = get_state()
    approvals = [
        {
            "id": actor,
            "title": "Operator Approval",
            "requested_by": "system",
            "state": "APPROVED",
            "created_ts": ts,
        }
        for actor, ts in state.control.approvals.items()
    ]
    required = 2
    pending = max(required - len(approvals), 0)
    return {
        "required": required,
        "approved": approvals,
        "pending": pending,
        "two_man_rule": state.control.two_man_rule,
    }
