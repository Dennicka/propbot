from fastapi.testclient import TestClient

from app.metrics import observability


def test_metrics_exposed_and_incremented(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(observability, "METRICS_SLO_ENABLED", True, raising=False)
    observability.reset_for_tests()
    observability.register_slo_metrics()

    observability.observe_api_request("/health", "GET", 200, 0.1)
    observability.set_market_data_staleness("binance", "BTCUSDT", 1.5)
    observability.record_order_error("binance", "http_500")
    observability.set_watchdog_health("binance", False)

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.text

    assert 'api_request_latency_seconds_count{method="GET",route="/health",status="200"}' in body
    assert 'market_data_staleness_seconds{symbol="BTCUSDT",venue="binance"} 1.5' in body
    assert 'order_errors_total{reason="http_500",venue="binance"} 1.0' in body
    assert 'watchdog_health{venue="binance"} 0.0' in body
