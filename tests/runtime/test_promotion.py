from __future__ import annotations

import pytest

from app.runtime.promotion import get_promotion_status
from app.services import runtime as runtime_module


@pytest.fixture(autouse=True)
def reset_runtime_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_module, "_PROFILE", None, raising=False)


def test_promotion_status_default_paper_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PROMOTION_STAGE", raising=False)
    monkeypatch.delenv("PROMOTION_ALLOWED_NEXT", raising=False)
    monkeypatch.setenv("EXEC_PROFILE", "paper")

    status = get_promotion_status()

    assert status.stage == "paper_only"
    assert status.is_live_profile is False
    assert "testnet_sandbox" in status.allowed_next_stages
    assert status.reason is None or "invalid" not in status.reason


def test_promotion_status_live_full_with_live_profile(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMOTION_STAGE", "live_full")
    monkeypatch.delenv("PROMOTION_ALLOWED_NEXT", raising=False)
    monkeypatch.setenv("EXEC_PROFILE", "live")

    status = get_promotion_status()

    assert status.stage == "live_full"
    assert status.is_live_profile is True
    assert list(status.allowed_next_stages) == []
    assert status.reason is None


def test_promotion_status_inconsistent_profile_and_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROMOTION_STAGE", "live_full")
    monkeypatch.delenv("PROMOTION_ALLOWED_NEXT", raising=False)
    monkeypatch.setenv("EXEC_PROFILE", "paper")

    status = get_promotion_status()

    assert status.stage == "live_full"
    assert status.is_live_profile is False
    assert status.reason is not None
    assert "runtime profile" in status.reason
