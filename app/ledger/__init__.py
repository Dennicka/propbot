from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Dict, List, Tuple

LEDGER_PATH = Path("data/ledger.db")
_LEDGER_LOCK = threading.Lock()


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
            if new_qty < 0:
                # still short, keep average entry
                pass
            elif new_qty > 0:
                # flipped from short to long
                avg_price = price
            else:
                avg_price = 0.0
        base_qty = new_qty
    else:
        new_qty = base_qty - qty
        if base_qty <= 0:
            if new_qty < 0:
                prev_abs = abs(base_qty)
                new_abs = abs(new_qty)
                avg_price = (
                    ((avg_price * prev_abs) + price * qty) / new_abs if new_abs else 0.0
                )
            elif new_qty > 0:
                avg_price = price
            else:
                avg_price = 0.0
        else:
            if new_qty > 0:
                # reducing existing long keeps entry price
                pass
            elif new_qty < 0:
                avg_price = price
            else:
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


def _record_event_locked(conn: sqlite3.Connection, *, level: str, code: str, payload: Dict[str, object]) -> None:
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


def fetch_events(limit: int = 50) -> List[Dict[str, object]]:
    conn = _connect()
    rows = conn.execute(
        "SELECT ts, level, code, payload FROM events ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    events: List[Dict[str, object]] = []
    for row in rows:
        payload = json.loads(row["payload"]) if row["payload"] else {}
        events.append({"ts": row["ts"], "level": row["level"], "code": row["code"], "payload": payload})
    return events


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
            for table in ("orders", "fills", "positions", "balances", "events"):
                conn.execute(f"DELETE FROM {table}")


__all__ = [
    "LEDGER_PATH",
    "compute_exposures",
    "compute_pnl",
    "fetch_balances",
    "fetch_events",
    "fetch_positions",
    "init_db",
    "record_event",
    "record_fill",
    "record_order",
    "reset",
    "update_order_status",
]
