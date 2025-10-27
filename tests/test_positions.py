import json
import math

from positions import close_position, create_position, list_open_positions, list_positions
from positions_store import get_store_path, list_records
from services.risk_manager import can_open_new_position


def test_positions_store_file_created() -> None:
    path = get_store_path()
    assert path.exists(), "positions store should be created on reset"
    raw = path.read_text(encoding="utf-8")
    payload = json.loads(raw) if raw else []
    assert isinstance(payload, list)


def test_create_position_persists_record() -> None:
    record = create_position(
        symbol="ETHUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=500.0,
        entry_spread_bps=12.5,
        leverage=2.0,
        entry_long_price=1800.0,
        entry_short_price=1802.0,
    )
    stored = list_records()
    assert len(stored) == 1
    assert stored[0]["id"] == record["id"]
    assert stored[0]["status"] == "open"
    assert stored[0]["legs"][0]["side"] == "long"
    assert stored[0]["legs"][1]["side"] == "short"


def test_close_position_marks_record_closed() -> None:
    position = create_position(
        symbol="BTCUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=1000.0,
        entry_spread_bps=25.0,
        leverage=3.0,
        entry_long_price=20_000.0,
        entry_short_price=20_010.0,
    )
    closed = close_position(
        position["id"], exit_long_price=20_100.0, exit_short_price=20_120.0
    )
    assert closed["status"] == "closed"
    expected_qty = position["legs"][0]["base_size"]
    assert math.isclose(
        closed["pnl_usdt"],
        (20_120.0 - 20_100.0) * expected_qty,
        rel_tol=1e-6,
    )
    refreshed = list_positions()
    assert refreshed[0]["status"] == "closed"


def test_risk_manager_blocks_excess_notional(monkeypatch):
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "5")
    monkeypatch.setenv("MAX_NOTIONAL_PER_POSITION_USDT", "2000")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1500")
    create_position(
        symbol="BTCUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=900.0,
        entry_spread_bps=15.0,
        leverage=2.0,
        entry_long_price=30_000.0,
        entry_short_price=30_005.0,
    )
    allowed, reason = can_open_new_position(800.0, 2.0)
    assert allowed is False
    assert reason == "total_notional_limit_exceeded"


def test_simulated_positions_ignored_by_limits(monkeypatch):
    monkeypatch.setenv("MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("MAX_TOTAL_NOTIONAL_USDT", "1000")
    create_position(
        symbol="BTCUSDT",
        long_venue="binance-um",
        short_venue="okx-perp",
        notional_usdt=800.0,
        entry_spread_bps=10.0,
        leverage=2.0,
        status="simulated",
        simulated=True,
        entry_long_price=30_000.0,
        entry_short_price=30_010.0,
    )
    assert list_open_positions() == []

    allowed, reason = can_open_new_position(900.0, 2.0)
    assert allowed is True
    assert reason == ""
