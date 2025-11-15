"""Persistence layer for idempotent order intents and cancel intents."""

from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Iterable, Iterator, Sequence

from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    Float,
    Integer,
    MetaData,
    String,
    UniqueConstraint,
    create_engine,
    event,
    func,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker


_DEFAULT_DB_URL = "sqlite:///data/orders.db"


LOGGER = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db_url() -> str:
    return os.environ.get("ORDERS_DB_URL", _DEFAULT_DB_URL)


metadata = MetaData()
Base = declarative_base(metadata=metadata)


class OrderIntentState(str, Enum):
    NEW = "NEW"
    PENDING = "PENDING"
    SENT = "SENT"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    REPLACED = "REPLACED"


class OrderRequestState(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    COMPLETED = "COMPLETED"


class CancelIntentState(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    ACKED = "ACKED"
    REJECTED = "REJECTED"


class OrderIntent(Base):
    __tablename__ = "order_intents"

    id = Column(Integer, primary_key=True)
    intent_id = Column(String(64), nullable=False, unique=True)
    request_id = Column(String(64), nullable=False)
    account = Column(String(64), nullable=False)
    venue = Column(String(64), nullable=False)
    symbol = Column(String(64), nullable=False)
    side = Column(String(8), nullable=False)
    type = Column(String(16), nullable=False)
    tif = Column(String(16), nullable=True)
    qty = Column(Float, nullable=False)
    price = Column(Float, nullable=True)
    filled_qty = Column(Float, nullable=False, default=0.0)
    remaining_qty = Column(Float, nullable=True)
    avg_fill_price = Column(Float, nullable=True)
    strategy = Column(String(64), nullable=True)
    state = Column(SAEnum(OrderIntentState), nullable=False, default=OrderIntentState.NEW)
    broker_order_id = Column(String(128), nullable=True, index=True)
    replaced_by = Column(String(64), nullable=True)
    created_ts = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_ts = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("account", "venue", "intent_id", name="uq_order_intent_scope"),
    )


class CancelIntent(Base):
    __tablename__ = "cancel_intents"

    id = Column(Integer, primary_key=True)
    intent_id = Column(String(64), nullable=False, unique=True)
    request_id = Column(String(64), nullable=False)
    broker_order_id = Column(String(128), nullable=False, index=True)
    account = Column(String(64), nullable=False)
    venue = Column(String(64), nullable=False)
    reason = Column(String(128), nullable=True)
    state = Column(SAEnum(CancelIntentState), nullable=False, default=CancelIntentState.PENDING)
    created_ts = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_ts = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (
        UniqueConstraint("account", "venue", "intent_id", name="uq_cancel_intent_scope"),
    )


class OrderRequestLedger(Base):
    __tablename__ = "order_request_ledger"

    id = Column(Integer, primary_key=True)
    intent_id = Column(String(64), nullable=False, index=True)
    request_id = Column(String(64), nullable=False, unique=True, index=True)
    state = Column(SAEnum(OrderRequestState), nullable=False, default=OrderRequestState.ACTIVE)
    superseded_by = Column(String(64), nullable=True)
    created_ts = Column(DateTime(timezone=True), nullable=False, default=_now)
    updated_ts = Column(DateTime(timezone=True), nullable=False, default=_now, onupdate=_now)

    __table_args__ = (UniqueConstraint("intent_id", "request_id", name="uq_order_request_intent"),)


EngineFactory = sessionmaker

_ENGINE: Engine | None = None
_SESSION_FACTORY: EngineFactory | None = None


def _configure_engine(url: str) -> Engine:
    engine = create_engine(url, future=True)

    if url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, connection_record):  # pragma: no cover - wiring
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def _ensure_initialised() -> None:
    global _ENGINE, _SESSION_FACTORY
    if _ENGINE is None:
        _ENGINE = _configure_engine(_db_url())
        metadata.create_all(_ENGINE)
    if _SESSION_FACTORY is None:
        _SESSION_FACTORY = sessionmaker(bind=_ENGINE, expire_on_commit=False, future=True)


def get_engine() -> Engine:
    _ensure_initialised()
    assert _ENGINE is not None  # nosec B101  # initialised via configure_engine
    return _ENGINE


