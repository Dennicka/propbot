from __future__ import annotations

from pathlib import Path

import pytest

from app import startup_validation


def _setup_common_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(tmp_path / "runtime.json"))
    monkeypatch.setenv("POSITIONS_STORE_PATH", str(tmp_path / "positions.json"))
    monkeypatch.setenv("HEDGE_LOG_PATH", str(tmp_path / "hedge.json"))
    monkeypatch.setenv("OPS_ALERTS_FILE", str(tmp_path / "alerts.json"))
    monkeypatch.setenv("PNL_HISTORY_PATH", str(tmp_path / "pnl_history.json"))
    monkeypatch.setenv("OPS_APPROVALS_FILE", str(tmp_path / "approvals.json"))
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "3")
    monkeypatch.setenv("MAX_NOTIONAL_PER_POSITION_USDT", "1000")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "5000")
    monkeypatch.setenv("MAX_LEVERAGE", "3")


def test_validate_startup_passes_with_safe_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_common_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPROVE_TOKEN", "approve-token")
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "api-token")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("SAFE_MODE", "true")
    monkeypatch.setenv("TELEGRAM_ENABLE", "false")
    monkeypatch.setenv("AUTO_HEDGE_ENABLED", "false")

    startup_validation.validate_startup()


def test_validate_startup_requires_approve_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_common_env(monkeypatch, tmp_path)
    monkeypatch.delenv("APPROVE_TOKEN", raising=False)
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("SAFE_MODE", "true")

    with pytest.raises(SystemExit) as exc_info:
        startup_validation.validate_startup()

    message = str(exc_info.value)
    assert "APPROVE_TOKEN" in message
    assert "секрет" in message


def test_validate_startup_rejects_live_boot_without_hold(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_common_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPROVE_TOKEN", "approve-token")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "false")
    monkeypatch.setenv("SAFE_MODE", "false")

    with pytest.raises(SystemExit) as exc_info:
        startup_validation.validate_startup()

    message = str(exc_info.value)
    assert "DRY_RUN_MODE=false" in message
    assert "/resume-confirm" in message


def test_validate_startup_rejects_unwritable_runtime_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_common_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPROVE_TOKEN", "approve-token")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("SAFE_MODE", "true")

    unwritable_path = Path("/proc/startup_validation/runtime.json")
    monkeypatch.setenv("RUNTIME_STATE_PATH", str(unwritable_path))

    with pytest.raises(SystemExit) as exc_info:
        startup_validation.validate_startup()

    message = str(exc_info.value)
    assert "RUNTIME_STATE_PATH" in message
    assert "/proc" in message


def test_validate_startup_detects_placeholder_values(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_common_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPROVE_TOKEN", "approve-token")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("SAFE_MODE", "true")
    monkeypatch.setenv("API_TOKEN", "change-me")

    with pytest.raises(SystemExit) as exc_info:
        startup_validation.validate_startup()

    message = str(exc_info.value).lower()
    assert "api_token" in message
    assert "плейсхолдер" in message


def test_validate_startup_requires_persistent_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _setup_common_env(monkeypatch, tmp_path)
    monkeypatch.setenv("APPROVE_TOKEN", "approve-token")
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("DRY_RUN_MODE", "true")
    monkeypatch.setenv("SAFE_MODE", "true")
    monkeypatch.delenv("PNL_HISTORY_PATH", raising=False)

    with pytest.raises(SystemExit) as exc_info:
        startup_validation.validate_startup()

    message = str(exc_info.value)
    assert "PNL_HISTORY_PATH" in message
    assert "persistent" in message
