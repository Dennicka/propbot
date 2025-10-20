# PropBot v6.3.2 — обзор

## Назначение
PropBot — бумажный профиль кросс-биржевого арбитража (Binance UM ↔ OKX Perps). Сервис включает REST API, WebSocket-стрим, систему статусов, конфиг-пайплайн и P0-гардрейлы в SAFE_MODE.

## Структура репозитория
- `app/` — FastAPI-приложение, сервисы статусов, runtime, арбитражный движок, адаптеры бирж.
- `configs/` — профили (`paper`, `testnet`, `live`) и пороги SLO.
- `deploy/` — скрипты релиза/отката и unit-файлы systemd.
- `docs/` — операторская документация, гайды по арбитражу и рискам.
- `tests/` — pytest-набор (unit + mocked integration).

## Быстрый старт (paper)
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.server_ws:app --reload
# smoke
curl -s http://127.0.0.1:8000/api/health | jq
curl -s http://127.0.0.1:8000/api/ui/status/overview | jq
```

### Переменные окружения
- `EXCHANGE_PROFILE` — `paper|testnet|live` (по умолчанию paper).
- `SAFE_MODE` — `true` (dry-run до approvals).
- `ALLOW_LIVE_ORDERS` — 0 (CI запрет live).

### Основные возможности
- API: `/api/ui/*`, `/api/arb/*`, `/api/deriv/*`, `/metrics`, `/metrics/latency`.
- System Status ≥20 компонентов, SLO/thresholds читаются из YAML.
- Arbitrage engine: preflight, подсчёт edge, state-machine исполнения с rescue.
- Two-Man Rule и SAFE_MODE для перехода в live.
- Config pipeline с валидацией по Pydantic и rollback.
- Prometheus + вспомогательные метрики latency.

### Тесты и CI
```bash
pytest -q
```
Workflow `.github/workflows/ci.yml` запускает pytest + coverage (≥60%) и блокирует merge до зелёного статуса.

