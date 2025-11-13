from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from app.outbox.journal import OutboxJournal


def test_journal_write_and_reload(monkeypatch, tmp_path) -> None:
    outbox_path = tmp_path / "outbox.jsonl"
    monkeypatch.setenv("OUTBOX_PATH", str(outbox_path))
    journal = OutboxJournal(str(outbox_path), rotate_mb=8, flush_every=1)

    journal.begin_pending(
        intent_key="intent-1",
        order_id="order-1",
        strategy="alpha",
        symbol="BTCUSDT",
        venue="binance",
        side="buy",
        qty=Decimal("1.25"),
        px=Decimal("100.5"),
    )

    last = journal.last_by_intent("intent-1")
    assert last is not None
    _, status, order_id = last
    assert status == "PENDING"
    assert order_id == "order-1"
    assert journal.status_by_order("order-1") == "PENDING"

    journal.mark_acked("order-1", "exch-1")

    reloaded = OutboxJournal(str(outbox_path), rotate_mb=8, flush_every=1)
    assert reloaded.status_by_order("order-1") == "ACKED"

    reloaded.mark_final("order-1", reason="filled")
    assert reloaded.status_by_order("order-1") == "FINAL"


def test_journal_rotation(tmp_path) -> None:
    outbox_path = tmp_path / "outbox.jsonl"
    journal = OutboxJournal(str(outbox_path), rotate_mb=0, flush_every=1)

    for index in range(3):
        order_id = f"order-{index}"
        intent_key = f"intent-{index}"
        journal.begin_pending(
            intent_key=intent_key,
            order_id=order_id,
            strategy="beta",
            symbol="ETHUSDT",
            venue="okx",
            side="sell",
            qty=Decimal("0.5"),
            px=Decimal("99.1"),
        )
        journal.mark_final(order_id, reason="canceled")

    rotated = list(Path(outbox_path.parent).glob(outbox_path.name + ".*"))
    assert rotated
