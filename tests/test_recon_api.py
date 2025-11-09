from __future__ import annotations

from datetime import datetime, timezone

import pytest

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
                "severity": "OK",
            }
        ],
        desync_detected=True,
        last_checked=timestamp,
        metadata={"auto_hold": True, "has_warn": False, "has_crit": False},
    )

    response = client.get("/api/ui/recon_status")
    assert response.status_code == 200
    payload = response.json()

    assert payload["has_warn"] is False
    assert payload["has_crit"] is False
    assert len(payload["diffs"]) == 1
    diff = payload["diffs"][0]
    assert diff["venue"] == "binance-um"
    assert diff["symbol"] == "BTCUSDT"
    assert diff["diff_abs"] == pytest.approx(30_000.0)
    assert diff["severity"] == "OK"


def test_recon_status_endpoint_returns_snapshot(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setattr(runtime, "_persist_safety_snapshot", lambda *_args, **_kwargs: None)

    timestamp = "2024-03-15T12:00:00+00:00"
    runtime.update_reconciliation_status(
        diffs=[
            {
                "venue": "okx-perp",
                "symbol": "ETHUSDT",
                "delta": 1.0,
                "notional_usd": 100.0,
                "diff_rel": 0.1,
                "severity": "WARN",
            }
        ],
        desync_detected=True,
        last_checked=timestamp,
        metadata={"auto_hold": False, "has_warn": True, "has_crit": False},
    )

    response = client.get("/api/ui/recon_status")
    assert response.status_code == 200
    payload = response.json()

    assert payload["has_warn"] is True
    assert payload["has_crit"] is False
    assert len(payload["diffs"]) == 1
    diff = payload["diffs"][0]
    assert diff["venue"] == "okx-perp"
    assert diff["symbol"] == "ETHUSDT"
    assert diff["severity"] == "WARN"
    assert diff["diff_rel"] == pytest.approx(0.1)
