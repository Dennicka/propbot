from __future__ import annotations

from app.services import runtime
from app.routers import health


def _set_health(monkeypatch, **fields):
    payload = {"ok": True, "journal_ok": True, "resume_ok": True, "leader": True, "config_ok": True}
    payload.update(fields)
    monkeypatch.setattr(health, "evaluate_health", lambda _app: payload)


def test_healthz(client, monkeypatch):
    _set_health(monkeypatch)
    response = client.get("/healthz")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["journal_ok"] is True
    assert payload["resume_ok"] is True
    assert payload["leader"] is True
    assert payload["config_ok"] is True
    watchdog_block = payload.get("watchdog")
    assert isinstance(watchdog_block, dict)
    assert set(watchdog_block.get("components", {})) >= {
        "router",
        "recon",
        "ledger",
        "marketdata",
    }


def test_healthz_detects_auto_hedge_failure(client, monkeypatch):
    runtime.update_auto_hedge_state(enabled=True, last_execution_result="error: auto")
    _set_health(monkeypatch, ok=False)
    response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["journal_ok"] is True
    assert body["resume_ok"] is True


def test_healthz_ok_when_auto_hedge_disabled(client, monkeypatch):
    runtime.update_auto_hedge_state(enabled=False, last_execution_result="disabled")
    _set_health(monkeypatch)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_healthz_detects_scanner_error(client, monkeypatch):
    runtime.set_last_opportunity_state(None, "error: scanner")
    _set_health(monkeypatch, ok=False)
    response = client.get("/healthz")
    assert response.status_code == 503
