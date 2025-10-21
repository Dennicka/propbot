# Derivatives Setup Guide

Пошаговая инструкция по подготовке тестового арбитражного бота (paper / testnet) и проверке ключевых защит.

## 1. Конфигурация окружения

1. Скопируйте `.env.example` в `.env` и выставьте профиль:

   ```dotenv
   PROFILE=testnet
   MODE=testnet
   SAFE_MODE=true
   DRY_RUN_ONLY=false
   TWO_MAN_RULE=true

   BINANCE_UM_API_KEY_TESTNET=...
   BINANCE_UM_API_SECRET_TESTNET=...
   OKX_API_KEY_TESTNET=...
   OKX_API_SECRET_TESTNET=...
   OKX_API_PASSPHRASE_TESTNET=...
   ```

2. Примените переменные: `export $(grep -v '^#' .env | xargs)`.
3. Убедитесь, что в `configs/config.testnet.yaml` раздел `control` оставляет SAFE_MODE включённым, а лимиты соответствуют тестовому бюджету.

## 2. Старт сервисов

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Проверки:

- `curl -s http://127.0.0.1:8000/healthz` — API жив.
- `curl -s http://127.0.0.1:8000/api/ui/state | jq '.flags'` — убедитесь, что SAFE_MODE и DRY_RUN соответствуют ожиданиям.
- Откройте `http://127.0.0.1:8000/` и убедитесь, что дашборд отображает флаги и метрики.

## 3. Превью и исполнение

1. Получите план:

   ```bash
   curl -s -X POST http://127.0.0.1:8000/api/arb/preview \
     -H "Content-Type: application/json" \
     -d '{"symbol":"BTCUSDT","notional":100,"used_slippage_bps":2}' | jq
   ```

2. Безопасность: пока `SAFE_MODE=true`, попытка `POST /api/arb/execute` вернёт 403.
3. Для чистой симуляции установите `DRY_RUN_ONLY=true` (например, в `.env`) и временно отключите SAFE_MODE перед запуском. После теста верните SAFE_MODE в исходное состояние.

4. Отчёт об исполнении содержит список ордеров, обновлённые экспозиции и сводку PnL. Эти данные попадают в `data/ledger.db` и доступны через `GET /api/ui/state`.

## 4. Работа с CLI

CLI позволяет запускать планировщик без ручных HTTP-запросов:

```bash
python -m app.cli exec --profile testnet --artifact artifacts/last_plan.json
```

- В режиме `--loop` выполняется непрерывный dry-run.
- Артефакт `last_plan.json` содержит временную метку, план и отчёт.

## 5. Контроль рисков

- SAFE_MODE и TWO_MAN_RULE предотвращают живые сделки до явного разрешения.
- `POST /api/ui/hold` переводит движок в HOLD, а `/api/ui/resume` разрешает работу только при `SAFE_MODE=false`.
- Показатели PnL и экспозиций на дашборде позволяют убедиться в корректности закрытия позиций после тестовых сделок.

## 6. Наблюдение за леджером

- Для быстрой проверки используйте `sqlite3 data/ledger.db "SELECT * FROM orders"`.
- Таблица `events` фиксирует изменения режима и исполнений; они же отображаются на странице «System Status».
