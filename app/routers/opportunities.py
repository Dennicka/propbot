from __future__ import annotations
from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

router = APIRouter()

class Opportunity(BaseModel):
    symbol: str
    venue: str
    edge_bps: float

@router.get("/opportunities", response_model=List[Opportunity])
def opportunities() -> List[Opportunity]:
    # paper: пустой список
    return []
