# PropBot — Test Bot MVP

Минимальная реализация paper/testnet арбитражного бота с FastAPI, бумажным брокером, SQLite-леджером и веб-интерфейсом «System Status». Сервис запускается локально, в SAFE_MODE реальные ордера заблокированы, а все исполнения проходят через симулятор.

## 1. Подготовка окружения

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Скопируйте `.env.example` в `.env` и заполните значения при необходимости:

```bash
cp .env.example .env
```

По умолчанию `SAFE_MODE=true`, `DRY_RUN_ONLY=false`. Для работы с тестнетами Binance UM / OKX заполните API-ключи. Переменная `PROFILE` переключает конфигурацию (`paper` / `testnet` / `live`). Для реальной отправки заявок на тестнет необходимо явно отключить `SAFE_MODE`, установить `DRY_RUN_ONLY=false` и выставить `ENABLE_PLACE_TEST_ORDERS=1` (при отсутствии флага брокер автоматически переключится в paper-режим).

## 2. Запуск API и UI

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Доступные эндпоинты:

- `GET /healthz` — проверка живости.
- `POST /api/arb/preview` — расчёт плана (legs, комиссии, ожидаемый PnL). Проверки риск-лимитов выполняются перед оценкой спреда, поэтому при превышении `max_position_usdt` / `max_open_orders` / `max_daily_loss_usdt` поле `reason` укажет соответствующий ключ.
- `POST /api/arb/execute` — исполнение через брокер/маршрутизатор (в SAFE_MODE возвращает 403). В dry-run можно отправлять ответ превью «как есть» — эндпоинт принимает дополнительные поля и симулирует отчёт даже если `viable=false`.
- `POST /api/ui/hold` / `POST /api/ui/resume` / `POST /api/ui/reset` — управление циклом.
- `GET /api/ui/state` — агрегированное состояние, флаги, PnL/экспозиции, события, статус auto-loop. В ответ добавлен блок `portfolio` с позициями (venue, qty, entry/mark, uPnL/rPnL), балансами по биржам и итоговыми PnL.
- `GET /api/ui/orders` — снимок открытых ордеров, позиций и последних fill'ов.
- `POST /api/ui/cancel_all` — массовый отзыв ордеров (только `ENV=testnet`).
- `POST /api/ui/close_exposure` — запрос на закрытие экспозиции (через `hedge.flatten`).
- `GET /api/ui/plan/last` — последний сохранённый план.
- `PATCH /api/ui/control` — частичное обновление runtime-параметров (только `paper`/`testnet` + `SAFE_MODE=true`).
- `GET /api/ui/events` — страница журнала событий с пагинацией (`offset`, `limit` ≤ 1000), фильтрами (`venue`, `symbol`, `level`, `search`) и окном по времени (`since`/`until` ≤ 7 суток).
- `GET /api/ui/events/export` — выгрузка событий в `csv`/`json` с теми же фильтрами (поддерживает `offset`/`limit`).
- `GET /api/ui/portfolio/export` — экспорт текущего снапшота портфеля (позиции/балансы) в `csv` или `json`.
- `GET /api/risk/state` — агрегированное состояние риск-монитора (позиций и сработавших лимитов).

> При включённом `AUTH_ENABLED=true` все POST/PATCH/DELETE-запросы к `/api/ui/*` и `/api/arb/*` требуют заголовок `Authorization: Bearer <API_TOKEN>`. Запросы чтения (`GET`/`HEAD`/`OPTIONS`) остаются публичными.

Для всех мутаций можно (и рекомендуется) передавать заголовок `Idempotency-Key`. Сервис нормализует JSON-тело и кэширует ответ на `IDEM_TTL_SEC` секунд (по умолчанию 600). Повторный запрос с тем же методом/путём/телом вернёт сохранённый ответ и установит заголовок `Idempotent-Replay: true`.

На уровне приложения действует токен-бакапный rate limit: идентификатором служит bearer-токен (если передан) либо IP клиента. Значения по умолчанию — `API_RATE_PER_MIN=30` с `API_BURST=10`. При превышении квоты возвращается `HTTP 429` с телом

```json
{"detail": "rate limit exceeded"}
```

и заголовками `X-RateLimit-Remaining` (оставшийся запас) и `X-RateLimit-Reset` (секунд до полного восстановления бакета).

Эндпоинт `PATCH /api/ui/control` нормализует входящие значения и валидирует диапазоны: `max_slippage_bps ∈ [0, 50]`, `min_spread_bps ∈ [0, 100]`, `order_notional_usdt ∈ [1, 1_000_000]`. Поля с `null` пропускаются без ошибок. После применения изменения сохраняются в файл `data/runtime_state.json`, и сервис подхватывает последний снапшот контролов при рестарте.

Веб-страница «System Status» доступна на `http://localhost:8000/`. Она отображает основные флаги, экспозиции, PnL и журнал событий, а также включает:

- кнопку **Edit Config** (панель PATCH `/api/ui/control` с валидацией и ограничениями по профилю);
- карточку Runtime Flags с актуальным снапшотом контролов (значения после нормализации PATCH);
- карточку Orders & Positions с табами «Open Orders», «Positions» (кнопки Close по каждой строке) и «Fills», а также кнопки Cancel All по venue;
- карточку Events с фильтрами по venue/level/search, отображением общего количества записей, кнопкой **Download CSV** (переход к `/api/ui/events/export`) и кнопкой догрузки через `/api/ui/events`;
- карточку Exposures с подробной таблицей позиций (добавлен столбец `venue_type`) и мини-таблицей Balances с итоговой суммой USDT.

