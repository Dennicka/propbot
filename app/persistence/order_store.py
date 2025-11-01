"""Persistence layer for idempotent order intents and cancel intents."""

from __future__ import annotations

import os
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


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _db_url() -> str:
    return os.environ.get("ORDERS_DB_URL", _DEFAULT_DB_URL)


metadata = MetaData()
Base = declarative_base(metadata=metadata)


class OrderIntentState(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    ACKED = "ACKED"
    REJECTED = "REJECTED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    REPLACED = "REPLACED"


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
    strategy = Column(String(64), nullable=True)
    state = Column(SAEnum(OrderIntentState), nullable=False, default=OrderIntentState.PENDING)
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
    assert _ENGINE is not None
    return _ENGINE


@contextmanager
def session_scope() -> Iterator[Session]:
    _ensure_initialised()
    assert _SESSION_FACTORY is not None
    session: Session = _SESSION_FACTORY()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


TERMINAL_STATES: Sequence[OrderIntentState] = (
    OrderIntentState.ACKED,
    OrderIntentState.REJECTED,
    OrderIntentState.FILLED,
    OrderIntentState.CANCELED,
    OrderIntentState.REPLACED,
)


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
            state=OrderIntentState.PENDING,
            replaced_by=replaced_by,
        )
        session.add(intent)
        session.flush()
    else:
        _ensure_matches(intent, account, venue, symbol, side)
        if replaced_by and intent.replaced_by not in {None, replaced_by}:
            raise ValueError("intent already replaced by another request")
        if replaced_by:
            intent.replaced_by = replaced_by
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


def update_intent_state(
    session: Session,
    intent: OrderIntent,
    *,
    state: OrderIntentState,
    broker_order_id: str | None = None,
) -> OrderIntent:
    intent.state = state
    if broker_order_id:
        intent.broker_order_id = broker_order_id
    intent.updated_ts = _now()
    session.add(intent)
    session.flush()
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
        select(OrderIntent).where(OrderIntent.state.in_([OrderIntentState.PENDING, OrderIntentState.SENT]))
    ).scalars()


def open_intent_count(session: Session) -> int:
    return int(
        session.execute(
            select(func.count(OrderIntent.id)).where(
                OrderIntent.state.notin_(TERMINAL_STATES)
            )
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
    )


__all__ = [
    "CancelIntent",
    "CancelIntentState",
    "IntentSnapshot",
    "OrderIntent",
    "OrderIntentState",
    "TERMINAL_STATES",
    "ensure_cancel_intent",
    "ensure_order_intent",
    "get_engine",
    "inflight_intents",
    "load_intent",
    "load_intent_by_broker_id",
    "metadata",
    "open_intent_count",
    "session_scope",
    "snapshot",
    "update_cancel_state",
    "update_intent_state",
]

