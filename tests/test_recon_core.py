from __future__ import annotations

from app.recon.reconciler import RECON_QTY_TOL, Reconciler


class FakeClient:
    def __init__(self, positions: list[dict[str, object]], marks: dict[str, float] | None = None) -> None:
        self._positions = positions
        self._marks = marks or {}

    def positions(self) -> list[dict[str, object]]:
        return list(self._positions)

    def get_mark_price(self, symbol: str) -> dict[str, float]:  # pragma: no cover - fallback handled in tests
        if symbol in self._marks:
            return {"price": self._marks[symbol]}
        raise RuntimeError(f"no mark for {symbol}")


class FakeVenueRuntime:
    def __init__(self, client: FakeClient) -> None:
        self.client = client


class FakeSafety:
    def __init__(self, snapshot: dict[str, object] | None = None) -> None:
        self.risk_snapshot = snapshot or {}


class FakeState:
    def __init__(self, venues: dict[str, FakeVenueRuntime], snapshot: dict[str, object]) -> None:
        self.derivatives = type("Derivatives", (), {"venues": venues})()
        self.safety = FakeSafety(snapshot)


class FakeAdapters:
    def __init__(self, state: FakeState, ledger_rows: list[dict[str, object]]) -> None:
        self._state = state
        self._ledger_rows = ledger_rows

    def get_state(self) -> FakeState:
        return self._state

    def fetch_ledger_positions(self) -> list[dict[str, object]]:
        return list(self._ledger_rows)


def test_reconciler_produces_diffs() -> None:
    ledger_rows = [
        {"venue": "binance-um", "symbol": "BTCUSDT", "base_qty": 1.0, "avg_price": 20000.0},
        {"venue": "okx-perp", "symbol": "BTC-USDT-SWAP", "base_qty": -1.0, "avg_price": 19950.0},
    ]
    binance_client = FakeClient(
        positions=[{"symbol": "BTCUSDT", "position_amt": 1.002}],
        marks={"BTCUSDT": 20050.0},
    )
    okx_client = FakeClient(
        positions=[{"instId": "BTC-USDT-SWAP", "pos": -0.95}],
        marks={"BTC-USDT-SWAP": 19980.0},
    )
    venues = {
        "binance_um": FakeVenueRuntime(binance_client),
        "okx_perp": FakeVenueRuntime(okx_client),
    }
    snapshot = {"exposure_by_symbol": {"BTCUSDT": 40000.0}}
    state = FakeState(venues, snapshot)
    adapters = FakeAdapters(state, ledger_rows)
    reconciler = Reconciler(adapters=adapters)  # type: ignore[arg-type]

    diffs = reconciler.diff()
    assert len(diffs) == 2
    venues_seen = {(entry["venue"], entry["symbol"]) for entry in diffs}
    assert ("binance-um", "BTCUSDT") in venues_seen
    assert ("okx-perp", "BTCUSDT") in venues_seen
    for entry in diffs:
        assert abs(entry["delta"]) > 0
        assert entry.get("notional_usd", 0.0) > 0


def test_reconciler_applies_tolerance() -> None:
    ledger_rows = [
        {"venue": "binance-um", "symbol": "ETHUSDT", "base_qty": 10.0, "avg_price": 1200.0},
    ]
    delta = RECON_QTY_TOL / 10
    binance_client = FakeClient(
        positions=[{"symbol": "ETHUSDT", "position_amt": 10.0 + delta}],
        marks={"ETHUSDT": 1200.0},
    )
    venues = {"binance_um": FakeVenueRuntime(binance_client)}
    snapshot = {"exposure_by_symbol": {"ETHUSDT": 12000.0}}
    state = FakeState(venues, snapshot)
    adapters = FakeAdapters(state, ledger_rows)
    reconciler = Reconciler(adapters=adapters)  # type: ignore[arg-type]

    diffs = reconciler.diff()
    assert diffs == []
