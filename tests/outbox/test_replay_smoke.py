from __future__ import annotations

import time
from decimal import Decimal

from app.outbox.journal import OutboxJournal


def test_replay_candidates(monkeypatch, tmp_path) -> None:
    outbox_path = tmp_path / "outbox.jsonl"
    journal = OutboxJournal(str(outbox_path), rotate_mb=8, flush_every=1)

    real_time = time.time
    now_value = real_time()
    old_value = now_value - 10.0

    monkeypatch.setattr("app.outbox.journal.time.time", lambda: old_value)

    journal.begin_pending(
        intent_key="intent-a",
        order_id="order-a",
        strategy="gamma",
        symbol="BTCUSDT",
        venue="binance",
        side="buy",
        qty=Decimal("0.4"),
        px=Decimal("101.0"),
    )
    journal.begin_pending(
        intent_key="intent-b",
        order_id="order-b",
        strategy="gamma",
        symbol="ETHUSDT",
        venue="okx",
        side="sell",
        qty=Decimal("0.2"),
        px=Decimal("99.5"),
    )

    monkeypatch.setattr("app.outbox.journal.time.time", real_time)

    candidates = list(journal.iter_replay_candidates(now=now_value, min_age_sec=5))
    assert {candidate.order_id for candidate in candidates} == {"order-a", "order-b"}

    journal.mark_acked("order-a")
    journal.mark_final("order-a", reason="filled")
    journal.mark_acked("order-b")
    journal.mark_final("order-b", reason="canceled")

    replay_again = list(journal.iter_replay_candidates(now=now_value, min_age_sec=5))
    assert replay_again == []
