"""Shared fixtures and helpers for acceptance tests."""

from __future__ import annotations

import json
import os
import types
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Sequence

import httpx
import pytest

from app.config.profile import TradingProfile
from app.market.watchdog import watchdog
from app.orders.state import OrderState
from app.router.smart_router import SmartRouter
from app.services.safe_mode import SafeMode, reset_safe_mode_for_tests


class _NoNetworkClient:
    """Sentinel HTTPX client that blocks accidental network usage."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: D401 - trivial
        raise RuntimeError("network access disabled in acceptance tests")


class _FrozenClock:
    """Deterministic clock controller for time-based router behaviour."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = float(start)
        self._perf = float(start)

    def time(self) -> float:
        return self._now

    def perf_counter(self) -> float:
        return self._perf

    def time_ns(self) -> int:
        return int(self._now * 1_000_000_000)

    def tock(self, dt_seconds: float) -> float:
        self._now += float(dt_seconds)
        self._perf += float(dt_seconds)
        return self._now


@pytest.fixture()
def isolate_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Isolate environment variables and block outbound network calls."""

    for key in list(os.environ.keys()):
        monkeypatch.delenv(key, raising=False)

    metrics_path = tmp_path / "metrics.prom"
    monkeypatch.setenv("METRICS_PATH", str(metrics_path))
    monkeypatch.setenv("FF_MD_WATCHDOG", "0")
    monkeypatch.setenv("FF_ORDER_TIMEOUTS", "0")

    monkeypatch.setattr(httpx, "Client", _NoNetworkClient)
    monkeypatch.setattr(httpx, "AsyncClient", _NoNetworkClient)

    reset_safe_mode_for_tests()
    watchdog.ticks.clear()
    watchdog.clear_samples()

    return metrics_path


@pytest.fixture()
def frozen_time(monkeypatch: pytest.MonkeyPatch) -> _FrozenClock:
    """Freeze the wall clock and perf counter with manual advancement."""

    clock = _FrozenClock()
    monkeypatch.setattr("time.time", clock.time)
    monkeypatch.setattr("time.perf_counter", clock.perf_counter)
    monkeypatch.setattr("time.time_ns", clock.time_ns)
    monkeypatch.setattr("app.router.timeouts.time", clock.time)
    monkeypatch.setattr("app.router.adapter.time.time", clock.time)
    monkeypatch.setattr("app.orders.idempotency.time.time", clock.time)
    monkeypatch.setattr("app.market.watchdog.time.time", clock.time)
    monkeypatch.setattr("app.market.watchdog.time.time_ns", clock.time_ns, raising=False)
    return clock


@pytest.fixture()
def make_intent() -> Callable[..., dict[str, Any]]:
    """Return a factory for deterministic router intents."""

    def factory(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "side": "buy",
            "price": Decimal("25000"),
            "qty": Decimal("1"),
            "strategy": "acceptance",
            "client_tag": "test",
        }
        payload.update(overrides)
        for key in ("qty", "price"):
            if key in payload and payload[key] is not None:
                payload[key] = Decimal(str(payload[key]))
        return payload

    return factory


def _default_router_state() -> Any:
    return types.SimpleNamespace(
        control=types.SimpleNamespace(
            post_only=False,
            taker_fee_bps_binance=2.0,
            taker_fee_bps_okx=2.0,
            default_taker_fee_bps=2.0,
        ),
        config=types.SimpleNamespace(
            data=types.SimpleNamespace(
                tca=types.SimpleNamespace(
                    horizon_min=1.0,
                    impact=types.SimpleNamespace(k=0.0),
                    tiers={},
                ),
                derivatives=types.SimpleNamespace(
                    arbitrage=types.SimpleNamespace(prefer_maker=False),
                    fees=types.SimpleNamespace(manual={}),
                ),
            )
        ),
        derivatives=types.SimpleNamespace(venues={}),
    )


def _profile_from_env() -> TradingProfile:
    profile = os.getenv("EXEC_PROFILE", "paper").strip().lower()
    if profile == "live":
        return TradingProfile(name="live", allow_trading=True, strict_flags=True)
    if profile == "testnet":
        return TradingProfile(name="testnet", allow_trading=True, strict_flags=True)
    return TradingProfile(name="paper", allow_trading=True, strict_flags=False)


def _extract_block_reason(response: Mapping[str, Any]) -> str | None:
    status = str(response.get("status", "") or "").strip().lower()
    reason = str(response.get("reason", "") or "").strip().lower()
    if response.get("ok") is False:
        return reason or status or "blocked"
    if status in {"pretrade_rejected", "duplicate_intent"} and reason == "dupe-intent":
        return "dupe-intent"
    if status == "cooldown":
        return "cooldown"
    if status == "marketdata_stale":
        gate_reason = str(response.get("gate_reason", "") or "").strip().lower()
        return "stale-p95" if gate_reason or reason else "stale"
    if status in {"live-confirm-missing", "live-readiness-not-ok"}:
        return "live-guard"
    if reason == "safe-mode":
        return "safe-mode"
    return None


def _normalise_state(value: object) -> str | None:
    if isinstance(value, OrderState):
        return value.value
    if isinstance(value, str):
        text = value.strip().upper()
        return text or None
    return None


@dataclass
class RouterFlowResult:
    router: SmartRouter
    submission: Mapping[str, Any]
    order_id: str | None
    blocked_reason: str | None

    def snapshot(self) -> Mapping[str, Any]:
        if self.order_id is None:
            return {}
        return self.router.get_order_snapshot(self.order_id)

    def final_state(self) -> str | None:
        if self.order_id is None:
            return "BLOCKED" if self.blocked_reason else None
        state = self.snapshot().get("state")
        return _normalise_state(state)


def _iter_events(events: Iterable[Any]) -> Iterable[tuple[str, float | None]]:
    for item in events:
        if isinstance(item, Mapping):
            event = str(item.get("event", "")).strip()
            qty = item.get("quantity")
        elif isinstance(item, str):
            event = item
            qty = None
        else:
            seq: Sequence[Any] = tuple(item)  # type: ignore[arg-type]
            event = str(seq[0]) if seq else ""
            qty = seq[1] if len(seq) > 1 else None
        yield event, None if qty is None else float(Decimal(str(qty)))


def _normalise_event_name(event: str) -> str:
    mapping: MutableMapping[str, str] = {
        "ack": "ack",
        "acks": "ack",
        "acknowledged": "ack",
        "partially_filled": "partial_fill",
        "partial_fill": "partial_fill",
        "partial": "partial_fill",
        "filled": "filled",
        "fill": "filled",
        "rejected": "reject",
        "reject": "reject",
        "canceled": "canceled",
        "cancel": "canceled",
        "cancelled": "canceled",
        "expired": "expired",
        "expire": "expired",
    }
    key = event.strip().lower()
    return mapping.get(key, key)


@pytest.fixture()
def run_router_flow(
    monkeypatch: pytest.MonkeyPatch,
    isolate_env: Path,
    frozen_time: _FrozenClock,
) -> Callable[[Mapping[str, Any], Iterable[Any]], RouterFlowResult]:
    """Execute a router submission flow and replay lifecycle events."""

    def factory(
        intent: Mapping[str, Any],
        events: Iterable[Any] = (),
        *,
        router: SmartRouter | None = None,
    ) -> RouterFlowResult:
        monkeypatch.setenv("TEST_ONLY_ROUTER_META", "*")
        monkeypatch.setenv("TEST_ONLY_ROUTER_TICK_SIZE", "0.1")
        monkeypatch.setenv("TEST_ONLY_ROUTER_STEP_SIZE", "0.001")
        monkeypatch.delenv("TEST_ONLY_ROUTER_MIN_NOTIONAL", raising=False)
        state = _default_router_state()
        market = types.SimpleNamespace()
        profile = _profile_from_env()
        monkeypatch.setattr("app.router.smart_router.get_state", lambda: state)
        monkeypatch.setattr("app.router.smart_router.get_market_data", lambda: market)
        monkeypatch.setattr("app.router.smart_router.get_liquidity_status", lambda: {})
        monkeypatch.setattr("app.router.smart_router.get_profile", lambda: profile)

        router_instance = router or SmartRouter()
        ts_ns = int(intent.get("ts_ns", frozen_time.time_ns()))
        nonce = int(intent.get("nonce", 1))
        submission = router_instance.register_order(
            strategy=str(intent.get("strategy")),
            venue=str(intent.get("venue")),
            symbol=str(intent.get("symbol")),
            side=str(intent.get("side")),
            qty=float(Decimal(str(intent.get("qty", Decimal("0"))))),
            price=float(Decimal(str(intent["price"]))) if intent.get("price") is not None else None,
            ts_ns=ts_ns,
            nonce=nonce,
        )

        blocked_reason = _extract_block_reason(submission)
        order_id = submission.get("client_order_id") if blocked_reason is None else None

        if order_id:
            for name, qty in _iter_events(events):
                event_key = _normalise_event_name(name)
                router_instance.process_order_event(
                    client_order_id=order_id,
                    event=event_key,
                    quantity=qty,
                )

        return RouterFlowResult(
            router=router_instance,
            submission=submission,
            order_id=order_id,
            blocked_reason=blocked_reason,
        )

    return factory


def load_golden_fixture(path: str) -> Mapping[str, Any]:
    file_path = Path(__file__).parent / "golden" / path
    with file_path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
