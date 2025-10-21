# ЗАДАНИЕ ДЛЯ CODEX — **TEST BOT MVP (paper/testnet) В ОДИН PR**

Цель: выдать **полностью рабочий тестовый арбитражный бот** (без реальных денег), который можно
запустить локально и/или на сервере: **API + минимальный дашборд + постановка/отмена ордеров на paper/testnet**,
учёт в журнале (SQLite), реконcиляция, базовые метрики/статус, и зелёный CI. Весь объём — в **один PR**,
который проходит чек `test` и готов к merge **без вопросов**.

---

## 0) Ветка и ограничения

- Создай ветку: `codex/test-bot-mvp-v1` (или похожее имя).  
- **CI чек должен называться `test`** (он уже настроен в Ruleset).  
- Не менять существующие правила RuleSet; использовать существующий `CI / test`.  
- По умолчанию **SAFE_MODE=true** и **DRY_RUN_ONLY=false** — т.е. реальных ордеров нет, но **paper/testnet** должны работать.

---

## 1) Профили, ENV и конфиги

- `.env.example` должен содержать *минимум*:
  ```env
  PROFILE=paper             # paper | testnet | live
  MODE=testnet              # для маршрутизации клиентов котировок/ордеров
  SAFE_MODE=true            # блокирует любые live операции
  DRY_RUN_ONLY=false        # если true — форсируем симуляцию
  POST_ONLY=true
  REDUCE_ONLY=true
  TWO_MAN_RULE=true
  # paper/testnet ключи (значения можно оставить пустыми)
  BINANCE_UM_API_KEY_TESTNET=
  BINANCE_UM_API_SECRET_TESTNET=
  OKX_API_KEY_TESTNET=
  OKX_API_SECRET_TESTNET=
  OKX_API_PASSPHRASE_TESTNET=
  # universe/venues
  UNIVERSE=BTCUSDT,ETHUSDT
  VENUES=binance-um,okx-perp
  ```
- Конфиги в `configs/` (если применимо): `config.paper.yaml`, `config.testnet.yaml` — c явными флагами SAFE и ограничений.

---

## 2) Точка входа FastAPI + маршруты

- Добавить **явную точку входа**: `app/main.py`
  ```python
  from fastapi import FastAPI
  from app.routers import ui_state, ui_config, ui_control, arb, deriv  # использовать существующие роутеры
  def create_app():
      app = FastAPI(title="PropBot API")
      # регистрируй существующие роутеры и новые (см. ниже)
      # app.include_router(...)
      return app
  app = create_app()
  ```
- Эндпоинты, которые должны быть доступны и задокументированы в `docs/OPENAPI.md`:
  - `GET /api/ui/state` — runtime флаги, guard/SLO/incident кратко (уже есть, привести к единому контракту).
  - `POST /api/arb/preview` — вернуть план (legs, fees, expected pnl) **без размещения**.
  - `POST /api/arb/execute` — в `paper/testnet` режимах **разместить ордера** через брокер/роутер (см. §3), в SAFE_MODE — отказать.
  - `POST /api/ui/hold` — HOLD (остановить исполнение), `POST /api/ui/resume` — RESUME.
  - `GET /api/ui/plan/last` — последний собранный план (для UI).
  - `GET /healthz` — liveness.
- Запуск: `uvicorn app.main:app --port 8000` (и `/docs` должен открываться).

---

## 3) Broker/Router (paper + testnet)

- Реализовать слой `app/broker/` с унифицированным интерфейсом:
  ```python
  class Broker:
      async def create_order(self, venue, symbol, side, qty, price=None, type="LIMIT", post_only=True, reduce_only=False): ...
      async def cancel(self, venue, order_id): ...
      async def positions(self, venue): ...
      async def balances(self, venue): ...
  ```
