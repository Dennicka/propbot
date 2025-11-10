from app.router.adapter import generate_client_order_id


def test_same_context_same_identifier() -> None:
    ts = 1_700_000_000.0
    context = {
        "strategy": "alpha",
        "venue": "binance-um",
        "symbol": "BTCUSDT",
        "side": "buy",
        "nonce": "intent-1",
    }

    first = generate_client_order_id(timestamp=ts, **context)
    # Retry a few seconds later but within the same bucket should yield the same id
    second = generate_client_order_id(timestamp=ts + 10, **context)
    assert first == second


def test_different_nonce_changes_identifier() -> None:
    ts = 1_700_000_000.0
    base_kwargs = {
        "strategy": "alpha",
        "venue": "binance-um",
        "symbol": "BTCUSDT",
        "side": "buy",
    }

    first = generate_client_order_id(timestamp=ts, nonce="intent-1", **base_kwargs)
    second = generate_client_order_id(timestamp=ts, nonce="intent-2", **base_kwargs)
    assert first != second


def test_different_context_changes_identifier() -> None:
    ts = 1_700_000_000.0
    first = generate_client_order_id(
        strategy="alpha",
        venue="binance-um",
        symbol="BTCUSDT",
        side="buy",
        nonce="intent-1",
        timestamp=ts,
    )
    other = generate_client_order_id(
        strategy="alpha",
        venue="binance-um",
        symbol="ETHUSDT",
        side="buy",
        nonce="intent-1",
        timestamp=ts,
    )
    assert first != other

