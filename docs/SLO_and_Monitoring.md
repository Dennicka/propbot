# SLO и мониторинг

## Источник порогов
- Конфиг `configs/status_thresholds.yaml` содержит целевые значения SLO и минимальные скоринги по уровням P0…P3.
- `RuntimeState.metrics.slo` хранит текущие измерения, а `/api/ui/status/slo` отдаёт фактические значения и thresholds.

## Ключевые метрики
| Метрика | Описание | Порог |
| --- | --- | --- |
| `ws_gap_ms_p95` | задержка WebSocket-инкрементов | `local_ok/vps_ok` из YAML |
| `order_cycle_ms_p95` | цикл заявки (две ноги) | `local_ok/vps_ok` |
| `reject_rate` | доля отказов | `ok` |
| `cancel_fail_rate` | отказ отмен | `ok` |
| `recon_mismatch` | расхождение сверки | `ok` |
| `max_day_drawdown_bps` | просадка | `ok` |
| `budget_remaining` | остаток бюджета на риски | операторский контроль |

## Алёрты / HOLD
- P0 гард в `WARN/ERROR` → overview переходит в `HOLD` (см. `/api/ui/status/components`).
- `runaway_breaker` отслеживает количество rescue (`metrics.counters.rescues`).
- SAFE_MODE позволяет выполнять dry-run, но live доступен только при двух approvals и успешном preflight.

## Exporters
- `/metrics` — Prometheus, включает бизнес-счётчики и latency-гистограмму `app_latency_ms`.
- `/metrics/latency` — endpoint, который дополняет гистограмму и сохраняет выборки в runtime.

## Дашборды
- System Status UI (React/графики) потребляет `/api/ui/status/*` и `/api/ui/stream`.
- Дополнительный мониторинг: интегрировать `/metrics` в Grafana, настроить алерты по SLO-порогам.
