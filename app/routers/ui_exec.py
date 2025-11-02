from __future__ import annotations
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from ..broker.paper import PaperBroker
from ..router.order_router import OrderRouter
from ..persistence import order_store
from ..rules.pretrade import PretradeValidationError


router = APIRouter()

_BROKER = PaperBroker("paper")
_ROUTER = OrderRouter(_BROKER)


class SubmitOrderRequest(BaseModel):
    account: str
    venue: str
    symbol: str
    side: str
    qty: float
    price: float | None = None
    type: str = "LIMIT"
    tif: str | None = None
    strategy: str | None = None
    request_id: str | None = None


class SubmitOrderResponse(BaseModel):
    intent_id: str
    request_id: str
    broker_order_id: str | None
    state: str


def _get_router() -> OrderRouter:
    return _ROUTER


@router.get("/execution")
def execution() -> dict:
    return {"orders": []}


@router.post("/execution/orders", response_model=SubmitOrderResponse)
async def submit_order(
    payload: SubmitOrderRequest,
    router: OrderRouter = Depends(_get_router),
) -> SubmitOrderResponse:
    try:
        ref = await router.submit_order(
            account=payload.account,
            venue=payload.venue,
            symbol=payload.symbol,
            side=payload.side,
            order_type=payload.type,
            qty=payload.qty,
            price=payload.price,
            tif=payload.tif,
            strategy=payload.strategy,
            request_id=payload.request_id,
        )
    except PretradeValidationError as exc:
        detail = {"code": "PRETRADE_INVALID", "reason": exc.reason, "details": exc.details}
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail) from exc
    return SubmitOrderResponse(
        intent_id=ref.intent_id,
        request_id=ref.request_id,
        broker_order_id=ref.broker_order_id,
        state=ref.state.value,
    )


@router.get("/intents/{intent_id}")
def get_intent(intent_id: str) -> dict:
    with order_store.session_scope() as session:
        snapshot = order_store.snapshot(session, intent_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail="intent not found")
    return {
        "intent_id": snapshot.intent_id,
        "request_id": snapshot.request_id,
        "state": snapshot.state.value,
        "broker_order_id": snapshot.broker_order_id,
        "account": snapshot.account,
        "venue": snapshot.venue,
        "symbol": snapshot.symbol,
        "side": snapshot.side,
    }
