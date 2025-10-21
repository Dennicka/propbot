# Derivatives Setup Guide

Гид описывает полный путь от подготовки `.env` до тестовой сделки на деривативном арбитраже Binance ↔ OKX.

## 1. Подготовка `.env`

Создайте файл `.env` в корне репозитория и сохраните testnet-ключи:

```dotenv
# профиль и режим по умолчанию
EXCHANGE_PROFILE=testnet
SAFE_MODE=true

# Binance USD-M Futures testnet
BINANCE_UM_API_KEY_TESTNET=...
BINANCE_UM_API_SECRET_TESTNET=...

# OKX demo (paper trading)
OKX_API_KEY_TESTNET=...
OKX_API_SECRET_TESTNET=...
OKX_API_PASSPHRASE_TESTNET=...
```

> ⚠️ SAFE_MODE должен оставаться включённым, пока два оператора не одобрят выход в боевой режим.

Загрузите переменные в окружение: `export $(grep -v '^#' .env | xargs)`.

## 2. Binance USD-M Futures (testnet)

1. Включите testnet в личном кабинете Binance Futures и создайте API-ключ с правами *Futures → Trade*.
2. Клиент `BinanceUMClient` автоматически ставит **hedge mode**, `isolated` маржу и плечо из `configs/config.testnet.yaml` при старте сервиса.
3. Верифицируйте режимы:
   ```bash
   curl -s http://127.0.0.1:8000/api/deriv/status | jq '.venues[] | select(.venue=="binance_um")'
   ```
4. Проверяйте торговые фильтры `/api/deriv/status` и `GET /api/arb/preview` — в SAFE_MODE данные приходят из тестового «бумажного» слоя, в live режиме используются реальные REST вызовы `exchangeInfo`, `commissionRate`, `depth`.

## 3. OKX Perpetual Swaps (demo)

1. На OKX включите *Demo trading*, создайте API key/secret/passphrase.
2. Наш адаптер переводит аккаунт в `long_short_mode`, `isolated` и устанавливает плечо для каждого инструмента из конфига.
3. Проверьте настройки:
   ```bash
   curl -s http://127.0.0.1:8000/api/deriv/status | jq '.venues[] | select(.venue=="okx_perp")'
   ```
4. Для анализа фильтров/комиссий используйте `GET /api/deriv/status` и `GET /api/arb/edge` — данные поступают из `/api/v5/public/instruments` и `/api/v5/account/trade-fee`.

## 4. Запуск в testnet

1. Активируйте виртуальное окружение и зависимости (`pip install -r requirements.txt`).
2. Убедитесь, что профиль `testnet` выбран через `.env`.
3. Запустите сервис: `uvicorn app.server_ws:app --host 0.0.0.0 --port 8000`.
4. Проверьте доступность: `/api/health`, `/api/ui/status/overview`, `/api/deriv/status`.
5. Просмотрите флаги исполнения:
   ```bash
   curl -s http://127.0.0.1:8000/api/ui/state | jq '.flags'
   ```
   Пример ответа:
   ```json
   {
     "MODE": "testnet",
     "SAFE_MODE": true,
     "POST_ONLY": true,
     "REDUCE_ONLY": false,
     "ENV": "testnet",
     "DRY_RUN": true,
     "ORDER_NOTIONAL_USDT": 50.0,
     "MAX_SLIPPAGE_BPS": 2,
     "TAKER_FEE_BPS_BINANCE": 2,
     "TAKER_FEE_BPS_OKX": 2
   }
   ```

## 5. Dry-run arbitrage

1. Получите превью арбитража, указав символ, нотионал и допуск по слиппеджу:
   ```bash
   curl -s "http://127.0.0.1:8000/api/arb/preview?symbol=BTCUSDT&notional=50&slippage_bps=2" | jq
   ```
   Ответ содержит legs с рассчитанными ценами покупки/продажи, комиссией и оценкой PnL после вычета `used_fees_bps` и указанного `used_slippage_bps`.
2. SAFE_MODE по умолчанию включён и блокирует исполнение: `POST /api/arb/execute` вернёт `403 SAFE_MODE blocks execution` даже при передаче валидного плана. Это защита от случайных запусков.
3. Для симуляции в dry-run режиме (без реальных ордеров) временно отключите SAFE_MODE вручную, но оставьте `DRY_RUN=true`. В этом случае `POST /api/arb/execute` вернёт отчёт `simulated=true` и рассчитанный PnL.
4. Полноценное исполнение двух ног разрешайте только после двойного одобрения (`/api/ui/approvals`) и перевода SAFE_MODE в `false`. После отработки не забудьте вызвать `POST /api/hedge/flatten` для закрытия позиций.

## 6. Funding / Edge Policy

- `include_next_window=true` — учитывает ставку следующего расчётного окна.
- `avoid_window_minutes` блокирует запуск арбитража вблизи расчёта funding.
- `min_edge_bps`, `max_leg_slippage_bps`, `post_only_maker` и `prefer_maker` задают требования к спреду и режиму исполнения (IOC или Post Only).

## Testnet smoke

Для наблюдения за автоматической проверкой тестнета откройте GitHub Actions → **Testnet Smoke** → **Run workflow**. Этот джоб запускается вручную или по ночному расписанию и требует секретов, поэтому триггер по pull request отключён. Если smoke упал, нажмите "Run workflow" на ветке `main`, убедившись, что секреты заданы.
