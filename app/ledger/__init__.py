from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Sequence, Tuple

from .pnl_sources import build_ledger_from_history

from ..runtime import leader_lock

LOGGER = logging.getLogger(__name__)

LEDGER_PATH = Path("data/ledger.db")
_LEDGER_LOCK = threading.Lock()
SAFE_RESET_TABLES = frozenset(
    {
        "orders",
        "fills",
        "positions",
        "balances",
        "events",
        "order_journal",
    }
)


def _attach_fencing_meta(payload: Mapping[str, object]) -> Dict[str, object]:
    try:
        return leader_lock.attach_fencing_meta(payload)
    except Exception as exc:
        LOGGER.debug(
            "failed to attach fencing metadata", extra={"error": str(exc)}
        )
        return dict(payload)


def _feature_enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in {"", "0", "false", "off", "no", "disable", "disabled"}


def _connect() -> sqlite3.Connection:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(LEDGER_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _LEDGER_LOCK:
        conn = _connect()
        with conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL,
                    status TEXT NOT NULL,
                    client_ts TEXT NOT NULL,
                    exchange_ts TEXT,
                    idemp_key TEXT UNIQUE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS fills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER NOT NULL,
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    side TEXT NOT NULL,
                    qty REAL NOT NULL,
                    price REAL NOT NULL,
                    fee REAL NOT NULL,
                    ts TEXT NOT NULL,
                    FOREIGN KEY(order_id) REFERENCES orders(id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS positions (
                    venue TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    base_qty REAL NOT NULL,
                    avg_price REAL NOT NULL,
                    ts TEXT NOT NULL,
                    PRIMARY KEY(venue, symbol)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS balances (
                    venue TEXT NOT NULL,
                    asset TEXT NOT NULL,
                    qty REAL NOT NULL,
                    ts TEXT NOT NULL,
                    PRIMARY KEY(venue, asset)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts TEXT NOT NULL,
                    level TEXT NOT NULL,
                    code TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            if _feature_enabled("FEATURE_JOURNAL"):
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS order_journal (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        uuid TEXT NOT NULL UNIQUE,
                        ts TEXT NOT NULL,
                        type TEXT NOT NULL,
                        payload TEXT NOT NULL
                    )
                    """
                )


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fetch_order_by_key(conn: sqlite3.Connection, idemp_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM orders WHERE idemp_key = ?",
        (idemp_key,),
    ).fetchone()


def record_order(
    *,
    venue: str,
    symbol: str,
    side: str,
    qty: float,
    price: float | None,
    status: str,
    client_ts: str,
    exchange_ts: str | None,
    idemp_key: str,
) -> int:
    with _LEDGER_LOCK:
        conn = _connect()
        with conn:
            existing = _fetch_order_by_key(conn, idemp_key)
            if existing:
                conn.execute(
                    """
                    UPDATE orders
                    SET venue = ?, symbol = ?, side = ?, qty = ?, price = ?, status = ?, client_ts = ?, exchange_ts = ?
                    WHERE id = ?
                    """,
                    (
                        venue,
                        symbol,
                        side,
                        qty,
                        price,
                        status,
                        client_ts,
                        exchange_ts,
                        int(existing["id"]),
                    ),
                )
                _record_event_locked(
                    conn,
                    level="INFO",
                    code="order_upserted",
                    payload={
                        "order_id": int(existing["id"]),
                        "venue": venue,
                        "symbol": symbol,
                        "status": status,
                        "idemp_key": idemp_key,
                    },
                )
                return int(existing["id"])
            cursor = conn.execute(
                """
                INSERT INTO orders (venue, symbol, side, qty, price, status, client_ts, exchange_ts, idemp_key)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (venue, symbol, side, qty, price, status, client_ts, exchange_ts, idemp_key),
            )
            order_id = int(cursor.lastrowid)
            _record_event_locked(
                conn,
                level="INFO",
                code="order_recorded",
                payload={
                    "order_id": order_id,
                    "venue": venue,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "status": status,
                },
            )
            return order_id


def get_order(order_id: int) -> Dict[str, object] | None:
    conn = _connect()
    row = conn.execute(
        """
        SELECT id, venue, symbol, side, qty, price, status, client_ts, exchange_ts, idemp_key
        FROM orders
        WHERE id = ?
        """,
        (order_id,),
    ).fetchone()
    return dict(row) if row else None


def update_order_status(order_id: int, status: str) -> None:
    with _LEDGER_LOCK:
        conn = _connect()
        with conn:
            conn.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
            _record_event_locked(
                conn,
                level="INFO",
                code="order_status",
                payload={"order_id": order_id, "status": status},
            )


def _apply_position(
    conn: sqlite3.Connection,
    *,
    venue: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    ts: str,
) -> None:
    row = conn.execute(
        "SELECT base_qty, avg_price FROM positions WHERE venue = ? AND symbol = ?",
        (venue, symbol),
    ).fetchone()
    base_qty = float(row["base_qty"]) if row else 0.0
    avg_price = float(row["avg_price"]) if row else 0.0
    side_lower = side.lower()
    if side_lower == "buy":
        new_qty = base_qty + qty
        if base_qty >= 0:
            total_cost = avg_price * base_qty + price * qty
            avg_price = total_cost / new_qty if new_qty else 0.0
        else:
            if new_qty > 0:
                # flipped from short to long
                avg_price = price
            elif new_qty == 0:
                avg_price = 0.0
        base_qty = new_qty
    else:
        new_qty = base_qty - qty
        if base_qty <= 0:
            if new_qty < 0:
                prev_abs = abs(base_qty)
                new_abs = abs(new_qty)
                avg_price = ((avg_price * prev_abs) + price * qty) / new_abs if new_abs else 0.0
            elif new_qty > 0:
                avg_price = price
            else:
                avg_price = 0.0
        else:
            if new_qty < 0:
                avg_price = price
            elif new_qty == 0:
                avg_price = 0.0
        base_qty = new_qty
    if abs(base_qty) <= 1e-12:
        base_qty = 0.0
        avg_price = 0.0
    conn.execute(
        """
        INSERT INTO positions (venue, symbol, base_qty, avg_price, ts)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(venue, symbol) DO UPDATE SET
            base_qty = excluded.base_qty,
            avg_price = excluded.avg_price,
            ts = excluded.ts
        """,
        (venue, symbol, base_qty, avg_price, ts),
    )


def _apply_balance(
    conn: sqlite3.Connection,
    *,
    venue: str,
    asset: str,
    delta: float,
    ts: str,
) -> None:
    row = conn.execute(
        "SELECT qty FROM balances WHERE venue = ? AND asset = ?",
        (venue, asset),
    ).fetchone()
    qty = float(row["qty"]) if row else 0.0
    qty += delta
    conn.execute(
        """
        INSERT INTO balances (venue, asset, qty, ts)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(venue, asset) DO UPDATE SET
            qty = excluded.qty,
            ts = excluded.ts
        """,
        (venue, asset, qty, ts),
    )


def record_fill(
    *,
    order_id: int,
    venue: str,
    symbol: str,
    side: str,
    qty: float,
    price: float,
    fee: float,
    ts: str,
) -> int:
    with _LEDGER_LOCK:
        conn = _connect()
        with conn:
            cursor = conn.execute(
                """
                INSERT INTO fills (order_id, venue, symbol, side, qty, price, fee, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (order_id, venue, symbol, side, qty, price, fee, ts),
            )
            _apply_position(
                conn,
                venue=venue,
                symbol=symbol,
                side=side,
                qty=qty,
                price=price,
                ts=ts,
            )
            cash_delta = price * qty
            if side.lower() == "buy":
                cash_delta = -cash_delta - fee
            else:
                cash_delta = cash_delta - fee
            _apply_balance(
                conn,
                venue=venue,
                asset="USDT",
                delta=cash_delta,
                ts=ts,
            )
            _record_event_locked(
                conn,
                level="INFO",
                code="fill_recorded",
                payload={
                    "order_id": order_id,
                    "venue": venue,
                    "symbol": symbol,
                    "side": side,
                    "qty": qty,
                    "price": price,
                    "fee": fee,
                },
            )
            return int(cursor.lastrowid)


def _record_event_locked(
    conn: sqlite3.Connection, *, level: str, code: str, payload: Dict[str, object]
) -> None:
    payload = _attach_fencing_meta(payload)
    conn.execute(
        "INSERT INTO events (ts, level, code, payload) VALUES (?, ?, ?, ?)",
        (_now(), level, code, json.dumps(payload, separators=(",", ":"))),
    )


def record_event(*, level: str, code: str, payload: Dict[str, object]) -> None:
    with _LEDGER_LOCK:
        conn = _connect()
        with conn:
            _record_event_locked(conn, level=level, code=code, payload=payload)


def fetch_positions() -> List[Dict[str, object]]:
    conn = _connect()
    rows = conn.execute("SELECT venue, symbol, base_qty, avg_price, ts FROM positions").fetchall()
    return [dict(row) for row in rows]


def fetch_balances() -> List[Dict[str, object]]:
    conn = _connect()
    rows = conn.execute("SELECT venue, asset, qty, ts FROM balances").fetchall()
    return [dict(row) for row in rows]


def fetch_open_orders() -> List[Dict[str, object]]:
    conn = _connect()
    rows = conn.execute(
        """
        SELECT id, venue, symbol, side, qty, price, status, client_ts, exchange_ts, idemp_key
        FROM orders
        WHERE status NOT IN ('filled', 'cancelled')
        ORDER BY client_ts DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_recent_fills(limit: int = 20) -> List[Dict[str, object]]:
    conn = _connect()
    rows = conn.execute(
        """
        SELECT id, order_id, venue, symbol, side, qty, price, fee, ts
        FROM fills
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def fetch_fills_since(since: datetime | str | None = None) -> List[Dict[str, object]]:
    conn = _connect()
    if since is None:
        rows = conn.execute(
            """
            SELECT id, order_id, venue, symbol, side, qty, price, fee, ts
            FROM fills
            ORDER BY ts ASC
            """
        ).fetchall()
        return [dict(row) for row in rows]
    if isinstance(since, datetime):
        ts_value = since.astimezone(timezone.utc).isoformat()
    else:
        ts_value = str(since)
    rows = conn.execute(
        """
        SELECT id, order_id, venue, symbol, side, qty, price, fee, ts
        FROM fills
        WHERE ts >= ?
        ORDER BY ts ASC
        """,
        (ts_value,),
    ).fetchall()
    return [dict(row) for row in rows]


_EVENT_LEVELS = {"info", "warning", "error"}
_MAX_EVENT_LIMIT = 1_000
_MAX_EVENT_WINDOW = timedelta(days=7)


def _parse_timestamp(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    raw = str(value).strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:  # pragma: no cover - FastAPI validation should catch most cases
        raise ValueError(f"invalid timestamp '{value}'") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    else:
        parsed = parsed.astimezone(timezone.utc)
    return parsed


def _normalise_level(level: str | None) -> str | None:
    if not level:
        return None
    value = str(level).strip().lower()
    if not value:
        return None
    if value not in _EVENT_LEVELS:
        raise ValueError("level must be one of: info, warning, error")
    return value


def _event_message(payload: Mapping[str, object] | None) -> str:
    if not payload:
        return ""
    message = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(message, str) and message:
        return message
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail:
        return detail
    return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def _event_lookup(payload: Mapping[str, object] | None, keys: Sequence[str]) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _filter_events(
    rows: Iterable[sqlite3.Row],
    *,
    venue: str | None,
    symbol: str | None,
    search: str | None,
) -> List[Dict[str, object]]:
    venue_filter = venue.lower() if venue else None
    symbol_filter = symbol.lower() if symbol else None
    search_filter = search.lower() if search else None
    events: List[Dict[str, object]] = []
    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        venue_value = _event_lookup(payload, ("venue", "exchange", "source_venue"))
        symbol_value = _event_lookup(payload, ("symbol", "pair"))
        message_value = _event_message(payload)
        if venue_filter and (venue_value or "").lower() != venue_filter:
            continue
        if symbol_filter and (symbol_value or "").lower() != symbol_filter:
            continue
        if search_filter and search_filter not in message_value.lower():
            continue
        level_value = str(row["level"]).upper()
        code_value = row["code"]
        events.append(
            {
                "id": int(row["id"]),
                "ts": row["ts"],
                "level": level_value,
                "code": code_value,
                "type": code_value,
                "venue": venue_value,
                "symbol": symbol_value,
                "message": message_value,
                "payload": payload,
            }
        )
    return events


def fetch_events_page(
    *,
    offset: int = 0,
    limit: int = 50,
    order: str = "desc",
    venue: str | None = None,
    symbol: str | None = None,
    level: str | None = None,
    since: datetime | str | None = None,
    until: datetime | str | None = None,
    search: str | None = None,
) -> Dict[str, object]:
    try:
        limit_value = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if limit_value <= 0:
        raise ValueError("limit must be positive")
    if limit_value > _MAX_EVENT_LIMIT:
        raise ValueError(f"limit must not exceed {_MAX_EVENT_LIMIT}")
    try:
        offset_value = int(offset)
    except (TypeError, ValueError) as exc:
        raise ValueError("offset must be an integer") from exc
    if offset_value < 0:
        raise ValueError("offset must be greater or equal to zero")
    order_value = str(order or "desc").lower()
    if order_value not in {"asc", "desc"}:
        raise ValueError("order must be either 'asc' or 'desc'")

    level_value = _normalise_level(level)
    since_dt = _parse_timestamp(since)
    until_dt = _parse_timestamp(until)
    if since_dt and until_dt and until_dt < since_dt:
        since_dt, until_dt = until_dt, since_dt
    if since_dt and until_dt and until_dt - since_dt > _MAX_EVENT_WINDOW:
        raise ValueError("time window must not exceed 7 days")

    conn = _connect()
    conditions: List[str] = []
    params: List[object] = []
    if since_dt:
        conditions.append("ts >= ?")
        params.append(since_dt.isoformat())
    if until_dt:
        conditions.append("ts <= ?")
        params.append(until_dt.isoformat())
    if level_value:
        conditions.append("lower(level) = ?")
        params.append(level_value)
    query = "SELECT id, ts, level, code, payload FROM events"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY ts DESC, id DESC"
    rows = conn.execute(query, tuple(params)).fetchall()
    events = _filter_events(rows, venue=venue, symbol=symbol, search=search)
    if order_value == "asc":
        events.reverse()
    total = len(events)
    start = min(offset_value, total)
    end = min(start + limit_value, total)
    items = events[start:end]
    next_offset = end if end < total else end
    has_more = end < total
    return {
        "items": items,
        "total": total,
        "offset": start,
        "limit": limit_value,
        "order": order_value,
        "next_offset": next_offset,
        "has_more": has_more,
    }


def fetch_events(
    limit: int = 50,
    offset: int = 0,
    *,
    order: str = "desc",
    venue: str | None = None,
    symbol: str | None = None,
    level: str | None = None,
    since: datetime | str | None = None,
    until: datetime | str | None = None,
    search: str | None = None,
) -> List[Dict[str, object]]:
    page = fetch_events_page(
        offset=offset,
        limit=limit,
        order=order,
        venue=venue,
        symbol=symbol,
        level=level,
        since=since,
        until=until,
        search=search,
    )
    return page["items"]


def compute_exposures() -> List[Dict[str, object]]:
    exposures: List[Dict[str, object]] = []
    for row in fetch_positions():
        base_qty = float(row["base_qty"])
        avg_price = float(row["avg_price"])
        exposures.append(
            {
                "venue": row["venue"],
                "symbol": row["symbol"],
                "base_qty": base_qty,
                "avg_price": avg_price,
                "notional": base_qty * avg_price,
                "ts": row["ts"],
            }
        )
    return exposures


def compute_pnl() -> Dict[str, float]:
    conn = _connect()
    rows = conn.execute(
        "SELECT venue, symbol, side, qty, price, fee FROM fills ORDER BY id ASC"
    ).fetchall()
    position_state: Dict[Tuple[str, str], Dict[str, float]] = {}
    realized = 0.0
    for row in rows:
        key = (row["venue"], row["symbol"])
        state = position_state.setdefault(key, {"qty": 0.0, "avg": 0.0})
        qty = float(row["qty"])
        price = float(row["price"])
        fee = float(row["fee"])
        side = row["side"].lower()
        if side == "buy":
            prev_qty = state["qty"]
            prev_cost = state["avg"] * prev_qty
            new_qty = prev_qty + qty
            total_cost = prev_cost + price * qty + fee
            state["qty"] = new_qty
            state["avg"] = total_cost / new_qty if new_qty else 0.0
        else:
            held_qty = state["qty"]
            if held_qty <= 0:
                realized += price * qty - fee
            else:
                trade_qty = min(qty, held_qty)
                realized += (price - state["avg"]) * trade_qty - fee
                state["qty"] = max(0.0, held_qty - trade_qty)
                if state["qty"] == 0:
                    state["avg"] = 0.0
    unrealized = 0.0
    for row in conn.execute("SELECT base_qty, avg_price FROM positions").fetchall():
        unrealized += float(row["base_qty"]) * float(row["avg_price"])
    total = realized + unrealized
    return {"realized": realized, "unrealized": unrealized, "total": total}


def reset() -> None:
    with _LEDGER_LOCK:
        conn = _connect()
        with conn:
            tables = ["orders", "fills", "positions", "balances", "events"]
            if _feature_enabled("FEATURE_JOURNAL"):
                tables.append("order_journal")
            for table in tables:
                if table not in SAFE_RESET_TABLES:
                    raise ValueError(f"Unexpected table name: {table}")
                conn.execute(f"DELETE FROM {table}")  # nosec B608  # table name validated


__all__ = [
    "LEDGER_PATH",
    "compute_exposures",
    "compute_pnl",
    "fetch_open_orders",
    "fetch_recent_fills",
    "fetch_fills_since",
    "get_order",
    "fetch_balances",
    "fetch_events",
    "fetch_events_page",
    "fetch_positions",
    "build_ledger_from_history",
    "init_db",
    "record_event",
    "record_fill",
    "record_order",
    "reset",
    "update_order_status",
]
