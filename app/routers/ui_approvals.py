from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

router = APIRouter()

class Approval(BaseModel):
    id: str
    title: str
    requested_by: str
    state: str  # PENDING|APPROVED|REJECTED
    created_ts: str

@router.get("/approvals")
def approvals_list() -> dict:
    return {"pending": []}
