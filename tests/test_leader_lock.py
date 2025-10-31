import os

import os

from app.runtime import leader_lock


def test_leader_lock_acquire_and_release(monkeypatch):
    leader_lock.reset_for_tests()
    monkeypatch.setenv("FEATURE_LEADER_LOCK", "1")
    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "alpha")
    assert leader_lock.acquire() is True
    assert leader_lock.is_leader() is True

    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "beta")
    assert leader_lock.acquire() is False
    assert leader_lock.is_leader() is False

    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "alpha")
    assert leader_lock.release() is True
    assert leader_lock.is_leader() is False


def test_leader_lock_heartbeat_and_stale_takeover(monkeypatch):
    leader_lock.reset_for_tests()
    base_time = 1_000.0
    monkeypatch.setenv("FEATURE_LEADER_LOCK", "1")
    monkeypatch.setenv("LEADER_LOCK_TTL_SEC", "10")
    monkeypatch.setenv("LEADER_LOCK_STALE_SEC", "5")

    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "primary")
    monkeypatch.setattr(leader_lock, "_INSTANCE_ID", None)
    monkeypatch.setattr(leader_lock.time, "time", lambda: base_time)
    assert leader_lock.acquire(now=base_time) is True
    status_primary = leader_lock.get_status(now=base_time)
    primary_fencing = status_primary.get("fencing_id")
    assert primary_fencing
    heartbeat = leader_lock.last_heartbeat()
    assert heartbeat.fencing_id == primary_fencing
    assert heartbeat.pid == os.getpid()

    # Another instance cannot steal before the heartbeat is stale.
    monkeypatch.setenv("LEADER_LOCK_INSTANCE_ID", "secondary")
    monkeypatch.setattr(leader_lock, "_INSTANCE_ID", None)
    fresh_time = base_time + 2
    monkeypatch.setattr(leader_lock.time, "time", lambda: fresh_time)
    assert leader_lock.acquire(now=fresh_time) is False
    assert leader_lock.is_leader(now=fresh_time) is False

    # After staleness window, takeover succeeds with a new fencing id.
    stale_time = base_time + 12
    monkeypatch.setattr(leader_lock, "_INSTANCE_ID", None)
    monkeypatch.setattr(leader_lock.time, "time", lambda: stale_time)
    assert leader_lock.acquire(now=stale_time) is True
    status_secondary = leader_lock.get_status(now=stale_time)
    secondary_fencing = status_secondary.get("fencing_id")
    assert secondary_fencing and secondary_fencing != primary_fencing
    heartbeat_secondary = leader_lock.last_heartbeat()
    assert heartbeat_secondary.fencing_id == secondary_fencing
