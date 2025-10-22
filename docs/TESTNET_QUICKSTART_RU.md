# Testnet Quickstart (RU)

Ниже приведён чек-лист для запуска PropBot в режиме тестнетов Binance UM и OKX Perpetual.

## 1. Переменные окружения

1. Скопируйте `.env.example` в `.env` и добавьте ключи тестнета:

   ```dotenv
   BINANCE_UM_API_KEY_TESTNET=...
   BINANCE_UM_API_SECRET_TESTNET=...
   OKX_API_KEY_TESTNET=...
   OKX_API_SECRET_TESTNET=...
   OKX_API_PASSPHRASE_TESTNET=...
   ```

2. Установите профиль и флаги безопасности:

   ```dotenv
   PROFILE=testnet
   SAFE_MODE=true
   DRY_RUN_ONLY=true
   POST_ONLY=true
   REDUCE_ONLY=false
   ENABLE_PLACE_TEST_ORDERS=false
   ```

   Пока `ENABLE_PLACE_TEST_ORDERS=false`, брокер будет работать как paper-симулятор, но проверит наличие API-ключей. Для отправки реальных тестовых заявок выставьте `ENABLE_PLACE_TEST_ORDERS=true` **и** выключите `SAFE_MODE`.

## 2. Запуск API

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Проверьте `/api/ui/state` — в блоке `loop` отображается состояние авто-цикла (RUN/HOLD, последний план, ошибка).

### Управление циклом

- `POST /api/ui/resume` — старт авто-loop (требует `SAFE_MODE=false`).
- `POST /api/ui/hold` — остановка цикла.
- `POST /api/ui/reset` — сброс счётчиков и очистка последнего плана/исполнения.

## 3. CLI auto-loop

Для короткого теста используйте новый режим:

```bash
python -m app.cli loop \
  --env testnet \
  --pair BTCUSDT \
  --venues binance-um okx-perp \
  --notional 25 \
  --cycles 3
```

Команда прогонит указанное число циклов `preview → execute`, запишет события (`loop_cycle`, `loop_plan_unviable`) в `data/ledger.db/events` и вернёт управление. Параметры `--pair`, `--venues`, `--notional` сохраняются в state и отображаются в UI (карточка **Last Plan**, `/api/ui/secret`).

Для быстрого прогона без ограничений по числу повторов опустите `--cycles` (по умолчанию бесконечный режим до `Ctrl+C`).

### HTTP-примеры

Получить предпросмотр плана и одновременно записать его в UI:

```bash
curl -X POST http://127.0.0.1:8000/api/arb/preview \
  -H 'Content-Type: application/json' \
  -d '{"pair": "BTCUSDT", "notional": 50}'
```

Проверить параметры авто-цикла и его статус:

```bash
curl http://127.0.0.1:8000/api/ui/secret | jq
```

## 4. Проверка позиций/балансов

В тестовом режиме брокер агрегирует позиции через HTTP-клиентов Binance/OKX (при включённом `ENABLE_PLACE_TEST_ORDERS`). Балансы отображаются из локального леджера.

## 5. Безопасность

- Никогда не включайте `ENABLE_PLACE_TEST_ORDERS` вместе с production-ключами.
- Убедитесь, что `MAX_SLIPPAGE_BPS`, `POST_ONLY` и `REDUCE_ONLY` настроены в конфиге `configs/config.testnet.yaml`.
- Для возврата к paper-режиму достаточно вернуть `ENABLE_PLACE_TEST_ORDERS=false` и/или снова включить `SAFE_MODE`.

Удачных тестов!
