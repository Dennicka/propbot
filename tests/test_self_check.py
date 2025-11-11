from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from app.services import self_check


def _prepare_base_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, profile: str) -> None:
    monkeypatch.setenv("PROFILE", profile)
    monkeypatch.setenv("TRADING_PROFILE", profile)
    monkeypatch.setenv("APPROVE_TOKEN", "approve-token")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("API_TOKEN", "api-token")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("SAFE_MODE", "true")
    monkeypatch.setenv("TELEGRAM_ENABLE", "false")
    monkeypatch.setenv("AUTO_HEDGE_ENABLED", "false")
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "3")
    monkeypatch.setenv("MAX_NOTIONAL_PER_POSITION_USDT", "1000")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "5000")
    monkeypatch.setenv("MAX_LEVERAGE", "3")

    monkeypatch.setenv("RUNTIME_STATE_PATH", str(tmp_path / "runtime_state.json"))
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(tmp_path / "positions.json"))
    monkeypatch.setenv("HEDGE_LOG_PATH", str(tmp_path / "hedge_log.json"))
    monkeypatch.setenv("PNL_HISTORY_PATH", str(tmp_path / "pnl_history.json"))
    monkeypatch.setenv("OPS_ALERTS_FILE", str(tmp_path / "ops_alerts.json"))
    monkeypatch.setenv("OPS_APPROVALS_FILE", str(tmp_path / "ops_approvals.json"))


@pytest.fixture(autouse=True)
def _reset_network(monkeypatch: pytest.MonkeyPatch) -> None:
    original = socket.getaddrinfo

    def fake_getaddrinfo(host: str, *_args, **_kwargs):
        return original("localhost", None)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_self_check_success_paper(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prepare_base_env(monkeypatch, tmp_path, "paper")

    report = self_check.run_self_check(profile="paper")

    assert report.profile == "paper"
    assert not report.has_failures()
    assert report.overall_status() in {self_check.CheckStatus.OK, self_check.CheckStatus.WARN}


def test_self_check_environment_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prepare_base_env(monkeypatch, tmp_path, "paper")
    monkeypatch.delenv("APPROVE_TOKEN", raising=False)

    report = self_check.run_self_check(profile="paper", skip_network=True)

    assert report.has_failures()
    messages = {result.name: result for result in report.results}
    assert messages["environment"].status is self_check.CheckStatus.FAIL
    assert "APPROVE_TOKEN" in messages["environment"].message


def test_self_check_live_requires_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _prepare_base_env(monkeypatch, tmp_path, "live")

    secrets_path = tmp_path / "secrets_store.json"
    secrets_path.write_text(
        json.dumps(
            {
                "binance_key": "binance-key",
                "binance_secret": "binance-secret",
                "okx_key": "okx-key",
                "okx_secret": "okx-secret",
                "okx_passphrase": "okx-passphrase",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SECRETS_STORE_PATH", str(secrets_path))

    report = self_check.run_self_check(profile="live", skip_network=True)

    assert not report.has_failures()
    profile_messages = {result.name: result for result in report.results}
    assert profile_messages["profile.config"].status is self_check.CheckStatus.OK


def test_self_check_main_exit_code(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
    _prepare_base_env(monkeypatch, tmp_path, "paper")
    monkeypatch.delenv("APPROVE_TOKEN", raising=False)

    code = self_check.main(["--profile", "paper", "--skip-network"])

    assert code == 1
    captured = capsys.readouterr()
    assert "Overall: FAIL" in captured.out
