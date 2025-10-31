# Risk Limits & Guardrails

## Notional / Delta Caps
- Конфиги (`configs/config.*.yaml`) задают:
  - `notional_caps.per_symbol_usd`
  - `notional_caps.per_venue_usd`
  - `notional_caps.total_usd`
  - `cross_venue_delta_abs_max_usd`
- Нарушение → гард `kill_caps` = WARN/HOLD, SAFE_MODE запрещает новые открытия, рекомендовано `POST /api/hedge/flatten`.

## P0 Guardrails
| Guard | Триггер | Поведение |
| --- | --- | --- |
| `cancel_on_disconnect` | Потеря коннекта / ручное включение | HOLD, Overview=HOLD, инцидент | 
| `rate_limit` | Превышение place/cancel per min | WARN, отклонение симулированных ордеров |
| `clock_skew` | Δ времени > `clock_skew_guard_ms` | ERROR, HOLD |
| `snapshot_diff` | Gap в потоках снапшот+дифф | WARN, требуется реинициализация |
| `kill_caps` | Breach notional/delta caps | HOLD, требуется flatten |
| `runaway_breaker` | Rescue >= лимита, fail leg B | WARN/HOLD, ручной сброс |
| `maintenance_calendar` | Текущее время попадает в окно | HOLD до окончания окна |

## Two-Man Rule
- `control-state.two_man_rule=true` → для live-исполнения нужны ≥2 approvals (`/api/ui/approvals`).
- Approvals фиксируются в runtime (`register_approval`).

## SAFE_MODE
- По умолчанию `true`, `/api/arb/execute` только dry-run.
- Для live: изменить конфиг/ENV, выполнить preflight, собрать approvals.
- `/live-readiness` отражает готовность (ок=true) пока watchdog не в `AUTO_HOLD` и дневной loss cap не в `BREACH`.
- `/api/ui/state` → `flags` показывает текущие значения `MODE`, `SAFE_MODE`, `POST_ONLY`, `REDUCE_ONLY`, `ENV`.

## Incident Handling
1. Идентифицировать гард (`/api/ui/status/components`).
2. При runaway-breaker → `POST /api/hedge/flatten`, затем ручная сверка позиций `/api/deriv/positions`.
3. Зафиксировать RCA, сбросить гард в `OK` (UI), повторить preflight.
