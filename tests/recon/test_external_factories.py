from __future__ import annotations

from typing import Sequence

import pytest

from app.recon.external_client import ExchangeAccountClient
from app.recon.external_factories import get_exchange_account_client_for_venue
from app.recon.models import (
    ExchangeBalanceSnapshot,
    ExchangeOrderSnapshot,
    ExchangePositionSnapshot,
)


class DummyClient(ExchangeAccountClient):
    async def load_balances(self, venue_id: str) -> Sequence[ExchangeBalanceSnapshot]:
        return []

    async def load_positions(self, venue_id: str) -> Sequence[ExchangePositionSnapshot]:
        return []

    async def load_open_orders(self, venue_id: str) -> Sequence[ExchangeOrderSnapshot]:
        return []


@pytest.mark.parametrize(
    "venue_id",
    ["binance_um", "okx_perp"],
)
def test_factory_prefers_runtime_client(monkeypatch: pytest.MonkeyPatch, venue_id: str) -> None:
    dummy = DummyClient()

    monkeypatch.setattr(
        "app.recon.external_factories._resolve_runtime_client",
        lambda vid: dummy if vid == venue_id else None,
    )
    monkeypatch.setattr(
        "app.recon.external_factories._build_exchange_client",
        lambda vid: None,
    )

    client = get_exchange_account_client_for_venue(venue_id)
    assert client is dummy


def test_factory_falls_back_to_builder(monkeypatch: pytest.MonkeyPatch) -> None:
    dummy = DummyClient()

    monkeypatch.setattr(
        "app.recon.external_factories._resolve_runtime_client",
        lambda vid: None,
    )
    monkeypatch.setattr(
        "app.recon.external_factories._build_exchange_client",
        lambda vid: dummy if vid == "okx_perp" else None,
    )

    client = get_exchange_account_client_for_venue("okx_perp")
    assert client is dummy


def test_factory_unknown_venue_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.recon.external_factories._resolve_runtime_client",
        lambda vid: None,
    )
    monkeypatch.setattr(
        "app.recon.external_factories._build_exchange_client",
        lambda vid: None,
    )

    assert get_exchange_account_client_for_venue("unknown") is None
