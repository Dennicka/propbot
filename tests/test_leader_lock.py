from app.runtime import leader_lock


def test_leader_lock_acquire_and_release(monkeypatch):
    leader_lock.reset_for_tests()
    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "alpha")
    assert leader_lock.acquire() is True
    assert leader_lock.is_leader() is True

    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "beta")
    assert leader_lock.acquire() is False
    assert leader_lock.is_leader() is False

    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "alpha")
    assert leader_lock.release() is True
    assert leader_lock.is_leader() is False
