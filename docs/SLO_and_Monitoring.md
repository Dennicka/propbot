# SLO_and_Monitoring.md

- Метрики и пороги — см. `configs/status_thresholds.yaml`.
- Ключевые SLO: ws_gap_ms_p95, order_cycle_ms_p95, reject_rate, cancel_fail_rate, recon_mismatch, max_day_drawdown_bps.
- Алерты: HOLD при 3× превышении SLO ≥ 5 минут.
