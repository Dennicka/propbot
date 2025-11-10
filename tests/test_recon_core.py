from decimal import Decimal
from types import SimpleNamespace

from app.recon.core import detect_balance_drifts, detect_order_drifts, detect_position_drifts


def _cfg(
    *,
    balance_warn: str = "10",
    balance_critical: str = "100",
    position_warn: str = "0.001",
    position_critical: str = "0.01",
    order_critical_missing: bool = True,
):
    return SimpleNamespace(
        cfg=SimpleNamespace(
            recon=SimpleNamespace(
                balance_warn_usd=Decimal(balance_warn),
                balance_critical_usd=Decimal(balance_critical),
                position_size_warn=Decimal(position_warn),
                position_size_critical=Decimal(position_critical),
                order_critical_missing=order_critical_missing,
            )
        )
    )


def test_detect_balance_drifts_severity_thresholds() -> None:
    cfg = _cfg(balance_warn="5", balance_critical="15")
    local = [{"venue": "binance", "asset": "USDT", "total": Decimal("100.0")}]
    remote_ok = [{"venue": "binance", "asset": "USDT", "total": Decimal("103.0")}]
    remote_warn = [{"venue": "binance", "asset": "USDT", "total": Decimal("108.0")}]
    remote_crit = [{"venue": "binance", "asset": "USDT", "total": Decimal("130.0")}]

    assert detect_balance_drifts(local, remote_ok, cfg) == []

    warn = detect_balance_drifts(local, remote_warn, cfg)
    assert len(warn) == 1
    warn_entry = warn[0]
    assert warn_entry.severity == "WARN"
    assert warn_entry.delta == Decimal("8.0") or warn_entry.delta == 8.0

    critical = detect_balance_drifts(local, remote_crit, cfg)
    assert len(critical) == 1
    assert critical[0].severity == "CRITICAL"


def test_detect_position_drifts_includes_side_and_sign() -> None:
    cfg = _cfg(position_warn="0.1", position_critical="0.5")
    local = [
        {
            "venue": "okx",
            "symbol": "ETHUSDT",
            "qty": Decimal("0.4"),
            "entry_price": Decimal("1800"),
        }
    ]
    remote_flip = [
        {
            "venue": "okx",
            "symbol": "ETHUSDT",
            "qty": Decimal("-0.4"),
            "entry_price": Decimal("1795"),
        }
    ]
    drifts = detect_position_drifts(local, remote_flip, cfg)
    assert len(drifts) == 1
    drift = drifts[0]
    assert drift.severity == "CRITICAL"
    assert drift.delta == {"qty": -0.8}
    assert drift.local["qty"] == 0.4
    assert drift.local["entry_price"] == 1800.0 or drift.local["entry_price"] == Decimal("1800")

    remote_small = [
        {
            "venue": "okx",
            "symbol": "ETHUSDT",
            "qty": Decimal("0.25"),
        }
    ]
    warn = detect_position_drifts(local, remote_small, cfg)
    assert len(warn) == 1
    assert warn[0].severity == "WARN"


def test_detect_order_drifts_flags_orphans_and_stale() -> None:
    cfg = _cfg(order_critical_missing=True)
    local = [
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "id": "a-1",
            "qty": Decimal("1.0"),
            "status": "NEW",
        }
    ]
    remote = [
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "id": "b-2",
            "qty": Decimal("0.5"),
            "status": "NEW",
        },
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "id": "a-1",
            "qty": Decimal("1.0"),
            "status": "FILLED",
        },
    ]

    drifts = detect_order_drifts(local, remote, cfg)
    kinds = {
        (drift.delta.get("missing"), drift.severity)
        for drift in drifts
        if isinstance(drift.delta, dict)
    }
    assert ("local", "CRITICAL") in kinds  # orphan remote order
    stale = [drift for drift in drifts if drift.delta.get("note")]
    assert stale and stale[0].severity in {"WARN", "CRITICAL"}
