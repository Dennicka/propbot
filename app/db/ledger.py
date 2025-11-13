"""SQLite-backed ledger for orders, fills and balances."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Mapping

_LEDGER_DB_PATH_ENV = "LEDGER_DB_PATH"
_LEDGER_PRAGMAS_ENV = "LEDGER_PRAGMAS"

_CONN_LOCK = threading.Lock()
_CONN: sqlite3.Connection | None = None
_CONN_PATH: str | None = None


def _default_db_path() -> str:
    return os.getenv(_LEDGER_DB_PATH_ENV, "data/ledger/ledger.db")


def _apply_pragmas(conn: sqlite3.Connection) -> None:
    raw = os.getenv(_LEDGER_PRAGMAS_ENV, "")
    if not raw:
        return
    for chunk in raw.split(";"):
        stmt = chunk.strip()
        if not stmt:
            continue
        try:
            conn.execute(f"PRAGMA {stmt}")
        except sqlite3.Error:
            continue


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS orders(
            order_id TEXT PRIMARY KEY,
            intent_key TEXT,
            strategy TEXT,
            symbol TEXT,
            venue TEXT,
            side TEXT,
            qty TEXT,
            px TEXT,
            status TEXT,
            exch_order_id TEXT,
            ts_created REAL,
            ts_updated REAL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS fills(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id TEXT,
            t REAL,
            qty TEXT,
            px TEXT,
            fee_usd TEXT DEFAULT "0",
            realized_pnl_usd TEXT DEFAULT "0"
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS balances(
            venue TEXT,
            asset TEXT,
            free TEXT,
            locked TEXT,
            ts REAL,
            PRIMARY KEY (venue, asset)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_intent ON orders(intent_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fills_order ON fills(order_id)")
    conn.commit()


def init_db(path: str | None = None) -> sqlite3.Connection:
    """Initialise or replace the global SQLite connection."""

    db_path = path or _default_db_path()
    target = Path(db_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    global _CONN, _CONN_PATH
    with _CONN_LOCK:
        if _CONN is not None:
            try:
                _CONN.close()
            except sqlite3.Error:
                pass
            _CONN = None
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        _apply_pragmas(conn)
        _create_schema(conn)
        _CONN = conn
        _CONN_PATH = db_path
        return conn


def _ensure_conn() -> sqlite3.Connection:
    global _CONN
    if _CONN is not None:
        return _CONN
    with _CONN_LOCK:
        if _CONN is None:
            init_db(_CONN_PATH)
    if _CONN is None:
        raise RuntimeError("ledger connection is not initialised")
    return _CONN


def _to_decimal(value: Decimal | int | float | str | None, default: str = "0") -> Decimal:
    if value is None:
        return Decimal(default)
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal(default)


def upsert_order_begin(order: Mapping[str, object]) -> bool:
    conn = _ensure_conn()
    order_id = str(order.get("order_id", "")).strip()
    if not order_id:
        return False
    now_ts = time.time()
    intent_key = str(order.get("intent_key", ""))
    strategy = str(order.get("strategy", ""))
    symbol = str(order.get("symbol", ""))
    venue = str(order.get("venue", ""))
    side = str(order.get("side", ""))
    qty = _to_decimal(order.get("qty"), "0")
    px = _to_decimal(order.get("px"), "0")
    exch_order_id = str(order.get("exch_order_id", ""))
    cursor = conn.execute(
        """
        INSERT INTO orders (
            order_id,intent_key,strategy,symbol,venue,side,qty,px,status,exch_order_id,ts_created,ts_updated
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(order_id) DO UPDATE SET
            intent_key=excluded.intent_key,
            strategy=excluded.strategy,
            symbol=excluded.symbol,
            venue=excluded.venue,
            side=excluded.side,
            qty=excluded.qty,
            px=excluded.px,
            status=excluded.status,
            ts_updated=excluded.ts_updated,
            exch_order_id=COALESCE(orders.exch_order_id, excluded.exch_order_id),
            ts_created=COALESCE(orders.ts_created, excluded.ts_created)
        """,
        (
            order_id,
            intent_key,
            strategy,
            symbol,
            venue,
            side,
            str(qty),
            str(px),
            "PENDING",
            exch_order_id,
            now_ts,
            now_ts,
        ),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_order_acked(order_id: str, exch_order_id: str = "") -> bool:
    conn = _ensure_conn()
    now_ts = time.time()
    cursor = conn.execute(
        "UPDATE orders SET status=?, exch_order_id=?, ts_updated=? WHERE order_id=?",
        ("ACKED", str(exch_order_id), now_ts, order_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def mark_order_final(order_id: str, status: str) -> bool:
    conn = _ensure_conn()
    status_value = (status or "FINAL").strip().upper() or "FINAL"
    now_ts = time.time()
    cursor = conn.execute(
        "UPDATE orders SET status=?, ts_updated=? WHERE order_id=?",
        (status_value, now_ts, order_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def append_fill(
    order_id: str,
    t: float,
    qty: Decimal,
    px: Decimal,
    fee_usd: Decimal | str = Decimal("0"),
    realized_pnl_usd: Decimal | str = Decimal("0"),
) -> bool:
    conn = _ensure_conn()
    qty_value = _to_decimal(qty)
    px_value = _to_decimal(px)
    fee_value = _to_decimal(fee_usd)
    pnl_value = _to_decimal(realized_pnl_usd)
    cursor = conn.execute(
        """
        INSERT INTO fills(order_id, t, qty, px, fee_usd, realized_pnl_usd)
        VALUES(?,?,?,?,?,?)
        """,
        (
            order_id,
            float(t),
            str(qty_value),
            str(px_value),
            str(fee_value),
            str(pnl_value),
        ),
    )
    conn.execute(
        "UPDATE orders SET ts_updated=? WHERE order_id=?",
        (time.time(), order_id),
    )
    conn.commit()
    return cursor.rowcount > 0


def snapshot_positions() -> Dict[tuple[str, str], Dict[str, Decimal]]:
    conn = _ensure_conn()
    cursor = conn.execute(
        """
        SELECT o.venue, o.symbol, o.side, f.qty, f.px
        FROM fills AS f
        JOIN orders AS o ON o.order_id = f.order_id
        """
    )
    aggregates: Dict[tuple[str, str], Dict[str, Decimal]] = {}
    for venue, symbol, side, qty_str, px_str in cursor.fetchall():
        qty = _to_decimal(qty_str)
        px = _to_decimal(px_str)
        key = (str(venue), str(symbol))
        store = aggregates.setdefault(
            key,
            {
                "net_qty": Decimal("0"),
                "_abs_qty": Decimal("0"),
                "_notional": Decimal("0"),
            },
        )
        side_key = str(side).strip().upper()
        sign = Decimal("1") if side_key in {"BUY", "BID", "LONG"} else Decimal("-1")
        store["net_qty"] += sign * qty
        abs_qty = qty.copy_abs()
        store["_abs_qty"] += abs_qty
        store["_notional"] += abs_qty * px
    snapshot: Dict[tuple[str, str], Dict[str, Decimal]] = {}
    for key, payload in aggregates.items():
        abs_qty = payload["_abs_qty"]
        if abs_qty == 0:
            vwap = Decimal("0")
        else:
            vwap = payload["_notional"] / abs_qty
        snapshot[key] = {
            "net_qty": payload["net_qty"],
            "vwap": vwap,
        }
    return snapshot


def realized_pnl_day(day_key: str) -> Decimal:
    conn = _ensure_conn()
    cursor = conn.execute("SELECT t, realized_pnl_usd FROM fills")
    total = Decimal("0")
    for ts_value, pnl_str in cursor.fetchall():
        try:
            ts_float = float(ts_value)
        except (TypeError, ValueError):
            continue
        current_key = time.strftime("%Y-%m-%d", time.gmtime(ts_float))
        if current_key != day_key:
            continue
        total += _to_decimal(pnl_str)
    return total


def get_stale_pending(now: float, min_age_sec: int) -> list[str]:
    conn = _ensure_conn()
    cutoff = float(now) - float(min_age_sec)
    cursor = conn.execute(
        "SELECT order_id FROM orders WHERE status=? AND ts_updated <= ?",
        ("PENDING", cutoff),
    )
    return [row[0] for row in cursor.fetchall() if row[0]]


def fetch_orders_status() -> Dict[str, str]:
    conn = _ensure_conn()
    cursor = conn.execute("SELECT order_id, status FROM orders")
    return {str(order_id): str(status) for order_id, status in cursor.fetchall()}


__all__ = [
    "init_db",
    "upsert_order_begin",
    "mark_order_acked",
    "mark_order_final",
    "append_fill",
    "snapshot_positions",
    "realized_pnl_day",
    "get_stale_pending",
    "fetch_orders_status",
]
