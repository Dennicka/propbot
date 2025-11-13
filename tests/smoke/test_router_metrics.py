"""Smoke tests covering router metrics integration."""

from __future__ import annotations

import re
import time
from types import SimpleNamespace

from app.router.smart_router import SmartRouter


class StubMarketData:
    def __init__(self, payload: dict[tuple[str, str], dict[str, float]]) -> None:
        self._payload = payload

    def top_of_book(self, venue: str, symbol: str) -> dict[str, float]:
        key = (venue.lower(), symbol.upper())
        book = dict(self._payload[key])
        book.setdefault("ts", time.time())
        return book


def _metric_value(payload: str, name: str, labels: dict[str, str] | None = None) -> float:
    pattern = rf"^{re.escape(name)}"
    if labels:
        label_parts = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
        pattern += rf"\{{{re.escape(label_parts)}\}}"
    pattern += r"\s+([0-9.e+-]+)$"
    for line in payload.splitlines():
        if line.startswith("#"):
            continue
        match = re.match(pattern, line)
        if match:
            return float(match.group(1))
    raise AssertionError(f"metric {name} not found")


def _setup_router(monkeypatch) -> SmartRouter:
    state = SimpleNamespace(
        control=SimpleNamespace(
            post_only=False,
            taker_fee_bps_binance=2,
            taker_fee_bps_okx=2,
            default_taker_fee_bps=2,
        ),
        config=SimpleNamespace(
            data=SimpleNamespace(
                tca=SimpleNamespace(
                    horizon_min=1.0,
                    impact=SimpleNamespace(k=0.0),
                    tiers={},
                ),
                derivatives=SimpleNamespace(
                    arbitrage=SimpleNamespace(prefer_maker=False),
                    fees=SimpleNamespace(manual={}),
                ),
            )
        ),
        derivatives=SimpleNamespace(venues={}),
    )
    market = StubMarketData(
        {
            ("binance-um", "BTCUSDT"): {"bid": 100.0, "ask": 101.0},
        }
    )
    monkeypatch.setattr("app.router.smart_router.get_state", lambda: state)
    monkeypatch.setattr("app.router.smart_router.get_market_data", lambda: market)
    monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
    monkeypatch.setattr("app.router.smart_router.ff.pretrade_strict_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.md_watchdog_on", lambda: False)
    monkeypatch.setattr("app.router.smart_router.ff.risk_limits_on", lambda: False)
    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active",
        classmethod(lambda cls: False),
    )
    monkeypatch.setattr("app.router.smart_router.get_profile", lambda: SimpleNamespace(name="test"))
    monkeypatch.setattr("app.router.smart_router.is_live", lambda profile: False)
    monkeypatch.setattr("app.router.smart_router.provider.get", lambda *_, **__: None)
    return SmartRouter()


def test_router_metrics_smoke(tmp_path, monkeypatch) -> None:
    metrics_path = tmp_path / "metrics.prom"
    monkeypatch.setenv("METRICS_PATH", str(metrics_path))

    router = _setup_router(monkeypatch)

    result = router.register_order(
        strategy="alpha",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=1_000_000,
        nonce=1,
    )
    assert "client_order_id" in result

    metrics_payload = metrics_path.read_text(encoding="utf-8")
    assert _metric_value(metrics_payload, "propbot_orders_submitted_total") >= 1.0
    assert _metric_value(metrics_payload, "propbot_router_latency_ms_count") >= 1.0

    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active",
        classmethod(lambda cls: True),
    )
    blocked = router.register_order(
        strategy="alpha",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=2_000_000,
        nonce=2,
    )
    assert blocked.get("reason") == "safe-mode"

    metrics_payload = metrics_path.read_text(encoding="utf-8")
    assert (
        _metric_value(
            metrics_payload,
            "propbot_orders_blocked_total",
            {"reason": "safe-mode"},
        )
        >= 1.0
    )

    monkeypatch.setattr(
        "app.router.smart_router.SafeMode.is_active",
        classmethod(lambda cls: False),
    )

    router.register_order(
        strategy="beta",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=3_000_000,
        nonce=10,
    )
    dupe = router.register_order(
        strategy="beta",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        qty=1.0,
        price=100.0,
        ts_ns=3_500_000,
        nonce=11,
    )
    assert dupe.get("reason") == "dupe-intent"

    metrics_payload = metrics_path.read_text(encoding="utf-8")
    assert metrics_path.exists()
    assert (
        _metric_value(
            metrics_payload,
            "propbot_orders_blocked_total",
            {"reason": "dupe-intent"},
        )
        >= 1.0
    )
