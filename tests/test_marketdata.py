from app.services.marketdata import MarketDataAggregator


def test_marketdata_rest_fallback_and_cache():
    calls = {}

    def fetch(symbol: str):
        calls.setdefault("count", 0)
        calls["count"] += 1
        calls["symbol"] = symbol
        return {"bid": 100.0, "ask": 101.0, "ts": 123.0}

    aggregator = MarketDataAggregator(rest_fetchers={"binance-um": fetch}, stale_after=10.0)
    book = aggregator.top_of_book("binance-um", "BTCUSDT")
    assert book["bid"] == 100.0
    assert calls["count"] == 1

    aggregator.update_from_ws(venue="binance-um", symbol="BTCUSDT", bid=99.5, ask=100.5)
    cached = aggregator.top_of_book("binance-um", "BTCUSDT")
    assert cached["bid"] == 99.5
    assert calls["count"] == 1, "should not call REST when cache fresh"
