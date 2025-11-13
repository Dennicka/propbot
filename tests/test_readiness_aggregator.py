from app.health.aggregator import HealthAggregator


def test_is_ready_false_when_required_missing() -> None:
    agg = HealthAggregator()
    ok, reason = agg.is_ready(now=100.0)
    assert not ok
    assert reason == "readiness-missing:adapters,market,recon"


def test_is_ready_reports_bad_signals() -> None:
    agg = HealthAggregator(required={"market", "recon"})
    agg.set("market", True, now=1.0)
    agg.set("recon", False, reason="lag", now=1.0)
    ok, reason = agg.is_ready(now=2.0)
    assert not ok
    assert reason == "readiness-bad:recon:lag"


def test_is_ready_respects_ttl() -> None:
    agg = HealthAggregator(ttl_seconds=5, required={"market"})
    agg.set("market", True, now=10.0)
    ok, reason = agg.is_ready(now=14.0)
    assert ok
    assert reason == "ok"
    ok, reason = agg.is_ready(now=16.1)
    assert not ok
    assert reason == "readiness-missing:market"
