"""Restart-resume helpers for FEATURE_JOURNAL."""
from __future__ import annotations

import logging
import uuid
from typing import Any, Dict, List, Tuple

from . import ledger
from .journal import is_enabled as journal_enabled
from .journal import order_journal
from .services.runtime import set_open_orders, set_positions_state

LOGGER = logging.getLogger(__name__)


def _summarise_orders(orders: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "id": int(order.get("id", 0)),
            "venue": order.get("venue"),
            "symbol": order.get("symbol"),
            "side": order.get("side"),
            "qty": order.get("qty"),
            "status": order.get("status"),
        }
        for order in orders
    ]


def _summarise_positions(positions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [
        {
            "venue": position.get("venue"),
            "symbol": position.get("symbol"),
            "base_qty": position.get("base_qty"),
            "avg_price": position.get("avg_price"),
            "ts": position.get("ts"),
        }
        for position in positions
    ]


def perform_resume() -> Tuple[bool, Dict[str, Any]]:
    """Load ledger state into runtime and persist a RESUMED journal entry."""
    if not journal_enabled():
        return True, {"enabled": False}
    try:
        open_orders = ledger.fetch_open_orders()
        positions = ledger.fetch_positions()
    except Exception as exc:  # pragma: no cover - defensive logging
        LOGGER.exception("failed to load ledger state for resume")
        return False, {"error": str(exc)}

    set_open_orders(open_orders)
    set_positions_state(positions)

    orders_summary = _summarise_orders(open_orders)
    positions_summary = _summarise_positions(positions)
    payload = {
        "status": "RESUMED",
        "open_orders": orders_summary,
        "positions": positions_summary,
    }
    entry = order_journal.append(
        {
            "uuid": str(uuid.uuid4()),
            "type": "restart.resume",
            "payload": payload,
        }
    )
    ledger.record_event(
        level="INFO",
        code="restart_resume",
        payload={
            "status": "RESUMED",
            "open_orders": len(orders_summary),
            "positions": len(positions_summary),
        },
    )
    payload["entry_id"] = entry.get("id")
    return True, payload


__all__ = ["perform_resume"]
