from __future__ import annotations

from datetime import datetime, timezone

from app.services import runtime


def test_recon_status_endpoint(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(runtime, "_persist_safety_snapshot", lambda *_args, **_kwargs: None)

    timestamp = datetime.now(timezone.utc).isoformat()
    runtime.update_reconciliation_status(
        diffs=[
            {
                "venue": "binance-um",
                "symbol": "BTCUSDT",
                "exch_qty": 2.0,
                "ledger_qty": 1.0,
                "delta": 1.0,
                "notional_usd": 30_000.0,
            }
        ],
        desync_detected=True,
        last_checked=timestamp,
        metadata={"auto_hold": True},
    )

    response = client.get("/api/ui/recon/status")
    assert response.status_code == 200
    payload = response.json()

    assert payload["desync_detected"] is True
    assert payload["auto_hold"] is True
    assert payload["diff_count"] == 1
    assert payload["last_checked"] == timestamp
    assert payload["diffs"][0]["venue"] == "binance-um"
    assert payload["diffs"][0]["notional_usd"] == 30000.0
