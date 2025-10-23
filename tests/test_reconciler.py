import pytest

from app import ledger
from app.services.reconciler import FillReconciler
from app.services.runtime import reset_for_tests


class _StubBroker:
    def __init__(self) -> None:
        self._fills = [
            {
                "symbol": "BTCUSDT",
                "side": "buy",
                "qty": 0.01,
                "price": 20_000.0,
                "fee": 0.1,
                "ts": "2024-01-01T00:00:00Z",
            }
        ]
        self._positions = [
            {"symbol": "BTCUSDT", "qty": 0.01, "avg_entry": 20_000.0, "notional": 200.0}
        ]

    async def get_fills(self, since=None):  # pragma: no cover - simple stub
        return list(self._fills)

    async def get_positions(self):  # pragma: no cover - simple stub
        return list(self._positions)


class _StubRouter:
    def __init__(self) -> None:
        self._brokers = {"paper": _StubBroker()}

    def brokers(self):
        return dict(self._brokers)


@pytest.mark.asyncio
async def test_fill_reconciler_records_into_ledger():
    reset_for_tests()
    ledger.reset()
    reconciler = FillReconciler(router=_StubRouter())
    result = await reconciler.run_once()
    assert result["fills"], "fills should be recorded"
    recorded = ledger.fetch_recent_fills()
    assert recorded, "ledger should contain fills after reconciliation"
