# Derivatives Setup Guide

Гид описывает полный путь от подготовки `.env` до тестовой сделки на деривативном арбитраже Binance ↔ OKX.

## 1. Подготовка `.env`

Скопируйте шаблон `.env.example` → `.env` и заполните testnet-ключи. Значения по умолчанию уже выставляют безопасные режимы:

```dotenv
MODE=testnet
SAFE_MODE=true
POST_ONLY=true
REDUCE_ONLY=true

BINANCE_UM_API_KEY_TESTNET=...
BINANCE_UM_API_SECRET_TESTNET=...

OKX_API_KEY_TESTNET=...
OKX_API_SECRET_TESTNET=...
OKX_API_PASSPHRASE_TESTNET=...
```

Файл автоматически подхватывается при запуске приложения (`app/__init__.py` грузит `.env` без изменения боевого окружения). Вручную подгрузить значения можно командой `export $(grep -v '^#' .env | xargs)`.

> ⚠️ SAFE_MODE, POST_ONLY и REDUCE_ONLY должны оставаться включёнными, пока два оператора не одобрят выход в боевой режим.

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
2. Убедитесь, что профиль `testnet` выбран через `.env` (значение `MODE`).
3. Запустите сервис: `PYTHONPATH=. uvicorn app.server_ws:app --host 0.0.0.0 --port 8000`.
4. Проверьте доступность: `/api/health`, `/api/ui/status/overview`, `/api/deriv/status`, `/api/status/slo`.

## 5. Префлайт и выполнение

1. Выполните префлайт:
   ```bash
   curl -s -X POST http://127.0.0.1:8000/api/arb/preview | jq
   ```
   Вы получите отчёт с проверкой connectivity, режимов, фильтров и edge после комиссий.
2. SAFE_MODE остаётся `true` — исполнение `/api/arb/execute` вернёт dry-run план с шагами state-machine.
3. Для реальной тестовой пары сделок:
   - Соберите два approvals в UI (`/api/ui/approvals`).
   - Перезапустите сервис с `SAFE_MODE=false` (только после успешного префлайта).
   - Запустите `curl -s -X POST http://127.0.0.1:8000/api/arb/execute | jq`.
4. После сделки выполните `POST /api/hedge/flatten`, чтобы закрыть оставшиеся позиции.

Дополнительно доступен скрипт `scripts/testnet_smoke.py`, который запускает dry-run, выводит предторговый отчёт и пишет лог `logs/testnet_smoke.log`. Запускайте его после поднятия `uvicorn` или в отдельном процессе:

```bash
python scripts/testnet_smoke.py  # dry-run, SAFE_MODE остается включённым
```

> Скрипт отправит реальные testnet-ордера только если одновременно выполнены условия `SAFE_MODE=false` и `ENABLE_PLACE_TEST_ORDERS=true` в окружении.

## 6. Funding / Edge Policy

- `include_next_window=true` — учитывает ставку следующего расчётного окна.
- `avoid_window_minutes` блокирует запуск арбитража вблизи расчёта funding.
- `min_edge_bps`, `max_leg_slippage_bps`, `post_only_maker` и `prefer_maker` задают требования к спреду и режиму исполнения (IOC или Post Only).
