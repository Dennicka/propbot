from __future__ import annotations

import time

from app.services import runtime
from app.routers import health


def test_healthz(client, monkeypatch):
    original = health._is_task_running
    auto_task = getattr(client.app.state.auto_hedge_daemon, "_task", None)
    scanner_task = getattr(client.app.state.opportunity_scanner, "_task", None)

    def _fake_task_running(task) -> bool:
        if task is auto_task or task is scanner_task:
            return True
        return original(task)

    monkeypatch.setattr(health, "_is_task_running", _fake_task_running)
    response = client.get("/healthz")
    attempts = 0
    while response.status_code != 200 and attempts < 5:
        time.sleep(0.05)
        response = client.get("/healthz")
        attempts += 1
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "journal_ok": True, "resume_ok": True}


def test_healthz_detects_auto_hedge_failure(client, monkeypatch):
    runtime.update_auto_hedge_state(enabled=True, last_execution_result="error: auto")
    daemon = client.app.state.auto_hedge_daemon
    scanner_task = getattr(client.app.state.opportunity_scanner, "_task", None)
    original = health._is_task_running
    monkeypatch.setattr(health, "_scanner_healthy", lambda app: True)

    def _fake_task_running(task) -> bool:
        if task is getattr(daemon, "_task", None):
            return False
        if task is scanner_task:
            return True
        return original(task)

    monkeypatch.setattr(health, "_is_task_running", _fake_task_running)

    response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["journal_ok"] is True
    assert body["resume_ok"] is True


def test_healthz_ok_when_auto_hedge_disabled(client, monkeypatch):
    runtime.update_auto_hedge_state(enabled=False, last_execution_result="disabled")
    daemon = client.app.state.auto_hedge_daemon
    scanner_task = getattr(client.app.state.opportunity_scanner, "_task", None)
    original = health._is_task_running
    monkeypatch.setattr(health, "_scanner_healthy", lambda app: True)

    def _fake_task_running(task) -> bool:
        if task is getattr(daemon, "_task", None):
            return False
        if task is scanner_task:
            return True
        return original(task)

    monkeypatch.setattr(health, "_is_task_running", _fake_task_running)

    response = client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body == {"ok": True, "journal_ok": True, "resume_ok": True}


def test_healthz_detects_scanner_error(client):
    runtime.set_last_opportunity_state(None, "error: scanner")

    response = client.get("/healthz")
    assert response.status_code == 503
    body = response.json()
    assert body["ok"] is False
    assert body["journal_ok"] is True
    assert body["resume_ok"] is True
