from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
    ReconSnapshot,
)
from app.routers.ui_recon import get_recon_service


class _StubReconService:
    async def run_for_venue(self, venue_id: str) -> ReconSnapshot:
        return ReconSnapshot(
            venue_id=venue_id,
            balances_internal=[
                ExchangeBalanceSnapshot(
                    venue_id=venue_id,
                    asset="USDT",
                    total=Decimal("100"),
                    available=Decimal("80"),
                )
            ],
            balances_external=[],
            positions_internal=[
                ExchangePositionSnapshot(
                    venue_id=venue_id,
                    symbol="BTCUSDT",
                    qty=Decimal("1"),
                    entry_price=Decimal("25000"),
                    notional=Decimal("25000"),
                )
            ],
            positions_external=[],
            open_orders_internal=[
                ExchangeOrderSnapshot(
                    venue_id=venue_id,
                    symbol="BTCUSDT",
                    client_order_id="order-1",
                    exchange_order_id="ex-1",
                    side="buy",
                    qty=Decimal("0.5"),
                    price=Decimal("20000"),
                    status="open",
                )
            ],
            open_orders_external=[],
            issues=[],
        )


def test_recon_snapshot_endpoint_smoke(client: TestClient) -> None:
    client.app.dependency_overrides[get_recon_service] = lambda: _StubReconService()
    try:
        response = client.get("/api/ui/recon/snapshot", params={"venue_id": "test_venue"})
        assert response.status_code == 200
        data = response.json()
        assert data["venue_id"] == "test_venue"
        assert "issues" in data
        assert "errors_count" in data
        assert "warnings_count" in data
    finally:
        client.app.dependency_overrides.pop(get_recon_service, None)
