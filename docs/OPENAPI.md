# OPENAPI — ключевые эндпоинты

Сервис публикует OpenAPI по `/openapi.json` и интерактивный swagger `/docs`.

## Health / readiness
- `GET /api/health` — статус сервиса.
- `GET /live-readiness` — готовность к live (watchdog!=AUTO_HOLD, daily loss!=BREACH).

## UI / Control
- `GET /api/ui/status/overview|components|slo` — агрегаторы System Status.
- `GET /api/ui/control-state` — SAFE_MODE, Two-Man Rule, статусы гардов.
- `GET /api/ui/pnl`, `/api/ui/exposure`, `/api/ui/limits`, `/api/ui/universe`, `/api/ui/execution` — дашборды (бумажные данные).
- `GET /api/ui/approvals` — список подтверждений.
- `POST /api/ui/config/{validate,apply,rollback}` — конфиг-пайплайн.
- `GET /api/ui/recon/status|history`, `POST /api/ui/recon/run` — сверки.
- `WS /api/ui/stream` — статусы в real-time.

## Arbitrage / Derivatives
- `GET /api/deriv/status` — состояния биржевых адаптеров.
- `POST /api/deriv/setup` — установка режимов маржи/позиции/плеча.
- `GET /api/deriv/positions` — бумажные позиции.
- `GET /api/arb/edge` — оценка edges для пар.
- `POST /api/arb/preview` — префлайт + dry-run план.
- `POST /api/arb/execute` — state-machine исполнения (SAFE_MODE → dry-run).
- `POST /api/hedge/flatten` — reduceOnly закрытие всех ног.

## Monitoring
- `GET /metrics` — Prometheus.
- `GET /metrics/latency` — вспомогательный гистограммный эндпоинт.
