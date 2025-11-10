from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.recon.daemon import DaemonConfig, ReconDaemon, run_recon_cycle


def _daemon_config() -> DaemonConfig:
    return DaemonConfig(
        enabled=True,
        interval_sec=1.0,
        epsilon_position=Decimal("0.0001"),
        epsilon_balance=Decimal("0.5"),
        epsilon_notional=Decimal("1.0"),
        auto_hold_on_critical=True,
        balance_warn_usd=Decimal("5"),
        balance_critical_usd=Decimal("25"),
        position_size_warn=Decimal("0.1"),
        position_size_critical=Decimal("0.5"),
        order_critical_missing=True,
    )


@pytest.mark.asyncio
async def test_recon_cycle_sets_hold_on_critical_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    safety = SimpleNamespace(hold_active=False, hold_reason=None)
    state = SimpleNamespace(derivatives=None, safety=safety)
    monkeypatch.setattr("app.recon.daemon.runtime.get_state", lambda: state)

    context = SimpleNamespace(
        cfg=SimpleNamespace(recon=_daemon_config()),
        state=state,
        local_positions=lambda: [{"venue": "binance", "symbol": "BTCUSDT", "qty": Decimal("0.0")}],
        remote_positions=lambda: [{"venue": "binance", "symbol": "BTCUSDT", "qty": Decimal("1.0")}],
        local_balances=lambda: [],
        remote_balances=lambda: [],
        local_orders=lambda: [],
        remote_orders=lambda: [],
    )

    async def fake_build_context(self, _state):
        return context

    monkeypatch.setattr("app.recon.daemon.ReconDaemon._build_context", fake_build_context)

    holds: dict[str, str] = {}

    def engage(reason: str, *, source: str) -> bool:
        holds["reason"] = reason
        holds["source"] = source
        return True

    metadata_calls: list[dict[str, object]] = []

    def update_status(**kwargs) -> None:
        metadata_calls.append(kwargs.get("metadata", {}))

    monkeypatch.setattr("app.recon.daemon.runtime.engage_safety_hold", engage)
    monkeypatch.setattr("app.recon.daemon.runtime.update_reconciliation_status", update_status)

    daemon = ReconDaemon(_daemon_config())
    result = await daemon.run_once()

    assert holds["reason"] == "RECON_DIVERGENCE"
    assert holds["source"] == "recon"
    assert metadata_calls
    last_meta = metadata_calls[-1]
    assert last_meta.get("status") == "CRITICAL"
    assert last_meta.get("auto_hold") is True
    assert result.issues and result.issues[0].severity == "CRITICAL"


def test_recon_cycle_logs_and_metrics_for_warn(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    cfg = _daemon_config()
    cfg.auto_hold_on_critical = False
    state = SimpleNamespace(hold_active=False, hold_reason=None)

    metrics_calls: list[tuple[str, str]] = []

    class _Counter:
        def __init__(self, label):
            self.label = label

        def inc(self):
            metrics_calls.append(self.label)

    class _CounterFactory:
        def labels(self, **labels):
            label = (labels.get("kind"), labels.get("severity"))
            return _Counter(label)

    monkeypatch.setattr("app.recon.daemon.RECON_DRIFT_TOTAL", _CounterFactory())
    monkeypatch.setattr("app.recon.daemon.RECON_ISSUES_TOTAL", _CounterFactory())
    monkeypatch.setattr("app.recon.daemon.runtime.update_reconciliation_status", lambda **_: None)
    monkeypatch.setattr("app.recon.daemon.runtime.engage_safety_hold", lambda *_, **__: False)

    ctx = SimpleNamespace(
        cfg=SimpleNamespace(recon=cfg),
        state=state,
        local_positions=lambda: [],
        remote_positions=lambda: [],
        local_balances=lambda: [{"venue": "paper", "asset": "USDT", "total": Decimal("100") }],
        remote_balances=lambda: [{"venue": "paper", "asset": "USDT", "total": Decimal("110")}],
        local_orders=lambda: [],
        remote_orders=lambda: [],
    )

    with caplog.at_level("WARNING"):
        drifts = run_recon_cycle(ctx)

    assert drifts and drifts[0].severity == "WARN"
    assert any(label[1] == "WARN" for label in metrics_calls)
    assert any(
        getattr(record, "event", None) == "recon_drift"
        or "recon.drift" in getattr(record, "message", "")
        for record in caplog.records
    )
