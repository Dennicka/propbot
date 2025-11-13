from __future__ import annotations

import json
import time
from decimal import Decimal

import pytest

from app.db import ledger
from app.metrics import core as metrics_core
from app.outbox.journal import OutboxJournal
from app.recon.engine import run_recon


def test_recon_engine_detects_inconsistencies(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    db_path = tmp_path / "ledger.db"
    report_path = tmp_path / "report.json"
    outbox_path = tmp_path / "outbox.jsonl"

    monkeypatch.setenv("LEDGER_DB_PATH", str(db_path))
    monkeypatch.setenv("RECON_REPORT_PATH", str(report_path))
    monkeypatch.setenv("RECON_FAIL_AGE_SEC", "5")
    monkeypatch.setenv("OUTBOX_PATH", str(outbox_path))

    ledger.init_db(str(db_path))

    now = time.time()

    ledger.upsert_order_begin(
        {
            "order_id": "order-pending",
            "intent_key": "intent-pending",
            "strategy": "strat",
            "symbol": "ETHUSDT",
            "venue": "binance",
            "side": "BUY",
            "qty": Decimal("1"),
            "px": Decimal("1000"),
        }
    )

    ledger.upsert_order_begin(
        {
            "order_id": "order-acked",
            "intent_key": "intent-acked",
            "strategy": "strat",
            "symbol": "BTCUSDT",
            "venue": "binance",
            "side": "SELL",
            "qty": Decimal("0.5"),
            "px": Decimal("20000"),
        }
    )
    ledger.mark_order_acked("order-acked", "exch-acked")

    ledger.upsert_order_begin(
        {
            "order_id": "order-final",
            "intent_key": "intent-final",
            "strategy": "strat",
            "symbol": "BNBUSDT",
            "venue": "binance",
            "side": "BUY",
            "qty": Decimal("2"),
            "px": Decimal("300"),
        }
    )
    ledger.mark_order_final("order-final", "FILLED")

    journal = OutboxJournal(str(outbox_path), rotate_mb=0, flush_every=1)
    journal.begin_pending(
        intent_key="intent-acked",
        order_id="order-acked",
        strategy="strat",
        symbol="BTCUSDT",
        venue="binance",
        side="sell",
        qty=Decimal("0.5"),
        px=Decimal("20000"),
    )
    journal.mark_final("order-acked", reason="filled")

    report = run_recon(now + 20.0)

    assert report["counts"]["PENDING"] >= 1
    kinds = {item["kind"] for item in report["issues"]}
    assert "pending-stale" in kinds
    assert "mismatch-final" in kinds

    assert report_path.exists()
    with report_path.open("r", encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved == report

    gauge = metrics_core.gauge("propbot_recon_pending_stale")
    gauge_value = gauge._values.get((), 0.0)  # type: ignore[attr-defined]
    assert gauge_value >= 1.0
