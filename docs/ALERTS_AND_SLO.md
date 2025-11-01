# Alerts and SLOs

| Indicator | Metric | Target | Alert Rule Sketch |
| --- | --- | --- | --- |
| API latency | `api_latency_seconds{route="/api/ui/**"}` P95 | P95 < 350ms over 5m | `histogram_quantile(0.95, sum(rate(api_latency_seconds_bucket{route=~"/api/ui/.*"}[5m])) by (le)) > 0.35` triggers `UIHighLatency` |
| Market data freshness | `market_data_staleness_seconds{venue,symbol}` | < 3s for top symbols | `max_over_time(market_data_staleness_seconds{symbol=~"BTC.*"}[5m]) > 3` triggers `MarketDataStale` |
| Order errors | `order_errors_total{venue}` | 0 errors per 5m | `increase(order_errors_total[5m]) > 0` triggers `OrderErrorSpike` |
| Watchdog state | `watchdog_state{venue}` | 0 (OK) | `watchdog_state{venue!=""} == 2` => `WatchdogDown` (page) / `== 1` => `WatchdogDegraded` (ticket + chat) |
| Risk throttle | `propbot_risk_throttled{reason}` | 0 | `propbot_risk_throttled{reason!="none"} == 1` triggers `RiskThrottleActive` |
| Auto-HOLD escalations | `propbot_risk_windows_total{throttled="true"}` | monotonically increasing | `increase(propbot_risk_windows_total{throttled="true"}[5m]) > 1` with runtime hold => `AutoHoldFired` (page) |

These rules compliment the internal runtime gauges:

- `propbot_watchdog_state{exchange}` switches labels between OK/DEGRADED/AUTO_HOLD for dashboards.
- `propbot_risk_success_rate_1h`, `propbot_risk_order_error_rate_1h` and `propbot_risk_throttled{reason}` power the RISK_THROTTLED banner and downstream alerts.
- `propbot_daily_loss_breach` indicates risk guard posture and should stay `0`.

Prometheus recording rules should downsample the histogram buckets before alerting to avoid over-alerting. Each alert is paired with a PagerDuty route and Slack notification for the operations channel.