@contextmanager
def session_scope() -> Iterator[Session]:
    _ensure_initialised()
    assert _SESSION_FACTORY is not None  # nosec B101  # initialised via configure_engine
    session: Session = _SESSION_FACTORY()
    try:
        yield session
        session.commit()
    except Exception as exc:
        session.rollback()
        LOGGER.exception("order store session rollback", extra={"error": str(exc)})
        raise
    finally:
        session.close()


_STRICT_TERMINAL_STATES: Sequence[OrderIntentState] = (
    OrderIntentState.FILLED,
    OrderIntentState.CANCELED,
    OrderIntentState.REJECTED,
    OrderIntentState.EXPIRED,
    OrderIntentState.REPLACED,
)

TERMINAL_STATES: Sequence[OrderIntentState] = _STRICT_TERMINAL_STATES

ACTIVE_STATES: Sequence[OrderIntentState] = (
    OrderIntentState.NEW,
    OrderIntentState.PENDING,
    OrderIntentState.SENT,
    OrderIntentState.ACKED,
    OrderIntentState.PARTIAL,
)

COMPLETED_FOR_IDEMPOTENCY: Sequence[OrderIntentState] = (
    OrderIntentState.ACKED,
    *TERMINAL_STATES,
)

_ALLOWED_TRANSITIONS: dict[OrderIntentState | None, set[OrderIntentState]] = {
    None: {OrderIntentState.NEW},
    OrderIntentState.NEW: {
        OrderIntentState.PENDING,
        OrderIntentState.SENT,
        OrderIntentState.ACKED,
        OrderIntentState.PARTIAL,
        OrderIntentState.FILLED,
        OrderIntentState.CANCELED,
        OrderIntentState.REJECTED,
        OrderIntentState.EXPIRED,
    },
    OrderIntentState.PENDING: {
        OrderIntentState.PENDING,
        OrderIntentState.SENT,
        OrderIntentState.ACKED,
        OrderIntentState.FILLED,
        OrderIntentState.PARTIAL,
        OrderIntentState.REJECTED,
        OrderIntentState.CANCELED,
        OrderIntentState.EXPIRED,
        OrderIntentState.REPLACED,
    },
    OrderIntentState.SENT: {
        OrderIntentState.SENT,
        OrderIntentState.ACKED,
        OrderIntentState.PARTIAL,
        OrderIntentState.FILLED,
        OrderIntentState.REJECTED,
        OrderIntentState.CANCELED,
        OrderIntentState.EXPIRED,
        OrderIntentState.REPLACED,
    },
    OrderIntentState.ACKED: {
        OrderIntentState.ACKED,
        OrderIntentState.PENDING,
        OrderIntentState.SENT,
        OrderIntentState.PARTIAL,
        OrderIntentState.FILLED,
        OrderIntentState.CANCELED,
        OrderIntentState.EXPIRED,
        OrderIntentState.REPLACED,
    },
    OrderIntentState.PARTIAL: {
        OrderIntentState.PARTIAL,
        OrderIntentState.PENDING,
        OrderIntentState.SENT,
        OrderIntentState.FILLED,
        OrderIntentState.CANCELED,
        OrderIntentState.EXPIRED,
        OrderIntentState.REPLACED,
    },
    OrderIntentState.FILLED: set(),
    OrderIntentState.CANCELED: set(),
    OrderIntentState.REJECTED: set(),
    OrderIntentState.EXPIRED: set(),
    OrderIntentState.REPLACED: set(),
}


class OrderStateTransitionError(RuntimeError):
    pass


def load_intent(session: Session, intent_id: str) -> OrderIntent | None:
    return session.execute(
        select(OrderIntent).where(OrderIntent.intent_id == intent_id)
    ).scalar_one_or_none()


def load_intent_by_broker_id(session: Session, broker_order_id: str) -> OrderIntent | None:
    return session.execute(
        select(OrderIntent).where(OrderIntent.broker_order_id == broker_order_id)
    ).scalar_one_or_none()


