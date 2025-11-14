from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict

from ..broker.paper import PaperBroker
from ..router.order_router import OrderRouter, PretradeGateThrottled
from ..persistence import order_store
from ..rules.pretrade import PretradeValidationError
from ..pricing import TradeCostEstimate
from ..services.runtime import get_execution_orders


router = APIRouter()

_BROKER = PaperBroker("paper")
_ROUTER = OrderRouter(_BROKER)


def _cost_view_from_payload(cost: object) -> "TradeCostEstimateView | None":
    if isinstance(cost, TradeCostEstimate):
        return TradeCostEstimateView.from_estimate(cost)
    if isinstance(cost, Mapping):
        try:
            return TradeCostEstimateView(**cost)
        except (TypeError, ValueError):
            return None
    return None


class TradeCostEstimateView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    venue: str
    symbol: str
    side: str
    qty: Decimal
    price: Decimal
    taker_fee_bps: Decimal
    maker_fee_bps: Decimal
    estimated_fee: Decimal
    funding_rate: Decimal | None = None
    estimated_funding_cost: Decimal
    total_cost: Decimal

    @classmethod
    def from_estimate(cls, estimate: TradeCostEstimate) -> "TradeCostEstimateView":
        return cls(
            venue=estimate.venue,
            symbol=estimate.symbol,
            side=estimate.side,
            qty=estimate.qty,
            price=estimate.price,
            taker_fee_bps=estimate.taker_fee_bps,
            maker_fee_bps=estimate.maker_fee_bps,
            estimated_fee=estimate.estimated_fee,
            funding_rate=estimate.funding_rate,
            estimated_funding_cost=estimate.estimated_funding_cost,
            total_cost=estimate.total_cost,
        )


class ExecutionOrderView(BaseModel):
    model_config = ConfigDict(extra="ignore")

    client_order_id: str
    venue: str | None = None
    symbol: str | None = None
    side: str | None = None
    qty: float | None = None
    price: float | None = None
    strategy: str | None = None
    strategy_id: str | None = None
    ts_ns: int | None = None
    created_ts: float | None = None
    state: str | None = None
    filled_qty: float | None = None
    last_event: str | None = None
    closed: bool | None = None
    cost: TradeCostEstimateView | None = None

    @classmethod
    def from_runtime(cls, payload: Mapping[str, Any]) -> "ExecutionOrderView":
        data = dict(payload)
        cost_view = _cost_view_from_payload(data.pop("cost", None))
        state_value = data.get("state")
        if hasattr(state_value, "value"):
            data["state"] = str(state_value.value)
        elif state_value is not None:
            data["state"] = str(state_value)
        strategy_value = data.get("strategy")
        if strategy_value is not None:
            data["strategy"] = str(strategy_value)
        strategy_id_value = data.get("strategy_id")
        if strategy_id_value is not None:
            data["strategy_id"] = str(strategy_id_value)
        elif strategy_value is not None:
            data["strategy_id"] = str(strategy_value)
        for key in ("qty", "price", "filled_qty"):
            if key in data and data[key] is not None:
                try:
                    data[key] = float(data[key])
                except (TypeError, ValueError):
                    data[key] = None
        return cls(cost=cost_view, **data)


class ExecutionResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    orders: list[ExecutionOrderView]


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


@router.get("/execution", response_model=ExecutionResponse)
def execution() -> ExecutionResponse:
    orders = [ExecutionOrderView.from_runtime(entry) for entry in get_execution_orders()]
    return ExecutionResponse(orders=orders)


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
    except PretradeGateThrottled as exc:
        detail = {"code": "PRETRADE_BLOCKED", "reason": exc.reason, "details": exc.details}
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail) from exc
    except PretradeValidationError as exc:
        detail = {"code": "PRETRADE_INVALID", "reason": exc.reason, "details": exc.details}
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=detail
        ) from exc
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