## 3. CLI и планировщик

Запуск одиночного цикла (создаёт артефакт `artifacts/last_plan.json`):

```bash
python -m app.cli exec --profile paper
```

Для непрерывного прогона добавьте `--loop`. CLI автоматически включает `SAFE_MODE` и dry-run в paper/testnet профилях.

Автоматический цикл превью/исполнения (paper/testnet профили, логирование в SQLite):

```bash
python -m app.cli loop --env paper --cycles 10
```

Без флага `--cycles` процесс работает бесконечно; события (`loop_cycle`, `loop_plan_unviable`) пишутся в `data/ledger.db`.

### Экспорт через CLI

Отдельный модуль `api_cli.py` позволяет выгружать события и портфель в артефакты без открытия UI:

```bash
python -m api_cli events --limit 500 --format csv --venue binance-um --out artifacts/events.csv
python -m api_cli portfolio --format json --out artifacts/portfolio.json
python -m api_cli events --base-url https://propbot.local --api-token "$API_TOKEN"
python -m api_cli events --idempotency-key retry-42
PROPBOT_API_TOKEN="$API_TOKEN" python -m api_cli portfolio --format json
```

Поддерживаются все параметры фильтрации `/api/ui/events` (`--level`, `--search`, `--since`, `--until`, `--symbol`). По умолчанию база API — `http://localhost:8000`, изменяется флагом `--base-url`. Токен авторизации можно передать флагом `--api-token` или через переменные окружения `PROPBOT_API_TOKEN` / `API_TOKEN`.
Флаг `--idempotency-key` прокидывает одноимённый заголовок и позволяет безопасно ретраить мутации через CLI.

## 4. Леджер и журнал

- Файл `data/ledger.db` создаётся автоматически (SQLite).
- Таблицы: `orders`, `fills`, `positions`, `balances`, `events`.
- После каждого исполнения обновляются экспозиции и PnL (реализованная/нереализованная по mark/last price), которые отображаются в `/api/ui/state` и на дашборде.

Для просмотра содержимого можно использовать `sqlite3 data/ledger.db` или сторонние инструменты.

## 5. Тесты и качество

```bash
pytest -q
```

CI workflow `test` запускает тот же набор. Перед коммитом убедитесь, что рабочее дерево чистое и тесты зелёные.

## 6. Особенности безопасности

- `SAFE_MODE` блокирует исполнение ордеров. Для симуляции достаточно включить `DRY_RUN_ONLY=true` и оставить SAFE_MODE включённым.
- `TWO_MAN_RULE=true` требует двух одобрений для реального запуска (в текущем MVP проверка реализована в роутере и отключает live-выполнение).
- Параметры по умолчанию задаются в `.env` и `configs/config.*.yaml`.
- Для блокировки небезопасных мутаций включите `AUTH_ENABLED=true` и задайте общий токен через `API_TOKEN=<случайная_строка>`. Клиентам необходимо добавлять заголовок `Authorization: Bearer <API_TOKEN>` ко всем POST/PATCH/DELETE запросам, например:

  ```bash
  export AUTH_ENABLED=true
  export API_TOKEN="super-secret-token"
  uvicorn app.main:app --host 0.0.0.0 --port 8000
  curl -X POST http://localhost:8000/api/ui/kill -H "Authorization: Bearer $API_TOKEN"
  python -m api_cli events --api-token "$API_TOKEN"
  ```

## 7. Полезные команды Makefile

```
make venv      # создание виртуального окружения
make run       # запуск uvicorn app.main:app
make test      # pytest
make fmt       # форматирование (ruff + black)
make lint      # линтеры
make dryrun.once  # одиночный запуск CLI
make dryrun.loop  # непрерывный dry-run
```

## 8. Документация

- `docs/DERIV_SETUP_GUIDE.md` — обновлённая инструкция по настройке тестнета и проверке SAFE_MODE.
- `docs/TESTNET_QUICKSTART_RU.md` — быстрый запуск Binance UM / OKX testnet с флагом `ENABLE_PLACE_TEST_ORDERS`.
- `CODEX_TASK_TEST_BOT_MVP.md` — исходная постановка задания.

## 9. Обновление до Pydantic v2

- Вся валидация и сериализация переводится на Pydantic v2. Используйте `model_dump()` вместо устаревшего `dict()` и `model_validate()`/`TypeAdapter` вместо `parse_obj`/`parse_raw`.
- Конфигурация моделей теперь задаётся через `model_config = ConfigDict(...)`; параметры `class Config:` и `orm_mode` более не поддерживаются.
- Поля с `Field(..., validate_default=True)` требуют явного флага `validate_default=True`, если важно проверять дефолтные значения.
- Проверьте кастомные валидаторы: декораторы `@model_validator` и `@field_validator` заменяют `@root_validator`/`@validator` и принимают другие сигнатуры.
- Используйте `.model_dump()`/`.model_dump_json()` в местах сериализации для API/UI; старые методы вызовут предупреждения или ошибки.
