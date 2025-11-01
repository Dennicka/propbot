from tests.fakes.fake_broker import FakeBroker


def test_fake_broker_instantiates() -> None:
    broker = FakeBroker()

    assert isinstance(broker, FakeBroker)
    assert broker.metrics_tags()["broker"] == "fake"
    broker.emit_order_error("binance", "http_500")
    broker.emit_order_latency("binance", 0.1)
    broker.emit_marketdata_staleness("binance", "BTCUSDT", 1.0)