def ensure_order_intent(
    session: Session,
    *,
    intent_id: str,
    request_id: str,
    account: str,
    venue: str,
    symbol: str,
    side: str,
    order_type: str,
    qty: float,
    price: float | None,
    tif: str | None,
    strategy: str | None,
    replaced_by: str | None = None,
) -> OrderIntent:
    intent = load_intent(session, intent_id)
    if intent is None:
        intent = OrderIntent(
            intent_id=intent_id,
            request_id=request_id,
            account=account,
            venue=venue,
            symbol=symbol,
            side=side,
            type=order_type,
            qty=qty,
            price=price,
            tif=tif,
            strategy=strategy,
            state=OrderIntentState.NEW,
            replaced_by=replaced_by,
        )
        intent.filled_qty = 0.0
        intent.remaining_qty = float(qty)
        session.add(intent)
        session.flush()
        _ensure_request_record(session, intent.intent_id, request_id)
    else:
        _ensure_matches(intent, account, venue, symbol, side)
        if replaced_by and intent.replaced_by not in {None, replaced_by}:
            raise ValueError("intent already replaced by another request")
        if replaced_by:
            intent.replaced_by = replaced_by
        intent.type = order_type
        intent.qty = float(qty)
        intent.price = float(price) if price is not None else None
        intent.tif = tif
        intent.strategy = strategy
        if intent.remaining_qty is None or intent.remaining_qty > intent.qty:
            intent.remaining_qty = max(float(intent.qty) - float(intent.filled_qty or 0.0), 0.0)
        previous_request = intent.request_id
        if intent.request_id != request_id:
            intent.request_id = request_id
            intent.updated_ts = _now()
            session.add(intent)
            if previous_request:
                _mark_request_state(
                    session,
                    intent.intent_id,
                    previous_request,
                    OrderRequestState.SUPERSEDED,
                    superseded_by=request_id,
                )
            _ensure_request_record(session, intent.intent_id, request_id)
        else:
            intent.updated_ts = _now()
            session.add(intent)
    return intent


def _ensure_matches(
    intent: OrderIntent,
    account: str,
    venue: str,
    symbol: str,
    side: str,
) -> None:
    if (
        intent.account != account
        or intent.venue != venue
        or intent.symbol != symbol
        or intent.side != side
    ):
        raise ValueError("intent parameters do not match existing record")


def _mark_request_state(
    session: Session,
    intent_id: str,
    request_id: str,
    state: OrderRequestState,
    *,
    superseded_by: str | None = None,
) -> None:
    record = session.execute(
        select(OrderRequestLedger).where(OrderRequestLedger.request_id == request_id)
    ).scalar_one_or_none()
    if record is None:
        record = OrderRequestLedger(intent_id=intent_id, request_id=request_id)
    record.state = state
    record.superseded_by = superseded_by
    record.updated_ts = _now()
    session.add(record)
    session.flush()


def _ensure_request_record(session: Session, intent_id: str, request_id: str) -> None:
    record = session.execute(
        select(OrderRequestLedger).where(OrderRequestLedger.request_id == request_id)
    ).scalar_one_or_none()
    if record is None:
        record = OrderRequestLedger(intent_id=intent_id, request_id=request_id)
    record.state = OrderRequestState.ACTIVE
    record.superseded_by = None
    record.updated_ts = _now()
    session.add(record)
    session.flush()


