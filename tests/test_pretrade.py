from decimal import Decimal

import pytest

from app.rules.pretrade import PretradeRejection, validate_pretrade


def test_min_qty_rejection() -> None:
    meta = {"minQty": "0.1", "venue": "binance", "symbol": "BTCUSDT"}
    with pytest.raises(PretradeRejection) as exc:
        validate_pretrade("buy", Decimal("100"), Decimal("0.09"), meta)
    assert exc.value.reason == "minQty"


def test_min_notional_checks() -> None:
    meta = {"minNotional": "50", "venue": "okx", "symbol": "ETHUSDT"}
    validate_pretrade("buy", Decimal("150"), Decimal("0.4"), meta)
    with pytest.raises(PretradeRejection) as exc:
        validate_pretrade("buy", Decimal("150"), Decimal("0.3"), meta)
    assert exc.value.reason == "minNotional"


def test_non_positive_rejection() -> None:
    with pytest.raises(PretradeRejection) as exc:
        validate_pretrade("sell", Decimal("0"), Decimal("1"), {})
    assert exc.value.reason == "non_positive"
    with pytest.raises(PretradeRejection):
        validate_pretrade("sell", Decimal("100"), Decimal("0"), {})
