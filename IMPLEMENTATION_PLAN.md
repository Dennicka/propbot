# IMPLEMENTATION_PLAN.md (≤120 строк)

## 1) Архитектура/модули
- FastAPI `app.server_ws:app`; слои: routers/, services/, util/, db/.
- Pydantic v2; asyncio; структурированные логи + reason-codes.
- БД: SQLite (WAL) + SQLAlchemy 2.0 + Alembic (миграции).
- Метрики: prometheus_client; /metrics, /metrics/latency.
- Конфиги: YAML (profiles: paper default), runtime apply/rollback.

## 2) API/контракты
- health: GET /api/health (200, build/version), GET /live-readiness.
- UI: /api/ui/{execution,pnl,exposure,state}.
- Recon: /api/ui/recon/{status,run,history}.
- Stream: WS /api/ui/stream (события) + /api/ui/status/stream.
- Config: /api/ui/config/{validate,apply,rollback}.
- Status: /api/ui/status/{overview,components,slo}.
- Opportunities: GET /api/opportunities (paper: мок/пусто).
- Metrics: /metrics (Prometheus), /metrics/latency (histogram dump).

## 3) Каталоги
- app/{routers,services,util,db}.py; configs/*.yaml; docs/*.md; tests/*.py; deploy/*.service.

## 4) Окружение
- Python 3.12; `make venv fmt lint typecheck test run-paper kill`.
- Ruff/Black/Mypy; Pytest+Coverage; .editorconfig.

## 5) БД/миграции
- alembic init; версия 0001: таблица config_changes(id, ts, op, actor, token, blob).
- Включить WAL; снапшот перед миграцией, auto-rollback при провале.

## 6) OBS/SLO
- Кастомные метрики: ws_gap_ms, order_cycle_ms, reject_rate, cancel_fail_rate, recon_mismatch, max_day_drawdown_bps.
- Status thresholds: configs/status_thresholds.yaml; сервис преобразует в OK/WARN/ERROR/HOLD.

## 7) Smoke/Acceptance
- test_smoke: проверка 200 на ключевых эндпоинтах; JSON схемы System Status.
- Логи без ERROR; /openapi.json доступен.

## 8) Release/Canary/Rollback
- scripts: release.sh / rollback.sh; deploy: crypto-bot.service, crypto-bot@.service.
- Canary-порт :9000; переключение симлинка; DoD ≥15 минут зелёных проверок.

## 9) Порядок раскатки
- Local→paper; testnet/shadow (моки данных); canary; ramp-up.
