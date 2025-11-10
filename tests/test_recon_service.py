from types import SimpleNamespace

from app.recon.core import ReconIssue, ReconResult
from app.recon.service import collect_recon_snapshot


def test_collect_recon_snapshot_returns_result(monkeypatch):
    issues = [
        ReconIssue(
            kind="BALANCE",
            venue="binance-um",
            symbol="USDT",
            severity="WARN",
            code="BALANCE_MISMATCH",
            details="delta",
        )
    ]
    result = ReconResult(ts=42.0, issues=issues)
    monkeypatch.setattr("app.recon.service.reconcile_once", lambda ctx=None: result)

    outcome = collect_recon_snapshot(SimpleNamespace())
    assert outcome is result
    assert outcome.issues == issues