def _validate_state_transition(
    previous: OrderIntentState | None,
    new_state: OrderIntentState,
    *,
    intent: OrderIntent,
    filled_qty: float,
    remaining_qty: float | None,
    broker_order_id: str | None,
) -> None:
    allowed = _ALLOWED_TRANSITIONS.get(previous)
    if allowed is None or new_state not in allowed:
        raise OrderStateTransitionError(
            f"illegal transition {previous!s} -> {new_state!s} for {intent.intent_id}"
        )
    qty = float(intent.qty)
    if qty < 0:
        raise OrderStateTransitionError(f"intent {intent.intent_id} has negative qty")
    if filled_qty < -1e-9:
        raise OrderStateTransitionError(
            f"intent {intent.intent_id} filled_qty negative: {filled_qty}"
        )
    if filled_qty > qty + 1e-6:
        raise OrderStateTransitionError(
            f"intent {intent.intent_id} filled_qty exceeds qty: {filled_qty}>{qty}"
        )
    if remaining_qty is not None and remaining_qty < -1e-9:
        raise OrderStateTransitionError(
            f"intent {intent.intent_id} remaining negative: {remaining_qty}"
        )
    if new_state == OrderIntentState.ACKED and not broker_order_id:
        raise OrderStateTransitionError(f"intent {intent.intent_id} ACKED without broker order id")
    if new_state == OrderIntentState.PARTIAL:
        if filled_qty <= 0 or filled_qty >= qty - 1e-9:
            raise OrderStateTransitionError(
                f"intent {intent.intent_id} invalid PARTIAL fill={filled_qty} qty={qty}"
            )
    if new_state == OrderIntentState.FILLED:
        if abs(filled_qty - qty) > 1e-6 and (remaining_qty or 0.0) > 1e-6:
            raise OrderStateTransitionError(
                f"intent {intent.intent_id} FILLED but qty mismatch ({filled_qty}!={qty})"
            )
    if new_state in (
        OrderIntentState.CANCELED,
        OrderIntentState.REJECTED,
        OrderIntentState.EXPIRED,
    ):
        if filled_qty > qty + 1e-9:
            raise OrderStateTransitionError(
                f"intent {intent.intent_id} terminal {new_state} with overfill"
            )


def _complete_request_if_needed(session: Session, intent: OrderIntent) -> None:
    if not intent.request_id:
        return
    if intent.state in TERMINAL_STATES:
        _mark_request_state(
            session,
            intent.intent_id,
            intent.request_id,
            OrderRequestState.COMPLETED,
        )


def update_intent_state(
    session: Session,
    intent: OrderIntent,
    *,
    state: OrderIntentState,
    broker_order_id: str | None = None,
    filled_qty: float | None = None,
    remaining_qty: float | None = None,
    avg_fill_price: float | None = None,
) -> OrderIntent:
    prev_state = intent.state
    if filled_qty is None:
        filled_value = float(intent.filled_qty or 0.0)
    else:
        filled_value = float(filled_qty)
    if remaining_qty is None:
        remaining_value = intent.remaining_qty
    else:
        remaining_value = float(remaining_qty)
    if avg_fill_price is not None:
        intent.avg_fill_price = float(avg_fill_price)

    _validate_state_transition(
        prev_state,
        state,
        intent=intent,
        filled_qty=filled_value,
        remaining_qty=remaining_value,
        broker_order_id=broker_order_id or intent.broker_order_id,
    )

    intent.state = state
    if broker_order_id:
        intent.broker_order_id = broker_order_id
    intent.filled_qty = filled_value
    if remaining_value is None:
        computed_remaining = max(float(intent.qty) - filled_value, 0.0)
        intent.remaining_qty = computed_remaining
    else:
        intent.remaining_qty = remaining_value
    intent.updated_ts = _now()
    session.add(intent)
    session.flush()
    _complete_request_if_needed(session, intent)
    return intent


def ensure_cancel_intent(
    session: Session,
    *,
    intent_id: str,
    request_id: str,
    broker_order_id: str,
    account: str,
    venue: str,
    reason: str | None = None,
) -> CancelIntent:
    intent = session.execute(
        select(CancelIntent).where(CancelIntent.intent_id == intent_id)
    ).scalar_one_or_none()
    if intent is None:
        intent = CancelIntent(
            intent_id=intent_id,
            request_id=request_id,
            broker_order_id=broker_order_id,
            account=account,
            venue=venue,
            reason=reason,
            state=CancelIntentState.PENDING,
        )
        session.add(intent)
        session.flush()
    else:
        if intent.broker_order_id != broker_order_id:
            raise ValueError("cancel intent broker order mismatch")
    return intent


def update_cancel_state(
    session: Session, intent: CancelIntent, *, state: CancelIntentState
) -> CancelIntent:
    intent.state = state
    intent.updated_ts = _now()
    session.add(intent)
    session.flush()
    return intent


def inflight_intents(session: Session) -> Iterable[OrderIntent]:
    return session.execute(
        select(OrderIntent).where(OrderIntent.state.in_(list(ACTIVE_STATES)))
    ).scalars()


