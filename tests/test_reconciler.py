import json

from app.services import runtime
from services import reconciler


def test_reconciler_detects_and_persists(tmp_path):
    runtime.reset_for_tests()
    alerts_path = tmp_path / "alerts.json"

    stored_positions = [
        {
            "id": "missing-exchange",
            "status": "open",
            "symbol": "BTCUSDT",
            "notional_usdt": 1000.0,
            "entry_spread_bps": 12.0,
            "leverage": 2.0,
            "legs": [
                {
                    "venue": "binance-um",
                    "symbol": "BTCUSDT",
                    "side": "long",
                    "status": "open",
                    "base_size": 0.5,
                },
                {
                    "venue": "okx-perp",
                    "symbol": "BTCUSDT",
                    "side": "short",
                    "status": "open",
                    "base_size": 0.5,
                },
            ],
        },
        {
            "id": "size-mismatch",
            "status": "open",
            "symbol": "ETHUSDT",
            "notional_usdt": 500.0,
            "entry_spread_bps": 6.0,
            "leverage": 2.0,
            "legs": [
                {
                    "venue": "binance-um",
                    "symbol": "ETHUSDT",
                    "side": "long",
                    "status": "open",
                    "base_size": 1.0,
                },
                {
                    "venue": "okx-perp",
                    "symbol": "ETHUSDT",
                    "side": "short",
                    "status": "open",
                    "base_size": 1.0,
                },
            ],
        },
        {
            "id": "closed-still-live",
            "status": "closed",
            "symbol": "LTCUSDT",
            "notional_usdt": 200.0,
            "entry_spread_bps": 4.0,
            "leverage": 1.5,
            "legs": [
                {
                    "venue": "binance-um",
                    "symbol": "LTCUSDT",
                    "side": "long",
                    "status": "closed",
                    "base_size": 0.8,
                },
                {
                    "venue": "okx-perp",
                    "symbol": "LTCUSDT",
                    "side": "short",
                    "status": "closed",
                    "base_size": 0.8,
                },
            ],
        },
        {
            "id": "partial-leg",
            "status": "partial",
            "symbol": "SOLUSDT",
            "notional_usdt": 300.0,
            "entry_spread_bps": 5.0,
            "leverage": 2.0,
            "legs": [
                {
                    "venue": "binance-um",
                    "symbol": "SOLUSDT",
                    "side": "long",
                    "status": "partial",
                    "base_size": 1.2,
                },
                {
                    "venue": "okx-perp",
                    "symbol": "SOLUSDT",
                    "side": "short",
                    "status": "missing",
                    "base_size": 0.0,
                },
            ],
        },
    ]

    exchange_positions = [
        {"venue": "binance-um", "symbol": "ETHUSDT", "base_qty": 1.5},
        {"venue": "okx-perp", "symbol": "ETHUSDT", "base_qty": -1.5},
        {"venue": "binance-um", "symbol": "LTCUSDT", "base_qty": 0.8},
        {"venue": "okx-perp", "symbol": "LTCUSDT", "base_qty": -0.8},
        {"venue": "binance-um", "symbol": "SOLUSDT", "base_qty": 1.2},
    ]

    issues = reconciler.reconcile(
        stored_positions=stored_positions,
        exchange_positions=exchange_positions,
        open_orders=[],
        alerts_path=alerts_path,
    )

    kinds = {issue.get("kind") for issue in issues}
    assert "position_missing_on_exchange" in kinds
    assert "unexpected_exchange_position" in kinds
    assert "position_size_mismatch" in kinds
    assert any(issue.get("kind") == "partial_leg_stalled" for issue in issues)

    status = runtime.get_reconciliation_status()
    assert status.get("desync_detected") is True
    assert status.get("issue_count") == len(issues)

    saved = json.loads(alerts_path.read_text())
    assert saved and saved[-1]["issue_count"] == len(issues)

    cleared = reconciler.reconcile(
        stored_positions=[],
        exchange_positions=[],
        open_orders=[],
        alerts_path=alerts_path,
    )
    assert cleared == []
    status = runtime.get_reconciliation_status()
    assert status.get("desync_detected") is False
    assert status.get("issue_count") == 0
