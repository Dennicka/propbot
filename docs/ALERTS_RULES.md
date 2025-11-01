# Prometheus Alert Rules

The following alert rules cover the primary PropBot SLO signals. All rules assume
that the metrics from ``app/metrics/observability.py`` are scraped into the same
Prometheus server.

```yaml
groups:
  - name: propbot-slo
    rules:
      - alert: PropbotApiLatencyP95Warning
        expr: |
          histogram_quantile(
            0.95,
            sum(rate(api_request_latency_seconds_bucket{route!="/metrics"}[5m])) by (le)
          ) > 0.5
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "API p95 latency above 500 ms"
          description: |
            The HTTP API 95th percentile latency exceeded 500 ms for five minutes.
            Investigate upstream dependencies and recent deploys.

      - alert: MarketDataStalenessCritical
        expr: max_over_time(market_data_staleness_seconds[1m]) > 3
        for: 1m
        labels:
          severity: critical
        annotations:
          summary: "Market data feed is stale"
          description: |
            Top-of-book market data for at least one venue stayed stale (>3 seconds)
            for over a minute. Check websocket connectivity and venue status dashboards.

      - alert: OrderErrorRateWarning
        expr: sum(increase(order_errors_total[1m])) > 3
        for: 1m
        labels:
          severity: warning
        annotations:
          summary: "Order error rate above 3/min"
          description: |
            More than three order placement errors were recorded in the past minute.
            Inspect recent ledger events and venue status before resuming trading.
```

Adjust thresholds if production baselines require different sensitivity, but keep
the same structure so dashboards and runbooks remain aligned.