- **Адаптеры**:
  - `paper`: симуляция ордербука/матчинга (простая), записи в **ledger** (см. §4).
  - `binanceum_testnet`, `okx_perp_testnet`: реальные вызовы **тестнет** эндпоинтов (использовать ключи из ENV).
    - Все вызовы **обязаны** уважать `SAFE_MODE`/`DRY_RUN_ONLY` и Two-Man Rule (если включен) → при нарушении вернуть 4xx и не размещать.
- `Router`: маршалит заявки от «двух ног» арбитража в соответствующие адаптеры (binance/okx/paper).

---

## 4) Ledger (SQLite) + Recon + PnL/Exposure

- SQLite `data/ledger.db` (создать при первом запуске, `alembic` опционально). Таблицы:
  - `orders(id, venue, symbol, side, qty, price, status, client_ts, exchange_ts, idemp_key)`
  - `fills(id, order_id, venue, symbol, side, qty, price, fee, ts)`
  - `positions(venue, symbol, base_qty, avg_price, ts)`
  - `balances(venue, asset, qty, ts)`
  - `events(ts, level, code, payload)`
- Idempotency: ключ `idemp_key` на стороне router/broker.
- **Recon** фоновой задачей: сверить биржу ⇄ ledger, поправить статусы ордеров, посчитать exposure/PnL.
- Расширить `/api/ui/state`: добавить `exposures`, `pnl`, `recon_status`.

---

## 5) Мини-UI «System Status» (минимум)

- Одна страница (можно на FastAPI templates или простом React/Streamlit) со следующими блоками:
  - Overall: SAFE/HOLD/RESUME, последние инциденты.
  - Buttons: **Start/Resume**, **Hold**, `Apply Config` (мок под Two-Man Rule).
  - Табличка: `exposures`, `pnl`, кратко `last_plan` (symbol, venues, notional, expected pnl).
- Веб-сокет или периодический polling 2–5 сек.

---

## 6) Командная строка / утилиты

- `make` цели: `make venv`, `make run`, `make test`, `make fmt`, `make lint`, `make dryrun.once`, `make dryrun.loop`.
- CLI: `python -m app.cli exec --profile testnet --loop` (исполняет план по профилю через Router/Broker, SAFE_MODE учитывается).

---

## 7) CI / Workflows

- Оставить существующий workflow `CI / test` (pytest).
- Добавить артефакт `last_plan.json` при smoke-run’ах, и Markdown-summary с ключевыми метриками.
- Не трогать Ruleset; чек должен остаться с именем `test` и **зелёным**.

---

## 8) Тесты (обязательно)

- **Юнит**: broker-paper (матчинг), idempotency, router fan-out, SAFE_MODE/Two-Man блокировки.
- **Интеграция**: `POST /api/arb/preview` и `.../execute` в `PROFILE=paper` и `PROFILE=testnet` (без реальных денег).
- **Merge safety**: рабочее дерево чистое; `pytest -q` зелёный.
- Прикрепить в PR артефакт с `last_plan.json` и скриншот страницы «System Status».

---

## 9) Документация

- Обновить `docs/DERIV_SETUP_GUIDE.md` и `README.md`:
  - Быстрый старт: venv, `.env`, запуск API/CLI, где dашборд.
  - Как включить **testnet** (секреты), как убедиться, что SAFE_MODE не даёт ставить live.
  - Как смотреть `ledger.db` и что такое recon.

---

## 10) Acceptance / Done

- Локально: `uvicorn app.main:app` поднимается; `/docs` открывается; превью и исполнение работают в `paper/testnet` (реальных денег нет).  
- Дашборд показывает основные флаги/метрики, кнопки HOLD/RESUME работают.  
- CI зелёный, артефакт с планом приложен, README/гайды обновлены.  
- PR готов к merge: **никаких предупреждений и «висящих» конфликтов**.

> Если упираешься в лимиты: коммить поэтапно **в ту же ветку** `codex/test-bot-mvp-v1`,
> но PR создавай один; при необходимости — черновой PR и постепенно доводи до «зелёного».