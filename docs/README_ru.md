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

## Быстрый старт (testnet)
```bash
export EXCHANGE_PROFILE=testnet
export ENABLE_PLACE_TEST_ORDERS=1
export BINANCE_UM_API_KEY_TESTNET=... # тестовые ключи
export BINANCE_UM_API_SECRET_TESTNET=...
export OKX_API_KEY_TESTNET=...
export OKX_API_SECRET_TESTNET=...
export OKX_API_PASSPHRASE_TESTNET=...
uvicorn app.server_ws:app --reload
pytest -k "api_flow and not paper"  # быстрая проверка маршрутов
```

### Переменные окружения
- `EXCHANGE_PROFILE` — `paper|testnet|live` (по умолчанию paper).
- `SAFE_MODE` — `true` (строгий dry-run без фактического исполнения).
- `DRY_RUN_ONLY` — `true` (симуляция через PaperBroker даже вне SAFE_MODE).
- `ORDER_NOTIONAL_USDT` — базовый размер заявки цикла.
- `MAX_SLIPPAGE_BPS` — допущенный слиппедж для построения плана.
- `MIN_SPREAD_BPS` — минимальный чистый спред для статуса viable.
- `ENABLE_PLACE_TEST_ORDERS` — разрешение маршрутизации в тестнет-брокеры.
- `MAX_POSITION_USDT__{SYMBOL}` — риск-лимит на позицию по символу.
- `MAX_OPEN_ORDERS__{VENUE}` — лимит открытых ордеров по venue.
- `MAX_DAILY_LOSS_USDT` — дневной стоп.
- `ALLOW_LIVE_ORDERS` — 0 (CI запрет live).

### Основные возможности
- API: `/api/ui/*`, `/api/arb/*`, `/api/deriv/*`, `/metrics`, `/metrics/latency`.
- System Status ≥20 компонентов, SLO/thresholds читаются из YAML.
- Arbitrage engine: preflight, подсчёт edge, state-machine исполнения с rescue.
- Two-Man Rule и SAFE_MODE для перехода в live.
- Config pipeline с валидацией по Pydantic и rollback.
- Prometheus + вспомогательные метрики latency.

### Операционный плейбук
- SAFE_MODE держит стратегию в режиме планирования; снять можно через `/api/ui/resume` при наличии двух approvals.
- `/api/arb/preview` использует агрегатор маркет-данных (WS → cache → REST) и возвращает `spread_bps`, `venues`, `risk_reason`.
- `/api/arb/execute` в SAFE_MODE/DRY_RUN отдаёт симуляцию, вне — маршрутизирует с пост-онли/IOC и тайм-аутами.
- `/api/ui/state` теперь публикует `risk_blocked`, `risk_reasons`, последние спреды и журнал ордеров.
- `/api/ui/cancel_all`, `/api/ui/close_exposure`, `/api/ui/kill` — горячие кнопки UI; kill включает SAFE_MODE и дёргает cancel-all.
- Реконсиляция `FillReconciler` пишет fills в SQLite, обновляет позиции и PnL; статус доступен в UI.
- Для paper-профиля заявки и позиции хранятся в `data/ledger.db`; очистка — `python -c "from app import ledger; ledger.reset()"`.

### Тесты и CI
```bash
pytest -q
```
Workflow `.github/workflows/ci.yml` запускает pytest + coverage (≥60%) и блокирует merge до зелёного статуса.

