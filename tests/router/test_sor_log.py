from datetime import datetime, timedelta, timezone
from decimal import Decimal

from app.router.sor_log import (
    RouterDecisionLogEntry,
    append_router_decision,
    get_recent_router_decisions,
    reset_router_decisions_for_tests,
)


def _make_entry(symbol: str, offset: int) -> RouterDecisionLogEntry:
    return RouterDecisionLogEntry(
        ts=datetime(2024, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=offset),
        symbol=symbol,
        strategy_id="strategy",
        runtime_profile="paper",
        candidates=(),
        chosen_venue_id="binance",
        chosen_score=Decimal("1"),
        reject_reason=None,
    )


def test_append_and_get_recent_decisions_ordering() -> None:
    reset_router_decisions_for_tests()
    first = _make_entry("BTCUSDT", 0)
    second = _make_entry("ETHUSDT", 1)

    append_router_decision(first)
    append_router_decision(second)

    recent = get_recent_router_decisions(limit=2)

    assert [entry.symbol for entry in recent] == ["ETHUSDT", "BTCUSDT"]


def test_ring_buffer_drops_old_entries() -> None:
    reset_router_decisions_for_tests()
    for idx in range(510):
        append_router_decision(_make_entry(f"SYM{idx}", idx))

    recent = get_recent_router_decisions(limit=9999)

    assert len(recent) == 500
    assert recent[0].symbol == "SYM509"
    assert recent[-1].symbol == "SYM10"
