from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.alerts import notifier
from app.alerts.notifier import Event
from app.recon import engine as recon_engine
from app.risk import pnl_caps
from app.router import smart_router


class DummyThread:
    def __init__(self, target, daemon: bool = False) -> None:
        self._target = target
        self.daemon = daemon

    def start(self) -> None:  # pragma: no cover - worker disabled
        return None


class CollectSink(notifier.Sink):
    def __init__(self) -> None:
        self.events: list[Event] = []

    def send(self, event: Event) -> bool:
        self.events.append(event)
        return True


@pytest.fixture(autouse=True)
def _reset_notifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(notifier.threading, "Thread", DummyThread)
    monkeypatch.setattr(notifier, "_NOTIFIER", None)


def test_alerts_flow(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    stdout_sink = CollectSink()
    file_sink = CollectSink()
    monkeypatch.setattr(notifier, "StdoutSink", lambda stream=None: stdout_sink)
    monkeypatch.setattr(notifier, "FileSink", lambda path: file_sink)
    monkeypatch.setenv("FF_ALERTS", "1")
    monkeypatch.setenv("FF_ALERTS_TELEGRAM", "0")
    monkeypatch.setenv("ALERTS_FILE_PATH", str(tmp_path / "alerts.log"))
    monkeypatch.setenv("ALERTS_RATE_LIMIT", "100/sec")
    monkeypatch.delenv("ALERTS_INCLUDE", raising=False)
    alert_notifier = notifier.get_notifier()
    before_sent = notifier._ALERTS_SENT_TOTAL._values.get(("stdout", "ok"), 0.0)

    # Router risk block
    monkeypatch.setattr(smart_router.SafeMode, "is_active", staticmethod(lambda: False))
    monkeypatch.setattr(smart_router.ff, "risk_limits_on", lambda: False)
    monkeypatch.setattr(smart_router.ff, "pretrade_strict_on", lambda: False)
    monkeypatch.setattr(smart_router.ff, "md_watchdog_on", lambda: False)
    monkeypatch.setattr(smart_router, "get_profile", lambda: SimpleNamespace(name="demo"))
    monkeypatch.setattr(smart_router, "is_live", lambda profile: False)
    monkeypatch.setattr(smart_router.watchdog, "staleness_ms", lambda venue, symbol: 0)
    monkeypatch.setattr(smart_router.watchdog, "get_p95", lambda venue: 0)
    monkeypatch.setattr(smart_router.watchdog, "stale_p95_limit_ms", lambda: 999999)
    monkeypatch.setattr(smart_router.watchdog, "cooldown_active", lambda venue: False)
    monkeypatch.setattr(smart_router.watchdog, "activate_cooldown", lambda venue: None)
    state = SimpleNamespace(config=None)
    market_data = SimpleNamespace()
    router = smart_router.SmartRouter(state=state, market_data=market_data)
    router._pnl_guard.should_block = lambda strategy: (False, "ok")  # type: ignore[assignment]
    router._risk_governor = SimpleNamespace(
        allow_order=lambda venue, symbol, strategy, price, qty: (False, "risk-test")
    )
    response = router.register_order(
        strategy="alpha",
        venue="x",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=1.0,
        ts_ns=0,
        nonce=0,
    )
    assert response["reason"] == "risk-test"

    # Recon issues block
    monkeypatch.setattr(recon_engine.ledger, "fetch_orders_status", lambda: {"o1": "PENDING"})
    monkeypatch.setattr(recon_engine.ledger, "get_stale_pending", lambda now, age: ["o1", "o2"])
    monkeypatch.setattr(recon_engine, "_load_outbox", lambda: {})
    monkeypatch.setattr(recon_engine, "_report_path", lambda: tmp_path / "recon.json")
    report = recon_engine.run_recon(now=0.0)
    assert len(report["issues"]) == 2

    # PnL cap block
    monkeypatch.setenv("FF_DAILY_LOSS_CAP", "1")
    monkeypatch.setenv("DAILY_LOSS_CAP_USD_GLOBAL", "1")
    monkeypatch.setenv("PNL_TZ", "UTC")
    policy = pnl_caps.CapsPolicy()
    agg = pnl_caps.PnLAggregator(policy.tz)
    fixed_time = 100.0
    agg.on_fill(
        pnl_caps.FillEvent(
            t=fixed_time,
            strategy="alpha",
            symbol="BTCUSDT",
            realized_pnl_usd=Decimal("-5"),
        )
    )
    clock = SimpleNamespace(time=lambda: fixed_time)
    guard = pnl_caps.PnLCapsGuard(policy, agg, clock=clock)
    blocked, reason = guard.should_block("alpha")
    assert blocked is True
    assert "daily-loss-cap" in reason

    alert_notifier.drain_once()
    kinds = [event.kind for event in stdout_sink.events]
    assert kinds == ["router-block", "recon-issues", "pnl-cap"]
    assert len(file_sink.events) == 3
    after_sent = notifier._ALERTS_SENT_TOTAL._values.get(("stdout", "ok"), 0.0)
    assert after_sent == pytest.approx(before_sent + len(stdout_sink.events))
