from datetime import datetime, timezone

import pytest

from app.approvals.live_toggle import LiveToggleStore


def test_create_request_sets_pending_status_and_timestamps() -> None:
    store = LiveToggleStore()
    before = datetime.now(timezone.utc)
    request = store.create_request(action="enable_live", requestor_id="user1", reason="go live")
    after = datetime.now(timezone.utc)

    assert request.status == "pending"
    assert request.requestor_id == "user1"
    assert request.approver_id is None
    assert request.resolution_reason is None
    assert request.created_at == request.updated_at
    assert before <= request.created_at <= after


def test_approve_request_enforces_two_man_rule() -> None:
    store = LiveToggleStore()
    request = store.create_request(action="enable_live", requestor_id="user1")

    with pytest.raises(PermissionError):
        store.approve_request(request_id=request.id, approver_id="user1")


def test_approve_request_from_other_user_changes_status() -> None:
    store = LiveToggleStore()
    request = store.create_request(action="enable_live", requestor_id="user1")
    original_updated = request.updated_at

    result = store.approve_request(
        request_id=request.id,
        approver_id="user2",
        resolution_reason="looks good",
    )

    assert result.status == "approved"
    assert result.approver_id == "user2"
    assert result.resolution_reason == "looks good"
    assert result.updated_at > original_updated


def test_reject_request_from_other_user_changes_status() -> None:
    store = LiveToggleStore()
    request = store.create_request(action="disable_live", requestor_id="user1")

    result = store.reject_request(
        request_id=request.id,
        approver_id="user2",
        resolution_reason="not now",
    )

    assert result.status == "rejected"
    assert result.approver_id == "user2"
    assert result.resolution_reason == "not now"


def test_effective_state_no_requests_disabled() -> None:
    store = LiveToggleStore()

    state = store.get_effective_state()

    assert state.enabled is False
    assert state.last_action is None
    assert state.last_status is None
    assert state.last_updated_at is None
    assert state.last_request_id is None
    assert state.requestor_id is None
    assert state.approver_id is None
    assert state.resolution_reason is None


def test_effective_state_last_approved_enable_wins() -> None:
    store = LiveToggleStore()
    first = store.create_request(action="disable_live", requestor_id="user1")
    store.reject_request(request_id=first.id, approver_id="ops", resolution_reason="deny")

    second = store.create_request(action="enable_live", requestor_id="user2")
    store.approve_request(request_id=second.id, approver_id="ops2", resolution_reason="ok")

    state = store.get_effective_state()

    assert state.enabled is True
    assert state.last_action == "enable_live"
    assert state.last_status == "approved"
    assert state.last_request_id == second.id
    assert state.requestor_id == "user2"
    assert state.approver_id == "ops2"
    assert state.resolution_reason == "ok"


def test_effective_state_last_approved_disable_wins() -> None:
    store = LiveToggleStore()
    enable = store.create_request(action="enable_live", requestor_id="user1")
    store.approve_request(request_id=enable.id, approver_id="ops1")

    disable = store.create_request(action="disable_live", requestor_id="user1")
    store.approve_request(request_id=disable.id, approver_id="ops2", resolution_reason="off")

    state = store.get_effective_state()

    assert state.enabled is False
    assert state.last_action == "disable_live"
    assert state.last_status == "approved"
    assert state.last_request_id == disable.id
    assert state.requestor_id == "user1"
    assert state.approver_id == "ops2"
    assert state.resolution_reason == "off"
