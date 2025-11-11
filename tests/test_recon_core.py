from decimal import Decimal
from types import SimpleNamespace

from app.recon.core import (
    compare_pnl_ledgers,
    detect_balance_drifts,
    detect_order_drifts,
    detect_pnl_drifts,
    detect_position_drifts,
)


def _cfg(
    *,
    balance_warn: str = "10",
    balance_critical: str = "100",
    position_warn: str = "0.001",
    position_critical: str = "0.01",
    order_critical_missing: bool = True,
    pnl_warn: str = "5",
    pnl_critical: str = "20",
    pnl_rel_warn: str = "0.01",
    pnl_rel_critical: str = "0.05",
    fee_warn: str = "2",
    fee_critical: str = "10",
    funding_warn: str = "2",
    funding_critical: str = "10",
):
    return SimpleNamespace(
        cfg=SimpleNamespace(
            recon=SimpleNamespace(
                balance_warn_usd=Decimal(balance_warn),
                balance_critical_usd=Decimal(balance_critical),
                position_size_warn=Decimal(position_warn),
                position_size_critical=Decimal(position_critical),
                order_critical_missing=order_critical_missing,
                pnl_warn_usd=Decimal(pnl_warn),
                pnl_critical_usd=Decimal(pnl_critical),
                pnl_relative_warn=Decimal(pnl_rel_warn),
                pnl_relative_critical=Decimal(pnl_rel_critical),
                fee_warn_usd=Decimal(fee_warn),
                fee_critical_usd=Decimal(fee_critical),
                funding_warn_usd=Decimal(funding_warn),
                funding_critical_usd=Decimal(funding_critical),
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


def test_detect_pnl_drifts_respects_thresholds() -> None:
    cfg = _cfg(
        pnl_warn="5",
        pnl_critical="25",
        pnl_rel_warn="1",
        pnl_rel_critical="2",
        fee_warn="1",
        fee_critical="5",
    )
    local = [
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "realized": Decimal("10"),
            "fees": Decimal("1"),
            "funding": Decimal("0"),
            "rebates": Decimal("0"),
            "net": Decimal("9"),
            "supports_fees": True,
            "supports_funding": True,
        }
    ]
    remote_warn = [
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "realized": Decimal("16"),
            "fees": Decimal("1"),
            "funding": Decimal("0"),
            "rebates": Decimal("0"),
            "net": Decimal("15"),
            "supports_fees": True,
            "supports_funding": True,
        }
    ]
    warn = detect_pnl_drifts(local, remote_warn, cfg)
    assert warn and warn[0].severity == "WARN"

    remote_crit = [
        {
            "venue": "binance",
            "symbol": "BTCUSDT",
            "realized": Decimal("50"),
            "fees": Decimal("1"),
            "funding": Decimal("0"),
            "rebates": Decimal("0"),
            "net": Decimal("49"),
            "supports_fees": True,
            "supports_funding": True,
        }
    ]
    critical = detect_pnl_drifts(local, remote_crit, cfg)
    assert critical and critical[0].severity == "CRITICAL"


def test_compare_pnl_ledgers_flags_missing_remote() -> None:
    local = [
        {
            "venue": "okx",
            "symbol": "ETHUSDT",
            "realized": Decimal("5"),
            "fees": Decimal("0.5"),
            "funding": Decimal("0"),
            "rebates": Decimal("0"),
            "net": Decimal("4.5"),
            "supports_fees": True,
            "supports_funding": True,
        }
    ]
    issues = compare_pnl_ledgers(local, [],)
    assert issues and issues[0].code == "PNL_REMOTE_MISSING"

    remote = [
        {
            "venue": "okx",
            "symbol": "ETHUSDT",
            "realized": Decimal("8"),
            "fees": Decimal("0.5"),
            "funding": Decimal("0"),
            "rebates": Decimal("0"),
            "net": Decimal("7.5"),
            "supports_fees": True,
            "supports_funding": True,
        }
    ]
    mismatches = compare_pnl_ledgers(local, remote)
    assert mismatches and mismatches[0].kind == "PNL"
