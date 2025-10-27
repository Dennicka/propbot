from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from .. import ledger
from ..security import require_token

router = APIRouter(prefix="/api/ui", tags=["ui"])


@router.get("/alerts", dependencies=[Depends(require_token)])
async def list_alerts(
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1_000),
    order: str = Query("desc"),
    venue: str | None = Query(None),
    symbol: str | None = Query(None),
    level: str | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
    search: str | None = Query(None),
) -> dict[str, object]:
    try:
        page = ledger.fetch_events_page(
            offset=offset,
            limit=limit,
            order=order,
            venue=venue.strip() if venue else None,
            symbol=symbol.strip() if symbol else None,
            level=level.strip() if level else None,
            since=since,
            until=until,
            search=search.strip() if search else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return page
