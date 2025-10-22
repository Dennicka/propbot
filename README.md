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
- `POST /api/arb/preview` — расчёт плана (legs, комиссии, ожидаемый PnL).
- `POST /api/arb/execute` — исполнение через брокер/маршрутизатор (в SAFE_MODE возвращает 403).
- `POST /api/ui/hold` / `POST /api/ui/resume` / `POST /api/ui/reset` — управление циклом.
- `GET /api/ui/state` — агрегированное состояние, флаги, PnL/экспозиции, события, статус auto-loop.
- `GET /api/ui/orders` — снимок открытых ордеров, позиций и последних fill'ов.
- `POST /api/ui/cancel_all` — массовый отзыв ордеров (только `ENV=testnet`).
- `POST /api/ui/close_exposure` — запрос на закрытие экспозиции (через `hedge.flatten`).
- `GET /api/ui/plan/last` — последний сохранённый план.

Веб-страница «System Status» доступна на `http://localhost:8000/`. Она отображает основные флаги, экспозиции, PnL и журнал событий, таблицы открытых ордеров/позиций/последних fill'ов и содержит кнопки HOLD/RESUME, Cancel All и Close Exposure.

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

## 4. Леджер и журнал

- Файл `data/ledger.db` создаётся автоматически (SQLite).
- Таблицы: `orders`, `fills`, `positions`, `balances`, `events`.
- После каждого исполнения обновляются экспозиции и PnL, которые отображаются в `/api/ui/state` и на дашборде.

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
