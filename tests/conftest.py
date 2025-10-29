from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("APPROVE_TOKEN", "pytest-approve")
os.environ.setdefault("AUTH_ENABLED", "false")
os.environ.setdefault("RUNTIME_STATE_PATH", "/tmp/propbot-tests-runtime.json")
os.environ.setdefault("POSITIONS_STORE_PATH", "/tmp/propbot-tests-positions.json")
os.environ.setdefault("HEDGE_LOG_PATH", "/tmp/propbot-tests-hedge-log.json")
os.environ.setdefault("OPS_ALERTS_FILE", "/tmp/propbot-tests-ops-alerts.json")
os.environ.setdefault("PNL_HISTORY_PATH", "/tmp/propbot-tests-pnl.json")
os.environ.setdefault("OPS_APPROVALS_FILE", "/tmp/propbot-tests-approvals.json")
os.environ.setdefault("DAILY_REPORTS_PATH", "/tmp/propbot-tests-daily.json")
os.environ.setdefault("CAPITAL_STATE_PATH", "/tmp/propbot-tests-capital.json")

from app import ledger
from app.main import app
from app.services import runtime, approvals_store
from app.services.loop import hold_loop
from app.services.runtime import reset_for_tests
from positions import reset_positions
from pnl_history_store import reset_store as reset_pnl_history_store
from app.capital_manager import CapitalManager, reset_capital_manager


@pytest.fixture
def client(monkeypatch) -> TestClient:
    reset_for_tests()
    runtime.record_resume_request("tests_bootstrap", requested_by="pytest")
    runtime.approve_resume(actor="pytest")
    ledger.reset()
    monkeypatch.setattr(
        "services.opportunity_scanner.check_spread",
        lambda symbol: {
            "cheap": "binance",
            "expensive": "okx",
            "spread": 10.0,
            "spread_bps": 15.0,
        },
    )
    # ensure background loop is not running between tests
    try:
        import asyncio

        asyncio.run(hold_loop())
    except RuntimeError:
        # event loop already running in this thread; skip explicit hold
        pass
    return TestClient(app)


@pytest.fixture(autouse=True)
def reset_auth_env(monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    monkeypatch.delenv("API_TOKEN", raising=False)
    monkeypatch.delenv("IDEM_TTL_SEC", raising=False)
    monkeypatch.delenv("API_RATE_PER_MIN", raising=False)
    monkeypatch.delenv("API_BURST", raising=False)
    monkeypatch.setenv("TELEGRAM_ENABLE", "false")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.delenv("OPS_ALERTS_DIR", raising=False)


@pytest.fixture(autouse=True)
def reset_idem_and_rate_limiters():
    cache = getattr(app.state, "idempotency_cache", None)
    if cache is not None:
        cache.clear()
    limiter = getattr(app.state, "rate_limiter", None)
    if limiter is not None:
        limiter.set_clock(time.monotonic)
        default_limits = getattr(app.state, "default_rate_limits", (limiter.rate_per_min, limiter.burst))
        limiter.set_limits(*default_limits)


@pytest.fixture(autouse=True)
def override_positions_store(monkeypatch, tmp_path: Path):
    path = tmp_path / "hedge_positions.json"
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(path))
    reset_positions()
    yield
    reset_positions()


@pytest.fixture(autouse=True)
def override_runtime_and_logs(monkeypatch, tmp_path: Path):
    runtime_path = tmp_path / "runtime_state.json"
    hedge_log = tmp_path / "hedge_log.json"
    alerts_path = tmp_path / "ops_alerts.json"
    daily_reports_path = tmp_path / "daily_reports.json"
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(runtime_path))
    monkeypatch.setenv("HEDGE_LOG_PATH", str(hedge_log))
    monkeypatch.setenv("OPS_ALERTS_FILE", str(alerts_path))
    monkeypatch.setenv("DAILY_REPORTS_PATH", str(daily_reports_path))
    yield


@pytest.fixture(autouse=True)
def override_pnl_history_store(monkeypatch, tmp_path: Path):
    path = tmp_path / "pnl_history.json"
    monkeypatch.setenv("PNL_HISTORY_PATH", str(path))
    reset_pnl_history_store()
    yield
    reset_pnl_history_store()


@pytest.fixture(autouse=True)
def override_approvals_store(monkeypatch, tmp_path: Path):
    path = tmp_path / "ops_approvals.json"
    monkeypatch.setenv("OPS_APPROVALS_FILE", str(path))
    approvals_store.reset_for_tests()
    yield
    approvals_store.reset_for_tests()


@pytest.fixture(autouse=True)
def override_capital_state(monkeypatch, tmp_path: Path):
    path = tmp_path / "capital_state.json"
    monkeypatch.setenv("CAPITAL_STATE_PATH", str(path))
    reset_capital_manager(CapitalManager(state_path=path))
    yield
    reset_capital_manager()