def open_intent_count(session: Session) -> int:
    return int(
        session.execute(
            select(func.count(OrderIntent.id)).where(OrderIntent.state.notin_(TERMINAL_STATES))
        ).scalar()
        or 0
    )


@dataclass(slots=True)
class IntentSnapshot:
    intent_id: str
    request_id: str
    state: OrderIntentState
    broker_order_id: str | None
    account: str
    venue: str
    symbol: str
    side: str
    filled_qty: float
    remaining_qty: float | None


def snapshot(session: Session, intent_id: str) -> IntentSnapshot | None:
    record = load_intent(session, intent_id)
    if record is None:
        return None
    return IntentSnapshot(
        intent_id=record.intent_id,
        request_id=record.request_id,
        state=record.state,
        broker_order_id=record.broker_order_id,
        account=record.account,
        venue=record.venue,
        symbol=record.symbol,
        side=record.side,
        filled_qty=float(record.filled_qty or 0.0),
        remaining_qty=record.remaining_qty,
    )


def active_request_id(session: Session, intent_id: str) -> str | None:
    record = session.execute(
        select(OrderRequestLedger)
        .where(
            OrderRequestLedger.intent_id == intent_id,
            OrderRequestLedger.state == OrderRequestState.ACTIVE,
        )
        .order_by(OrderRequestLedger.updated_ts.desc())
    ).scalar_one_or_none()
    if record is None:
        return None
    return record.request_id


def request_id_history(session: Session, intent_id: str, *, limit: int | None = None) -> list[str]:
    rows = (
        session.execute(
            select(OrderRequestLedger)
            .where(OrderRequestLedger.intent_id == intent_id)
            .order_by(OrderRequestLedger.updated_ts.desc(), OrderRequestLedger.id.desc())
        )
        .scalars()
        .all()
    )
    history: list[str] = []
    for row in rows:
        history.append(row.request_id)
        if limit is not None and len(history) >= max(limit, 0):
            break
    return history


def ensure_active_request(session: Session, intent: OrderIntent, request_id: str) -> OrderIntent:
    previous_request = intent.request_id
    if previous_request == request_id and intent.request_id:
        _ensure_request_record(session, intent.intent_id, request_id)
        return intent
    intent.request_id = request_id
    intent.updated_ts = _now()
    session.add(intent)
    if previous_request:
        _mark_request_state(
            session,
            intent.intent_id,
            previous_request,
            OrderRequestState.SUPERSEDED,
            superseded_by=request_id,
        )
    _ensure_request_record(session, intent.intent_id, request_id)
    return intent


def strategies_by_request_ids(
    session: Session, request_ids: Iterable[str]
) -> dict[str, str | None]:
    cleaned: set[str] = set()
    for value in request_ids:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            cleaned.add(text)
    if not cleaned:
        return {}
    rows = session.execute(
        select(OrderRequestLedger.request_id, OrderIntent.strategy)
        .join(OrderIntent, OrderIntent.intent_id == OrderRequestLedger.intent_id)
        .where(OrderRequestLedger.request_id.in_(cleaned))
    ).all()
    mapping: dict[str, str | None] = {}
    for request_id, strategy in rows:
        key = str(request_id)
        mapping[key] = str(strategy) if strategy else None
    return mapping


__all__ = [
    "CancelIntent",
    "CancelIntentState",
    "OrderRequestLedger",
    "OrderRequestState",
    "OrderStateTransitionError",
    "IntentSnapshot",
    "OrderIntent",
    "OrderIntentState",
    "ACTIVE_STATES",
    "TERMINAL_STATES",
    "COMPLETED_FOR_IDEMPOTENCY",
    "ensure_cancel_intent",
    "ensure_order_intent",
    "ensure_active_request",
    "active_request_id",
    "request_id_history",
    "get_engine",
    "inflight_intents",
    "load_intent",
    "load_intent_by_broker_id",
    "metadata",
    "open_intent_count",
    "session_scope",
    "snapshot",
    "strategies_by_request_ids",
    "update_cancel_state",
    "update_intent_state",
]
