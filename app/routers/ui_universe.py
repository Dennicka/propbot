from __future__ import annotations

from fastapi import APIRouter, Request

from ..security import require_token
from ..universe_manager import UniverseManager


router = APIRouter()


@router.get("/universe")
def universe(request: Request, limit: int = 3) -> dict:
    """Return top trading pairs ranked by the :class:`UniverseManager`."""

    require_token(request)
    manager = UniverseManager()
    pairs = manager.top_pairs(n=limit)
    return {"timestamp": UniverseManager.current_timestamp(), "pairs": pairs}
