from fastapi.testclient import TestClient

from app.metrics import observability
from app.metrics import execution as execution_metrics
from app.metrics import pnl as pnl_metrics


def test_metrics_exposed_and_incremented(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(observability, "METRICS_SLO_ENABLED", True, raising=False)
    observability.reset_for_tests()
    observability.register_slo_metrics()

    observability.observe_api_latency("/health", "GET", 200, 0.1)
    observability.set_market_data_staleness("binance", "BTCUSDT", 1.5)
    observability.record_order_error("binance", "http_500")
    observability.set_watchdog_state_metric("binance", "AUTO_HOLD")

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.text

    assert 'api_latency_seconds_count{method="GET",route="/health",status="200"}' in body
    assert 'market_data_staleness_seconds{symbol="BTCUSDT",venue="binance"} 1.5' in body
    assert 'order_errors_total{reason="http_500",venue="binance"} 1.0' in body
    assert 'watchdog_state{venue="binance"} 2.0' in body


def test_stuck_order_metrics_exposed(client: TestClient) -> None:
    execution_metrics.STUCK_ORDERS_TOTAL.labels("venue", "symbol").inc()
    execution_metrics.ORDER_RETRIES_TOTAL.labels("venue", "symbol").inc()
    execution_metrics.OPEN_ORDERS_GAUGE.labels("venue", "symbol", "status").set(2.0)
    execution_metrics.STUCK_RESOLVER_RETRIES_TOTAL.labels("venue", "symbol", "reason").inc()
    execution_metrics.STUCK_RESOLVER_ACTIVE_INTENTS.set(3.0)

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.text

    assert 'stuck_orders_total{symbol="symbol",venue="venue"} 1.0' in body
    assert 'order_retries_total{symbol="symbol",venue="venue"} 1.0' in body
    assert 'open_orders_gauge{status="status",symbol="symbol",venue="venue"} 2.0' in body
    assert 'stuck_resolver_retries_total{reason="reason",symbol="symbol",venue="venue"} 1.0' in body
    assert 'stuck_resolver_active_intents 3.0' in body


def test_pnl_metrics_exposed(client: TestClient) -> None:
    pnl_metrics.reset_for_tests()
    pnl_metrics.update_pnl_metrics(
        profile="paper",
        realized={"BTCUSDT": 5.0},
        unrealized={"BTCUSDT": 1.25},
        fees={"BTCUSDT": 0.1},
        funding={"BTCUSDT": -0.05},
        total_realized=5.0,
        total_unrealized=1.25,
        total_fees=0.1,
        total_funding=-0.05,
    )

    metrics = client.get("/metrics")
    assert metrics.status_code == 200
    body = metrics.text

    assert 'pnl_realized_usd{profile="paper",symbol="BTCUSDT"} 5.0' in body
    assert 'pnl_unrealized_usd{profile="paper",symbol="BTCUSDT"} 1.25' in body
    assert 'fees_paid_usd{profile="paper",symbol="BTCUSDT"} 0.1' in body
    assert 'funding_paid_usd{profile="paper",symbol="BTCUSDT"} -0.05' in body
    assert 'pnl_realized_usd{profile="paper",symbol="__total__"} 5.0' in body
