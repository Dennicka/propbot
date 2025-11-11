import json
from pathlib import Path

import pytest

from app.capital_manager import CapitalManager, reset_capital_manager
from app.strategy_orchestrator import StrategyOrchestrator, reset_strategy_orchestrator
from positions import create_position


def _configure_secrets(monkeypatch, tmp_path: Path) -> None:
    secrets_path = tmp_path / "secrets.json"
    secrets_payload = {
        "operator_tokens": {
            "alice": {"token": "operator-token", "role": "operator"},
            "bob": {"token": "viewer-token", "role": "viewer"},
        }
    }
    secrets_path.write_text(json.dumps(secrets_payload), encoding="utf-8")
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))


def test_daily_report_endpoint_operator_generates_snapshot(
    monkeypatch, client, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    _configure_secrets(monkeypatch, tmp_path)

    orchestrator = StrategyOrchestrator()
    orchestrator.bulk_enable(["alpha", "beta"], operator="pytest", role="operator")
    orchestrator.set_autopilot_active(True)
    reset_strategy_orchestrator(orchestrator)

    initial_state = {
        "total_capital_usdt": 100_000.0,
        "per_strategy_limits": {
            "alpha": {"max_notional": 10_000.0},
            "beta": {"max_notional": 5_000.0},
        },
        "current_usage": {"alpha": {"open_notional": 3_000.0}},
    }
    manager = CapitalManager(
        state_path=tmp_path / "custom_capital.json", initial_state=initial_state
    )
    reset_capital_manager(manager)

    create_position(
        symbol="BTCUSDT",
        long_venue="binance",
        short_venue="okx",
        notional_usdt=1_500.0,
        entry_spread_bps=12.0,
        leverage=5.0,
        status="open",
    )
    create_position(
        symbol="ETHUSDT",
        long_venue="binance",
        short_venue="okx",
        notional_usdt=500.0,
        entry_spread_bps=8.0,
        leverage=3.0,
        status="open",
    )

    response = client.post(
        "/api/ui/report/daily",
        headers={"Authorization": "Bearer operator-token"},
    )

    try:
        assert response.status_code == 200
        payload = response.json()
        assert payload["autopilot_active"] is True
        assert payload["enabled_strategies"] == ["alpha", "beta"]
        headroom = payload["per_strategy_headroom"]
        assert headroom.get("alpha", {}).get("headroom_notional") == pytest.approx(7_000.0)
        assert headroom.get("beta", {}).get("headroom_notional") == pytest.approx(5_000.0)
        assert payload["total_exposure_usdt"] > 0.0

        report_dir = Path("data/daily_reports")
        files = list(report_dir.glob("*.json"))
        assert files, "daily report file not created"
        stored = json.loads(files[0].read_text(encoding="utf-8"))
        assert stored["enabled_strategies"] == ["alpha", "beta"]
        assert stored["autopilot_active"] is True
    finally:
        reset_strategy_orchestrator()
        reset_capital_manager()


def test_daily_report_endpoint_forbidden_for_viewer(monkeypatch, client, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUTH_ENABLED", "true")
    _configure_secrets(monkeypatch, tmp_path)

    response = client.post(
        "/api/ui/report/daily",
        headers={"Authorization": "Bearer viewer-token"},
    )

    try:
        assert response.status_code == 403
        audit_log = Path("data/audit.log")
        assert audit_log.exists()
        lines = audit_log.read_text(encoding="utf-8").strip().splitlines()
        assert lines, "audit log should contain viewer rejection"
        last_entry = json.loads(lines[-1])
        assert last_entry["action"] == "report_daily_forbidden"
        assert last_entry["role"] == "viewer"
    finally:
        reset_strategy_orchestrator()
        reset_capital_manager()
