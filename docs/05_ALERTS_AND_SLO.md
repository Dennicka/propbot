# Alerts and SLO Overview

This playbook complements ``docs/ALERTS_RULES.md`` with the context behind each
signal, the precise SLO target, and the actions expected from the operator when a
breach occurs.

## SLO Targets

| Domain | Metric | Target |
| --- | --- | --- |
| API responsiveness | 95% of `api_request_latency_seconds` < 500 ms | Keep p95 latency under 500 ms over any 5 minute window. |
| Market data freshness | `market_data_staleness_seconds` < 3 s | Top-of-book feeds stay < 3 s old; otherwise treat the venue as stale. |
| Order success rate | `order_errors_total` increase ≤ 3 per minute | More than 3 errors/min triggers a trading pause and investigation. |
| Watchdog coverage | `watchdog_health{venue}` == 1 | Gauge must remain 1; zero indicates degraded or auto-hold state. |

## Operator Response

### API latency warning
1. Confirm alert details on Grafana (check per-route breakdown).
2. Inspect upstream dependencies (databases, ledger, external APIs) for elevated latency.
3. Review recent deploys or feature flags; roll back if correlated.
4. If the issue persists for >15 minutes, escalate to the on-call backend engineer.

### Market data staleness critical
1. Identify the affected venue and symbol from the alert labels.
2. Check websocket connections and the venue status page; restart the market-data
   service if connections dropped.
3. Fail over to REST snapshots or trigger a manual reconnect if automation fails.
4. Engage `HOLD` mode if the venue remains stale for >5 minutes to avoid trading on
   outdated quotes.

### Order error rate warning
1. Inspect `ledger.events` and the operations dashboard for repeated order failures.
2. Verify venue credentials and balance to ensure rejections are not due to
   insufficient margin or rate limits.
3. Pause automated strategies generating the failures until the root cause is fixed.
4. Escalate to the venue integration owner if errors continue for 10 minutes.

### Watchdog health degraded
1. The `watchdog_health` gauge dropping to 0 indicates the exchange watchdog marked
   the venue as unhealthy or auto-hold.
2. Check ``/api/ui/status`` for the detailed watchdog reason and ensure the
   corresponding mitigation (auto-hold, manual intervention) remains active.
3. Clear the condition once telemetry confirms the venue recovered.

## Dashboards and Ownership

* **Grafana dashboard**: `PropBot / Reliability` contains latency and staleness
  panels keyed off the new metrics.
* **Alert routing**: Warning-level alerts page the trading-operations rotation; the
  on-call backend engineer joins for critical incidents.
* **Runbook updates**: Keep this document and the YAML rules in sync whenever
  thresholds change to ensure alerts and operator actions remain aligned.
