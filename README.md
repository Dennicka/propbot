## Production Quickstart

1. Скопируйте `.env.prod.example` в `.env.prod`, задайте все обязательные секреты,
   `APPROVE_TOKEN`, ключи бирж, Telegram, а также значения `REPO` (организация в
   GHCR, например `REPO=my-org`) и `TAG` (например `TAG=main`) для образа в
   `docker-compose.prod.yml`.
2. Запустите стэк: `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d`.
3. Подтвердите, что `DRY_RUN_MODE=true` и сервис стартовал в SAFE_MODE/HOLD
   (см. `safe_mode`, `dry_run_mode` и `hold_active` в статусе).
4. Проверьте состояние через `/api/ui/status/overview`, `/api/ui/status/components`,
   `/api/ui/status/slo` и `/api/ui/positions`.
   - Появление позиции со статусом `partial` означает, что одна нога сделки уже
     исполнена, а вторая остановлена HOLD/лимитом — закройте хвост вручную на
     бирже и восстановите баланс штатными средствами.
5. Убедитесь, что `/healthz` отвечает `{"ok": true}`.
6. Для реальной торговли выполните ручной двухшаговый RESUME: сначала
   `POST /api/ui/resume-request`, затем `POST /api/ui/resume-confirm` с
   `APPROVE_TOKEN`.

⚠️ Без ручного двухшагового RESUME хеджер остаётся в SAFE_MODE/HOLD и не начнёт
реально торговать, даже если контейнер уже запущен.

## Coverage vs spec_archive

- **Боевой контур 24/7**: Текущий репозиторий покрывает хеджер и кросс-биржевой
  арбитраж с HOLD/SAFE_MODE, DRY_RUN, двухоператорным резюмом, журналами PnL и
  runtime snapshot'ами, операторской панелью `/ui/dashboard`, healthcheck'ом и
  build-метаданными (см. раздел `[ok]` в `docs/GAP_REPORT.md`). Эти блоки уже
  используются для безопасного запуска в paper/testnet/live с ручной защитой. 【F:docs/GAP_REPORT.md†L3-L25】
- **Требующее доработки**: Большая часть требований из `docs/spec_archive`
  (многослойный risk/strategy orchestrator, VaR, MSR/RPI, Autopilot UX, RBAC,
  защищённое хранение секретов и т.д.) пока отсутствует. Детальный список
  пробелов, а также рекомендации по месту интеграции и рисковым требованиям
  приведены в секции `[missing]` GAP-отчёта. Планируя production без ручного
  присмотра, используйте этот список как дорожную карту. 【F:docs/GAP_REPORT.md†L27-L126】

## Risk Core scaffold

- Добавлен каркас `RiskCaps`/`RiskGovernor` и in-memory `StrategyBudgetManager`
  для заготовки глобальных и пер-стратегийных лимитов.
- В `.env.example` задекларированы новые переменные `MAX_NOTIONAL_PER_EXCHANGE`
  и `RISK_CHECKS_ENABLED` вместе с глобальными cap'ами.
- ⚠️ Модули пока **не подключены** к исполнителям ордеров и роутерам — включение
  проверок планируется отдельным изменением.

### Production bring-up checklist

1. Клонируйте репозиторий на прод-хост и переключитесь на нужный релиз.
2. Скопируйте шаблон: `cp .env.prod.example .env.prod`. Заполните `REPO` и `TAG`
   для образа в GHCR, задайте уникальные `API_TOKEN` и `APPROVE_TOKEN`, пропишите
   реальные пути `RUNTIME_STATE_PATH`, `POSITIONS_STORE_PATH`, `PNL_HISTORY_PATH`,
   `HEDGE_LOG_PATH`, `OPS_ALERTS_FILE` внутри примонтированного `./data/`. Удалите
   все плейсхолдеры (`TODO`, `change-me` и т.п.).
3. Создайте каталог данных рядом с compose-файлом и выдайте права записи контейнеру:
   `mkdir -p ./data && chown 1000:1000 ./data && chmod 770 ./data`.
4. Проверьте, что в `.env.prod` нет пустых обязательных значений: `APPROVE_TOKEN`,
   все пути к persistent state файлам, биржевые ключи для включённых демонов.
5. Запустите стэк: `docker compose -f docker-compose.prod.yml --env-file .env.prod up -d`.
6. Просмотрите логи: `docker compose logs -f propbot_app_prod`. Убедитесь, что
   появляется запись `PropBot starting with build_version=...` и нет `[FATAL CONFIG]`
   ошибок — это означает, что startup validation прошёл успешно.
7. Проверьте `/healthz` (`curl -fsS http://localhost:8000/healthz`) и `/api/ui/status/overview`
   (с bearer-токеном) до того, как снимать HOLD/SAFE_MODE.

Если startup validation остановил контейнер, выполните `docker compose logs propbot_app_prod`
и устраните ошибки из сообщений `[FATAL CONFIG]` (самые частые причины: пустой
`APPROVE_TOKEN`, оставленные плейсхолдеры в `.env.prod`, отсутствующие пути к файлам
состояния). После исправления перезапустите `docker compose up -d`.

### CapitalManager snapshot

`GET /api/ui/capital` (с тем же bearer-токеном, что и остальные UI-ручки) возвращает
снимок CapitalManager: общий капитал в USDT, лимиты по стратегиям и текущий
используемый notional. Блок `per_strategy_limits` хранит заявленные потолки
notional'а, например:

```json
{
  "cross_exchange_arb": {"max_notional": 50000.0}
}
```

`current_usage` — это фактическая загрузка по стратегиям в момент снимка, с ключом
`open_notional`. Эндпоинт также возвращает `headroom`: оставшийся запас до лимита
(`max_notional - open_notional`).

⚠️ CapitalManager пока выполняет только учёт и планирование лимитов. Он **не**
блокирует сделки автоматически и не вмешивается в текущие исполнители ордеров —
используйте метрики как отчётность и ручной контроль.

### Capital / Per-Strategy Budget

В дополнение к глобальным лимитам риск-менеджера введён бюджет для каждой
стратегии. Менеджер `StrategyBudgetManager` хранит ограничения по
`max_notional_usdt` и `max_open_positions` в runtime-state (рядом с
`data/runtime_state.json`) и учитывает текущую загрузку. Сейчас в продакшн
конфигурации заведен бюджет для `cross_exchange_arb` — значения по умолчанию
подтягиваются из глобальных лимитов (`MAX_TOTAL_NOTIONAL_USDT` и
`MAX_OPEN_POSITIONS`).

Если стратегия выбирает свой бюджет, она блокирует только собственные новые
сделки: попытка открыть ещё одну ногу возвращает `state=BUDGET_BLOCKED` и
`reason=strategy_budget_exceeded`. Глобальный risk manager, SAFE_MODE/HOLD,
`DRY_RUN_MODE` и двухфакторное RESUME продолжают работать без изменений —
пер-стратегийный бюджет добавляет дополнительный уровень защиты капитала.

Мониторить состояние можно через `GET /api/ui/strategy_budget` (роли `viewer`
и `auditor` имеют read-only доступ — auditor видит весь бюджет без права
управления) и новую таблицу «Strategy Budgets» на `/ui/dashboard`.
Там отображается текущий notional и количество открытых позиций против лимитов,
а исчерпанные бюджеты подсвечиваются красным.

### Strategy Budgets (risk accounting)

- In-memory risk accounting держит отдельный дневной бюджет для каждой
  стратегии: `limit_usdt`, фактический расход `used_today_usdt`, остаток
  `remaining_usdt` и `last_reset_ts_utc`. Значения сбрасываются автоматически
  в 00:00 UTC (по epoch-day), поэтому старый убыток не тянется в следующий
  торговый день.
- Блокировка intents происходит только при одновременном выполнении трёх
  условий: `FeatureFlags.risk_checks_enabled()` → `true`,
  `FeatureFlags.enforce_budgets()` → `true` и `runtime_state.control.dry_run_mode`
  → `False`. В DRY_RUN/SAFE_MODE бюджет отображается как превышенный
  (`blocked_by_budget=True`), но фактического SKIP не происходит.
- `GET /api/ui/risk_snapshot` возвращает для каждой стратегии расширенный
  блок `budget` c полями `limit_usdt`, `used_today_usdt`, `remaining_usdt` и
  `last_reset_ts_utc`, а также флаг `blocked_by_budget`.
- Операторы могут обнулять дневной счётчик вручную через
  `POST /api/ui/budget/reset` (payload: `{"strategy": "...", "reason": "..."}`),
  событие фиксируется в `audit_log` (action=`BUDGET_RESET`).
- `/ui/dashboard` показывает отдельную таблицу «Daily Strategy Budgets» с
  колонками `limit`, `used_today`, `remaining`, `last_reset` и статусом
  `BLOCKED/OK`. Под таблицей есть форма ручного сброса и напоминание
  «Автосброс в 00:00 UTC».

### Per-Strategy PnL & Drawdown

- Runtime теперь ведёт отдельный журнал реализованного PnL по каждой стратегии.
  Для каждого имени сохраняются `realized_pnl_today`, `realized_pnl_total`,
  семидневное скользящее окно и `max_drawdown_observed` в абсолютном выражении.
- `/ui/dashboard` показывает блок «Strategy Performance» с этими метриками
  рядом со статусами `frozen`, `budget_blocked` и счётчиком
  `consecutive_failures`. Стратегии с активным freeze или блокировкой бюджета
  подсвечиваются красным.
- `/api/ui/ops_report` и CSV-экспорт содержат секцию `per_strategy_pnl` —
  данные можно забирать в внешние мониторинги без парсинга HTML.
- Freeze по дневному убытку теперь опирается на эти реальные данные из
  персистентного PnL-трекера. Как и раньше, ручной UNFREEZE возможен
  оператором, но действие фиксируется в audit log.

### Strategy status API & Dashboard

- `GET /api/ui/strategy_status` (роли `viewer`/`auditor`/`operator`) возвращает
  объединённый снимок риска, бюджета и PnL по каждой стратегии. В ответе есть
  `frozen`, `freeze_reason`, `budget_blocked`, `realized_pnl_today`,
  `max_drawdown_observed`, `consecutive_failures` и исходные лимиты.
- `/ui/dashboard` теперь использует этот же snapshot для блока «Strategy
  Performance / Risk» и подсвечивает стратегии, которые заморожены или
  уткнулись в бюджет. Это основной источник правды: таблица синхронизирована с
  runtime и ops_report.

### Execution risk accounting snapshot

- Добавлен read-only эндпоинт `GET /api/ui/risk_snapshot`. Он требует тот же
  bearer-токен, что и остальные `/api/ui` ручки, и возвращает структуру с
  флагами autopilot/HOLD/SAFE_MODE, агрегированным per-venue risk snapshot и
  вложенным блоком `accounting` (open notional, позиции, дневной PnL и budgets
  per strategy). Симуляционные (DRY_RUN / SAFE_MODE) подсчёты публикуются
  отдельно, чтобы их можно было мониторить без влияния на реальное исполнение.
- `/ui/dashboard` расширен карточкой **Risk snapshot (execution)**: в ней
  отображаются агрегированные показатели и таблица per-strategy с колонками
  «open notional», «open positions», «realized PnL today» и
  `budget used / limit`. Если дневной убыток или кап исчерпаны, строка
  подсвечивается флагом breach.

### Autopilot resume safety

- Автопилот больше не снимает HOLD автоматически, если стратегия заморожена
  или пер-стратегийный бюджет исчерпан. Решение (`last_decision`) и причина
  (`last_decision_reason`) записываются в runtime state, `/api/ui/ops_report`, и
  отображаются в «Autopilot mode» на `/ui/dashboard`.
- Баннер на дашборде информирует, что автопилот заблокирован риском, и подскажет
  причину, если последняя попытка была отклонена.

### Ops report coverage

- JSON `GET /api/ui/ops_report` и CSV-экспорт включают:
  - глобальное состояние (`mode`, `safe_mode`, `dry_run`, активный HOLD),
  - `strategy_status` с полным снапшотом риска/бюджета/PnL,
  - текущие позиции/экспозицию и частично закрытые хеджи,
  - журнал операторских действий и аудит событий,
  - `autopilot` с полями `last_decision`, `armed`, причиной последнего решения.
- Тест `tests/test_ops_report_endpoint.py` поднимает реальные менеджеры риска,
  бюджета и PnL на временном сторе, чтобы отчёт всегда отражал боевые данные.

### Exchange watchdog

`GET /api/ui/exchange_health` (bearer-токен тот же, что и для остальных `/api/ui/*`
ручек) возвращает агрегированное состояние подключённых бирж. Каждая запись в
ответе содержит флаги `reachable`/`rate_limited`, отметку `last_ok_ts`
(`float` с timestamp последнего успешного запроса) и текст `error`, если
клиент недавно упал. Роли `viewer` и `auditor` имеют read-only доступ,
`operator` видит тот же JSON.

Watchdog — единый источник правды о живости Binance и OKX. Он не выполняет
сетевых проверок сам по себе и не гасит торговлю автоматически: это каркас,
который заполняется данными от реальных клиентов. Решения о HOLD/RESUME и
ручном вмешательстве остаются за операторами.

# PropBot v0.1.2

Production-ready arbitrage runner with FastAPI, Binance Futures integration, SQLite
ledger, and the System Status web console. Release 0.1.2 ships the Binance live
broker, hardened risk limits with HOLD/SAFE_MODE automation, the Telegram control
bot, the SLO-driven System Status API + WebSocket feed, the production Docker
Compose profile with operator runbook, and the bearer-protected `propbotctl.py`
CLI (including safe `export-log`).

## Быстрый запуск продакшн-узла

1. Скопируйте `deploy/env.example.prod` в `.env` и заполните ключи бирж, профиль, лимиты, Telegram и `SAFE_MODE=true` для первого запуска.
2. Создайте каталог данных рядом с `deploy/docker-compose.prod.yml` и выдайте права контейнеру:
   ```bash
   sudo mkdir -p data
   sudo chown 1000:1000 data
   sudo chmod 770 data
   ```
3. Запустите сервис: `docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d`.
4. Проверьте состояние с временным токеном из `.env`: `curl -s -H "Authorization: Bearer $API_TOKEN" https://<host>/api/ui/status/overview | jq` (ожидается `overall=HOLD`).
5. Сгенерируйте bearer-токен и добавьте его в `.env`: `export API_TOKEN=$(openssl rand -hex 32)`.
6. Убедитесь, что CLI работает с токеном: `python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" status`.

## Getting started

Two supported bootstrap paths are outlined below. Both assume the repository has
been cloned to `~/propbot`.

### Option A — Local macOS virtualenv (no Docker)

Create an isolated Python environment, install dependencies, run tests, and
start the API in safe paper mode:

```bash
/usr/bin/python3 -m venv ~/propbot/.venv
source ~/propbot/.venv/bin/activate
~/propbot/.venv/bin/pip install -U pip wheel
~/propbot/.venv/bin/pip install -r ~/propbot/requirements.txt
~/propbot/.venv/bin/pytest -q
cp ~/propbot/.env.example ~/propbot/.env
SAFE_MODE=true PROFILE=paper AUTH_ENABLED=true API_TOKEN=devtoken123 \
  ~/propbot/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --reload
```

Interactive docs remain available at `http://127.0.0.1:8000/docs`.

### Option B — Docker Compose (new workstation friendly)

Pull the v0.1.2 image from GHCR (or build locally), then bring the stack up via
Compose. The compose file consumes the `TAG` environment variable for image
selection.

```bash
export REPO=my-org
docker pull ghcr.io/${REPO}/propbot:v0.1.2
TAG=v0.1.2 docker compose pull
TAG=v0.1.2 docker compose up -d
curl -f http://127.0.0.1:8000/healthz
```

Makefile helpers mirror the same workflow:

```bash
export REPO=my-org
TAG=v0.1.2 make up
make curl-health
make logs
make down
```

Set `BUILD_LOCAL=1 make up` to rebuild the image on the fly instead of pulling
from GHCR. Runtime artefacts (`runtime_state.json`, the SQLite ledger, incident
exports) are stored under `./data` and persist between restarts.

 codex/add-operator-runbook-documentation-30d5c6
### 🚀 Production deployment on Linux

1. Provision a clean Linux host with Docker Engine and the Compose plugin.
2. Clone the repository to `/opt/propbot` (or similar) and `cd /opt/propbot/deploy`.
3. Create the persistent data directory **before** starting the container and grant
   write access to the container user (UID 1000 in the default image):
   ```bash
   sudo mkdir -p /opt/propbot/data
   sudo chown 1000:1000 /opt/propbot/data
   sudo chmod 770 /opt/propbot/data
   ```
   The directory is mounted as `/app/data` and must remain writable so
   `runtime_state.json`, `ledger.db`, exports, and checkpoints survive restarts.
4. Copy `deploy/env.example.prod` to `.env`, then fill in API keys, `PROFILE`,
   `SAFE_MODE`, `DRY_RUN_ONLY`, `DRY_RUN_MODE`, Telegram settings, risk limits, and the bearer
   `API_TOKEN` (never commit secrets to git).
5. Keep the bot paused on first boot: `SAFE_MODE=true`, `DRY_RUN_ONLY=true` (for
   paper/testnet) or leave `SAFE_MODE=true` and plan to send `mode=HOLD` via
   Telegram/CLI in live environments. Use `DRY_RUN_MODE=true` to simulate the
   cross-exchange hedge even when connected to live venues.
6. Start the stack: `docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d`.
7. Validate the instance with Swagger (`https://<host>/docs`) and run `python3
   cli/propbotctl.py --base-url https://<host> status` to confirm the bot stays in
   HOLD.
8. After manual checks (balances, limits, `loop_pair`/`loop_venues`, approvals),
   resume trading via Telegram or `python3 cli/propbotctl.py --base-url
   https://<host> --token "$API_TOKEN" resume`.


 main
### Права на каталог `data`

Перед запуском production-контура через `docker-compose.prod.yml` создайте на
сервере каталог `./data` рядом с compose-файлом и убедитесь, что он доступен на
запись пользователю, от которого запускается Docker (например, `sudo mkdir -p
./data && sudo chown 1000:1000 ./data && sudo chmod 770 ./data`). Этот каталог
монтируется в контейнер как `/app/data` и содержит постоянные базы/состояние.
Права должны позволять контейнеру читать и записывать файлы, иначе сервис не
сможет стартовать.

## Environment configuration (`.env`)

Copy `.env.example` to `.env` and update the placeholders. Every variable in
`.env.example` is documented inline; the most important knobs are summarised
below:

- **Runtime profile & guards**
  - `PROFILE` — `paper`, `testnet`, or `live` broker profile.
  - `MODE` — descriptive deployment label used in metrics.
  - `SAFE_MODE` — when `true`, blocks live order placement (recommended
    default).
  - `DRY_RUN_ONLY` — forces the internal paper broker, regardless of profile.
  - `DRY_RUN_MODE` — simulates cross-exchange hedge execution without sending
    orders to external venues while keeping all risk guards active.
  - `TWO_MAN_RULE` — require two-man approval before resuming trading.
  - `POST_ONLY`, `REDUCE_ONLY`, `ORDER_NOTIONAL_USDT`, `MAX_SLIPPAGE_BPS`,
    `MIN_SPREAD_BPS`, `POLL_INTERVAL_SEC`, `TAKER_FEE_BPS_*` — runtime loop
    controls.
  - `LOOP_PAIR` / `LOOP_VENUES` — optional overrides for the live loop symbol
    and venue list (uppercase symbol, comma-separated venues). When unset the
    loop follows strategy defaults.
- `ENABLE_PLACE_TEST_ORDERS` — allow real order placement on testnet.

## Secrets store & RBAC

- Биржевые ключи и operator-токены читаются из JSON-хранилища, которое обрабатывает
  `SecretsStore`. Путь задаётся через `SECRETS_STORE_PATH` (см. `.env.example`). В
  production этот JSON монтируется в контейнер (например, через `docker secrets`
  или файловый volume) и **никогда** не коммитится в репозиторий.
- Формат файла:

  ```json
  {
    "binance_key": "...",
    "binance_secret": "...",
    "okx_key": "...",
    "okx_secret": "...",
    "operator_tokens": {
      "alice": { "token": "AAA", "role": "operator" }
    }
  }
  ```

  Секреты могут быть зашифрованы placeholder-кодеком `SECRETS_ENC_KEY`. При
  запуске клиент сначала читает значения из `SecretsStore`; если ключи отсутствуют,
  используется резервный путь с переменными окружения (`BINANCE_API_KEY`,
  `BINANCE_API_SECRET`, `OKX_API_KEY`, `OKX_API_SECRET`, `OKX_API_PASSPHRASE`), что
  упрощает локальную разработку. Для OKX добавьте поле `"okx_passphrase"` в JSON,
  если нужно хранить passphrase рядом с ключами.
- Вводятся роли операторов:
  - `viewer` — базовый read-only доступ для наблюдения за режимом и здоровьем
    сервисов. Управляющие формы в дэшборде не рендерятся.
  - `auditor` — ревизор: видит `/ui/dashboard`, `ops_report`,
    `audit_snapshot`, бюджеты, PnL, risk/freeze и audit trail, но не может
    инициировать HOLD, RESUME, UNFREEZE, KILL и не обязан иметь доступ к
    чувствительным ключам.
  - `operator` — полный доступ к управлению ботом, включая все защищённые
    POST-ручки и двухоператорный флоу.
  Проверки прав выполняет `app/rbac.py`.
- Все критические действия (`RESUME`, снятие HOLD, kill-switch / cancel-all,
  `UNFREEZE_STRATEGY`) проходят двухшаговый approval: оператор A создаёт
  запрос (`/api/ui/*-request`), оператор B подтверждает через
  `/api/ui/*-confirm` с `APPROVE_TOKEN`. Dashboard использует только эти
  безопасные wrapper-ручки и отображает статус "ожидает второго
  подтверждения".
- Любые привилегированные действия пишутся в аудит через
  `app/audit_log.log_operator_action`, чтобы фиксировать, кто и откуда инициировал
  операцию. Логи сохраняются в `data/audit.log`, а `/api/ui/audit_snapshot`
  возвращает последние записи с пометкой `status` (`requested` / `approved` /
  `denied` / `forbidden`) для всех ролей.

## Secrets & Rotation Policy

- JSON-хранилище секретов лежит в пути из `SECRETS_STORE_PATH`. Файл должен иметь
  строгие права на чтение/запись (например, `chmod 600`) и никогда не
  коммитится в репозиторий.
- При наличии `SECRETS_ENC_KEY` секреты хранятся в виде base64/XOR-заготовки.
  Ключ задаётся строкой, и используется как простой placeholder для шифрования
  «в покое». Если переменная не установлена, значения читаются в открытом виде.
- Для оценки возраста ключей в JSON добавлены поля `meta.*_last_rotated`.
  Эндпоинт `/api/ui/secrets/status` (роль `operator`) возвращает, требуется ли
  ротация с учётом заданного порога и список операторов (имя и роль) без самих
  токенов.
- Перед ротацией обновите файл, пересчитайте зашифрованные значения через тот же
  XOR/base64 stub и сохраните ISO8601-метку последней ротации.

## Risk governor / auto-HOLD

The runtime now includes a dedicated risk governor that continuously samples the
portfolio snapshot and trading runtime before every loop cycle and prior to
submitting live orders. The governor will automatically engage HOLD/SAFE_MODE,
persist the reason, and surface it in `/api/ui/status/overview` when any of the
following conditions trip:

- Daily realised PnL breaches `MAX_DAILY_LOSS_USD`.
- Aggregate open exposure exceeds `MAX_TOTAL_NOTIONAL_USD` (or the legacy
  `MAX_TOTAL_NOTIONAL_USDT`).
- Unrealised losses are deeper than `MAX_UNREALIZED_LOSS_USD`.
- Reported exchange server time drifts past `CLOCK_SKEW_HOLD_THRESHOLD_MS`.
- A connected derivatives venue reports `maintenance`/`read-only` mode.

The latest exposure snapshot (per venue and symbol), realised/unrealised PnL,
clock-skew sample, and any maintenance flags are stored in the runtime state and
returned as `safety.risk_snapshot` so operators can understand why HOLD was
activated. All limits read from the environment are optional—set a value of `0`
to disable a particular guard. Even in `DRY_RUN_MODE` the governor continues to
monitor clock skew and maintenance signals, but simulated fills do not contribute
to real risk limits. Never resume trading until the root cause is investigated
and addressed, then follow the existing two-step `resume-request`/`resume-confirm`
flow to clear HOLD.

## Pre-trade risk gate

Routers and orchestrator flows now run a lightweight `risk_gate(order_intent)`
helper before dispatching manual hedges or orchestrated plans. The helper
delegates to `RiskGovernor.validate(...)` so both pre-trade checks and risk
accounting share the same enforcement path. The call first evaluates
`FeatureFlags.risk_checks_enabled()` (backed by the `RISK_CHECKS_ENABLED`
environment flag, disabled by default). When the flag is off the gate returns
`{"allowed": true, "reason": "risk_checks_disabled"}` and has no side effects.
With the flag enabled the gate reads the current exposure snapshot and verifies
that adding the requested intent (`intent_notional`, optional position
increments) would stay inside the configured caps when
`FeatureFlags.enforce_caps()` is true. Manual routes **skip without raising**
when a cap would be breached, returning an HTTP 200 body such as
`{"status": "skipped", "state": "SKIPPED_BY_RISK", "reason": "caps_exceeded", "cap": "max_total_notional_usdt"}`
so operators can see why the order was ignored. Dry-run executions (either via
the runtime control toggle or the `DRY_RUN_MODE` flag) short-circuit with
`why="dry_run_no_enforce"`, keeping simulated counters in the snapshot without
blocking execution. Per-strategy drawdown budgets (when configured) are guarded
only when `FeatureFlags.enforce_budgets()` returns true, letting operators
observe loss telemetry without immediately halting trading.

### Risk skip reason codes

Risk-driven skips now emit consistent reason codes that surface in the
dashboard ("Risk skips (last run)") and via the `/metrics` endpoint as the
`risk_skips_total{reason,strategy}` counter.

| Code              | Description                                    | Where to monitor                  |
| ----------------- | ---------------------------------------------- | --------------------------------- |
| `caps_exceeded`   | Global RiskGovernor caps (notional/positions)  | UI risk skip block, `/metrics`    |
| `budget_exceeded` | `StrategyBudgetManager` per-strategy budgets   | UI risk skip block, `/metrics`    |
| `strategy_frozen` | `StrategyRiskManager` freeze due to breaches   | UI risk skip block, `/metrics`    |
| `other_risk`      | Any other risk gating condition or fallback    | UI risk skip block, `/metrics`    |

- **Risk limits**
  - `MAX_POSITION_USDT` and `MAX_POSITION_USDT__<SYMBOL>` — per-symbol notional
    caps.
  - `MAX_OPEN_ORDERS` and `MAX_OPEN_ORDERS__<venue>` — outstanding order caps.
  - `MAX_DAILY_LOSS_USDT` — absolute drawdown stop in USDT.
- **Auth & rate limiting**
  - `AUTH_ENABLED` + `API_TOKEN` — enable bearer auth for mutating routes.
  - `IDEM_TTL_SEC`, `API_RATE_PER_MIN`, `API_BURST` — idempotency and rate
    limit configuration.
- **Telegram control bot**
  - `TELEGRAM_ENABLE=true` to start the bot alongside FastAPI.
  - `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` — credentials issued by
    BotFather.
  - `TELEGRAM_PUSH_MINUTES` — periodic status push interval (minutes).
  - When enabled the same flag also activates the lightweight operations
    notifier that mirrors HOLD/RESUME, kill-switch, and auto-hedge alerts to
    Telegram.
- **Persistence**
  - `RUNTIME_STATE_PATH` — JSON snapshot of loop/control state.
  - `POSITIONS_STORE_PATH` — durable cross-exchange hedge position ledger
    (default `data/hedge_positions.json`).
- **Binance / OKX keys**
  - `BINANCE_UM_API_KEY_TESTNET` / `BINANCE_UM_API_SECRET_TESTNET` — Binance
    UM testnet credentials (`BINANCE_UM_BASE_TESTNET` override optional).
  - `BINANCE_LV_API_KEY` / `BINANCE_LV_API_SECRET` — Binance Futures live keys
    for the legacy router (kept for completeness).
  - `BINANCE_API_KEY` / `BINANCE_API_SECRET` — fallback variables for the new
    Binance USDⓈ-M hedge client. В production ключи читаются из `SecretsStore`.
  - `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE` — fallback для OKX
    perpetual hedge клиента. В production ключи читаются из `SecretsStore`
    (используйте ограниченный sub-account и IP whitelist).

## Deployment / prod

Перед запуском производственного инстанса подготовьте окружение так, чтобы
бот стартовал безопасно (SAFE_MODE/HOLD и `DRY_RUN_MODE=true`).

1. **Каталог с данными.** Создайте директорию `data/` рядом с репозиторием и
   настройте права пользователя контейнера (UID 1000):
   ```bash
   sudo mkdir -p /opt/propbot/data
   sudo chown 1000:1000 /opt/propbot/data
   sudo chmod 770 /opt/propbot/data
   ```
   Этот путь примонтируется в контейнер как `/app/data` (см.
   `docker-compose.prod.yml`). Здесь лежат `runtime_state.json`,
   `hedge_log.json`, `hedge_positions.json`, `ops_alerts.json`, `alerts.json`,
   файлы авто-хеджа и другие журналы — держите каталог на постоянном диске и
   включите регулярный бэкап. Потеря содержимого = потеря истории и контекста
   состояний.
2. **Файл окружения.** Скопируйте шаблон и заполните секреты:
   ```bash
   cp .env.prod.example .env.prod
   ```
   Обновите значения `API_TOKEN`, `APPROVE_TOKEN`, биржевые ключи
   (`BINANCE_*`, `OKX_*`), лимиты риска (`MAX_POSITION_USDT`,
   `MAX_DAILY_LOSS_USDT`, `MAX_ORDERS_PER_MIN`, `MAX_CANCELS_PER_MIN`), настройки
   Telegram (`TELEGRAM_ENABLE`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`). По
   умолчанию шаблон уже включает `SAFE_MODE=true`, `DRY_RUN_ONLY=true`,
   `DRY_RUN_MODE=true` и комментарии с подсказками — оставьте их включёнными до
   тех пор, пока оба оператора не пройдут двухшаговый `resume-request` →
   `resume-confirm` и не убедятся, что лимиты соблюдены.
3. **Старт сервисов.** Запустите контейнеры в фоне:
   ```bash
   docker compose -f docker-compose.prod.yml --env-file .env.prod up -d
   ```
   Проверить статус healthcheck можно командой
   ```bash
   docker inspect --format '{{json .State.Health}}' propbot_app_prod | jq
   ```
   Контейнер считается здоровым, когда `/healthz` отвечает `{ "ok": true }`.
4. **Проверка безопасности.** Убедитесь, что бот поднялся в HOLD/SAFE_MODE и с
   `DRY_RUN_MODE=true`:
   ```bash
   curl -sfS -H "Authorization: Bearer $API_TOKEN" \
     http://localhost:8000/api/ui/status/overview | jq '.flags'
   ```
   В ответе `safe_mode`, `hold_active` и `dry_run_mode` должны быть `true`.
   При необходимости дополнительно проверьте `GET /api/ui/state` и
   `GET /api/ui/control-state` — они отражают активные guard'ы и режим HOLD.
5. **Двухшаговый запуск торгов.** Переход к реальным сделкам разрешён только
   после ручного флоу:
   1. Первый оператор отправляет `POST /api/ui/resume-request` с причиной.
   2. Второй оператор подтверждает `POST /api/ui/resume-confirm` с
      `APPROVE_TOKEN`.
   3. Только после этого (и отключения SAFE_MODE/DRY_RUN вручную) выполняется
      `POST /api/ui/resume`.

> ⚠️ JSON-файлы в `data/` (runtime_state_store, hedge_log, alerts, позиции и т.д.)
> редактируйте вручную только в аварийных случаях. Эти файлы — единственный
> источник истины об истории состояний; потеря или порча приведёт к утрате
> журнала и нарушению расследований.

## Going live

После запуска `docker-compose.prod.yml` выполните быстрый чек-лист перед
реальным исполнением:

1. Убедитесь, что процесс и демоны живы:
   ```bash
   curl -sf http://localhost:8000/healthz | jq
   ```
   Ответ должен быть `{ "ok": true }`.
2. Изучите `/api/ui/status/overview` и проверьте флаги безопасности:
   ```bash
   curl -sfS -H "Authorization: Bearer $API_TOKEN" \
     http://localhost:8000/api/ui/status/overview | jq '.flags'
   ```
3. Сверьте открытые ноги и экспозицию:
   ```bash
   curl -sfS -H "Authorization: Bearer $API_TOKEN" \
     http://localhost:8000/api/ui/positions | jq '.positions'
   ```
4. Убедитесь, что бот остаётся в HOLD (`flags.hold_active=true`) и
   `dry_run_mode=true`. Первую загрузку проводите только с
   `DRY_RUN_MODE=true`.
5. Чтобы перейти к реальным сделкам, выполните двухшаговый процесс
   `resume-request` → `resume-confirm` (с `APPROVE_TOKEN`) → `resume`. Без
   подтверждения второго оператора HOLD не снимается.
6. Никогда не отключайте HOLD и `DRY_RUN_MODE` одновременно: сначала снимите
   HOLD через подтверждённый `resume-confirm`, затем, после финальных проверок,
   переключайте `DRY_RUN_MODE` и SAFE_MODE.

Для аудита используйте `data/runtime_state.json`: в нём фиксируются
`safety.hold_reason`, `safety.hold_since`, `safety.last_released_ts` и
`auto_hedge.last_success_ts` — это источник истины при расследованиях.

## Forensics snapshot / audit export

- Чтобы выгрузить полный срез, запросите защищённый эндпоинт:
  ```bash
  curl -H "Authorization: Bearer $API_TOKEN" \
    https://<host>/api/ui/snapshot | jq
  ```
  Он одновременно пишет файл `data/snapshots/<timestamp>.json` и возвращает его
  содержимое.
- В снэпшоте лежат: текущее состояние runtime (режим, HOLD, SAFE_MODE,
  dry-run флаги, лимиты), живые и `partial` позиции из `positions_store`,
  очередь two-man approvals, последние метрики исполнения (slippage), активные
  reconciliation alerts и свежий daily report.
- Для лёгкого JSON-снимка без побочных файлов используйте `GET
  /api/ui/audit_snapshot` (также требует bearer-токен). Ответ содержит текущий
  режим (`HOLD`, SAFE_MODE, DRY_RUN), экспозицию/хеджи, состояние
  `StrategyRiskManager` (включая `active`/`blocked_by_risk`/`frozen_by_risk`),
  UniverseManager-данные по разрешённым символам и `build_version`. Секреты из
  `secrets_store` не попадают в снимок — только операционные состояния.
- Новый read-only отчёт `GET /api/ui/ops_report` агрегирует режим runtime,
  SAFE_MODE/DRY_RUN/автопилот, статус двухфакторного RESUME, экспозиции,
  снапшот `StrategyRiskManager` (freeze/enable per strategy) и последние
  операторские действия/alerts. Эндпоинт доступен токенам `viewer` и
  `auditor` — для комплаенса, пост-моратория и ревизии без эскалации
  привилегий.
- Для экспорта в Excel/архив используйте `GET /api/ui/ops_report.csv` — тот же
  отчёт в стабильном CSV (`content-type: text/csv`) с секциями runtime, стратегий
  и аудита. Поддерживает те же bearer-токены, что и JSON.
- Используйте экспорт для отчётов инвесторам, расследования инцидентов и
  юридической фиксации «что бот знал и делал» без SSH-доступа к контейнеру.

## Ежедневный мониторинг

Операторы отслеживают жизнеспособность инстанса следующими инструментами:

- `GET /healthz` — базовая проверка живости контейнера.
- `GET /api/ui/status/overview` — общий статус, включающий SAFE_MODE, HOLD,
  runaway guard, auto-hedge (`consecutive_failures`).
- `GET /api/ui/status/components` и `GET /api/ui/status/slo` — детализация
  алертов и компонентов (требуется `API_TOKEN`).
- `GET /api/ui/positions` — экспозиция и PnL по открытым ногам.
- `GET /api/ui/alerts` — история событий (защищён bearer-токеном).
- Telegram-бот присылает HOLD/RESUME, runaway guard, kill switch, попытки
  авто-хеджа, ручные подтверждения RESUME.

Фиксируйте любые неожиданные `consecutive_failures` авто-хеджа, рост runaway
счётчиков и ручные HOLD — это ранние сигналы проблем.

## Crash / Restart recovery

При падении сервера и перезапуске контейнера бот автоматически стартует в
SAFE_MODE/HOLD, даже если до сбоя шла торговля.

1. После рестарта прочитайте `runtime_state.json` (через API) и убедитесь, что
   `hold_active=true`, `safe_mode=true`.
2. Проверьте `/api/ui/status/overview`, `/api/ui/positions`, `/api/ui/alerts` —
   восстановленные лимиты и статусы должны совпадать с тем, что было до сбоя.
3. Убедитесь, что runaway guard и risk-лимиты не превышены, а экспозиция
   соответствует ожиданиям.
4. Выполните существующий двухшаговый RESUME-флоу: `POST /api/ui/hold` (если
   нужно зафиксировать причину), затем `POST /api/ui/resume-request` и `POST
   /api/ui/resume-confirm` с `APPROVE_TOKEN`. Только после этого можно отправить
   `POST /api/ui/resume` и снять HOLD.
5. JSON-файлы в `data/` редактируйте вручную только при крайней необходимости —
   они служат аудиторским следом и должны бэкапиться.

## Safety / Controls

- **HOLD** — принудительная остановка торгового цикла; включает SAFE_MODE.
- **SAFE_MODE** — запрет на выставление ордеров, мониторинг остаётся активным.
- **Kill switch** — аварийное отключение, приводящее к HOLD и SAFE_MODE до ручной
  проверки.
- **Runaway guard** — лимиты на заявки/отмены в минуту, активирует HOLD при
  превышении.
- **Two-man rule** — обязательное двойное подтверждение через `resume-request`
  и `resume-confirm` с `APPROVE_TOKEN`. Без него торговля не возобновится.

После каждого рестарта повторяйте ручной двухшаговый RESUME. Автоторговля не
возобновляется сама, даже если `runtime_state.json` содержит `safe_mode=false`.
    (`BINANCE_LV_BASE_URL` optional).
  - `BINANCE_LV_API_KEY_TESTNET` / `BINANCE_LV_API_SECRET_TESTNET` — optional
    segregated credentials when running live and testnet bots in parallel.
  - `OKX_API_KEY_TESTNET`, `OKX_API_SECRET_TESTNET`,
    `OKX_API_PASSPHRASE_TESTNET` — optional OKX testnet integration.

For live trading, populate the `BINANCE_LV_*` variables only in locked-down
profiles and keep `.env` outside version control.

### Operations alerts & audit trail

- Every operator-facing action (HOLD/RESUME flow, kill switch, cancel-all,
  hedge outcomes, runaway guard trips) now appends a structured record to
  `data/ops_alerts.json`. This file contains sensitive operational context and
  should stay on secured hosts.
- With `TELEGRAM_ENABLE=true` plus valid `TELEGRAM_BOT_TOKEN` and
  `TELEGRAM_CHAT_ID`, the notifier also pushes the same text to the Telegram
  control chat via the official Bot API. Network errors are swallowed so CI and
  offline environments are unaffected.
- Operators can review recent activity through the token-protected
  `GET /api/ui/alerts` endpoint. Supply the same bearer token used for the rest
  of the UI API (`Authorization: Bearer <API_TOKEN>`). This feed is intended for
  internal desks only; do not expose it publicly.

### Edge Guard (adaptive entry filter)

- Before opening a new cross-exchange hedge the bot now consults an adaptive
  `edge_guard` module that evaluates live risk: HOLD/auto-throttle status,
  outstanding partial hedges, recent execution quality (average slippage and
  failure rate), and unrealised PnL trends versus current exposure.
- If the environment looks toxic (e.g. HOLD engaged, partial hedges still
  hanging, average slippage over the last attempts above the configured bps
  ceiling, or unrealised PnL falling five snapshots in a row while exposure is
  heavy) the guard refuses to place fresh legs. The rejection reason is logged
  to the ops/audit timeline so the desk has an audit trail.
- The operator dashboard exposes the live "Edge guard status" row under the
  runtime/risk section, showing whether new hedges are allowed and, if blocked,
  the exact reason to accelerate triage.

### Operator Dashboard (`/ui/dashboard`)

- Token-protected HTML dashboard for on-call operators. Access requires the
  same bearer token as other `api/ui` endpoints and works only when
  `AUTH_ENABLED=true`.
- Aggregates runtime state from `runtime_state.json`, the in-memory safety
  controller, hedge positions store, and the persistent two-man approvals queue.
- Shows the authenticated operator name and role badge (viewer vs
  auditor vs operator) so the desk immediately sees whether HOLD/RESUME
  actions are available.
- Shows build version, current HOLD status with reason/since timestamp,
  SAFE_MODE and DRY_RUN flags, runaway guard counters/limits, and the latest
  auto-hedge status (`enabled`, last success timestamp, consecutive failures,
  and last execution result).
- Displays live hedge exposure per venue, unrealised PnL totals, and detailed
  open/partial positions (venue, side, entry/mark prices, status). Simulated
  DRY_RUN hedges remain visible but are clearly marked as `SIMULATED`, while
  partial hedges and unbalanced exposure are flagged as `OUTSTANDING RISK`.
- Adds inline risk hints: runaway guard counters within 20% of their caps are
  labelled `NEAR LIMIT`, and background health rows for stalled auto-hedge or
  scanner tasks show red status/detail text for quick triage.
- Lists configured risk limits (e.g. `MAX_OPEN_POSITIONS`,
  `MAX_TOTAL_NOTIONAL_USDT`, per-venue order caps) together with the runtime
  snapshot of limits maintained by the risk engine.
- Highlights background daemon health (auto-hedge loop, opportunity scanner)
  using the same checks as `/healthz`, marking dead/inactive tasks in red.
- Renders pending approvals from the two-man workflow so the desk can see who
  requested HOLD release, limit changes, resume, or other guarded actions.
- Includes simple `<form>` controls that post to dedicated `/api/ui/dashboard-*`
  helper routes. These wrappers accept form-encoded submissions from the HTML
  dashboard, translate them into the JSON payloads expected by the guarded API,
  and call the existing `/api/ui/hold`, `/api/ui/resume-request`, and
  `/api/ui/unfreeze-strategy` logic. HOLD/RESUME/UNFREEZE actions therefore stay
  behind the same RBAC/two-man protections while remaining usable from the
  browser. Auditor accounts see a dedicated "auditor role: read only" banner
  and the control block is hidden entirely so they cannot submit HOLD/RESUME or
  KILL forms. The kill switch form now posts to `/api/ui/dashboard-kill` and
  records a request that still requires second-operator approval via
  `/api/ui/kill` with `APPROVE_TOKEN`.
- The Strategy Risk table now highlights each strategy’s risk state:
  `active`, `blocked_by_risk`, or `frozen_by_risk` (red badges for frozen or
  blocked strategies, green for active). Consecutive failure counters are shown
  alongside configured limits, with non-zero counts rendered in red so the desk
  can watch thaw progress after an unfreeze.
- Surfaces a read-only **PnL / Risk** card with unrealised PnL, the current
  day's realised PnL stub (currently fixed at `0.0` until settlement reporting
  is wired in), total live exposure, and CapitalManager headroom per strategy.
  Use it as the landing spot for a quick risk scan instead of grepping logs.

### PnL / Exposure trend

- Rolling exposure and PnL snapshots are persisted to the file configured by
  `PNL_HISTORY_PATH` (default: `data/pnl_history.json`). The path lives next to
  other operator-facing JSON stores and can be relocated via environment
  variable if the default does not suit your deployment layout.
- Operators can fetch the latest snapshots via the token-protected
  `GET /api/ui/pnl_history?limit=N` endpoint. The response contains
  `{ "snapshots": [...] }` with the newest entry first so the desk can export a
  quick history without shell access to the host.
- Each snapshot records live (non-simulated) open/partial positions only. Legs
  executed in `DRY_RUN_MODE` are labelled under a separate `simulated` section
  and excluded from the real exposure totals shown on the dashboard.
- The dashboard renders a compact "Risk & PnL trend" block comparing the two
  most recent snapshots, highlighting changes in unrealised PnL and aggregate
  exposure together with counts of open, partial, and simulated hedges.

### Hedge positions persistence & monitoring

- All cross-exchange hedge positions (including both legs, entry prices,
  leverage, timestamps, and status) are durably mirrored to the JSON file at
  `data/hedge_positions.json`. Override the location with
  `POSITIONS_STORE_PATH` if the default path does not suit your deployment
  layout.
- The token-protected `GET /api/ui/positions` endpoint exposes the same data to
  operators. The response includes each position with its long/short legs,
  calculated unrealised PnL per leg, the pair-level `unrealized_pnl_usdt`, and a
  venue exposure summary (`long_notional`, `short_notional`, `net_usdt`). When
  mark prices are unavailable (for example, in offline tests) the endpoint falls
  back to entry prices so unrealised PnL is reported as `0` rather than raising
  an error.

## Safety reminder for Binance live

`PROFILE=live` with `SAFE_MODE=false` **and** `DRY_RUN_ONLY=false` plus valid
`BINANCE_LV_*` keys will route orders to real Binance Futures accounts. Keep the
bot in HOLD and `SAFE_MODE=true` on startup, double-check risk limits,
`loop_pair`/`loop_venues`, balances, Telegram access, and two-man approvals
before resuming trading in live mode. Never store real credentials in
repositories or unattended hosts.

For routine operational procedures (health checks, HOLD management, secret
rotation, exports, safe restarts) see `docs/OPERATOR_RUNBOOK.md`. Оператор может
пользоваться Telegram-ботом или локальным `propbotctl` (CLI требует локального
или SSH-доступа к хосту и bearer-токен).

## HOLD / Two-Man resume flow

PropBot keeps the cross-exchange hedge engine untouched, but live trading now
ships with a hardened HOLD workflow. A global runtime safety block (`hold_active`)
stops every dangerous action (hedge execute, confirmations, cancel-all, loop
execution) until two operators explicitly approve the resume:

1. **Pause the system** — call `POST /api/ui/hold` with a reason. This sets
   `hold_active=true`, forces SAFE_MODE, and freezes the loop.
2. **Log the investigation** — when the desk is ready to resume, call
   `POST /api/ui/resume-request` with a human-readable reason (and optional
   `requested_by`). This records the request and timestamps it but *does not*
   clear the hold.
3. **Second-operator confirmation** — a different operator supplies the shared
   approval secret via `POST /api/ui/resume-confirm` with
   `{"token": "<APPROVE_TOKEN>", "actor": "name"}`. Only when the token matches
   does the runtime clear `hold_active`.
4. **Return to RUN** — once the hold is cleared and SAFE_MODE is disabled,
   trigger `POST /api/ui/resume` (or the corresponding CLI/Telegram command) to
   set `mode=RUN`.

Set the `APPROVE_TOKEN` variable in production `.env` files. If it is empty or
the token is wrong, `/api/ui/resume-confirm` returns `401` and the bot stays in
HOLD. The `/api/ui/status/overview` payload now exposes `hold_active`, the last
resume request, and the current reason so operators can coordinate responses.

This two-step confirmation is **required** before any real-money deployment.

## Autopilot mode

PropBot still defaults to manual resumes protected by the two-man rule. The
`AUTOPILOT_ENABLE` environment flag controls whether the runtime may clear HOLD
on its own after a restart.

* `AUTOPILOT_ENABLE=false` (default) — the service always boots into HOLD with
  SAFE_MODE engaged. Operators must file `/api/ui/resume-request`, obtain the
  second approval via `/api/ui/resume-confirm`, and manually call
  `/api/ui/resume` (or the CLI/Telegram equivalents) before trading resumes.
* `AUTOPILOT_ENABLE=true` — after a restart the bot inspects the existing safety
  guards (runaway breaker counters, auto-hedge health, exchange connectivity,
  preflight status, risk breaches). When everything is green it restores the
  prior SAFE_MODE setting, clears HOLD, and calls `resume_loop()` automatically.
  The action is written to the persistent audit log with initiator `autopilot`,
  broadcast to the ops Telegram channel as
  `AUTOPILOT: resumed trading after restart (reason=…)`, and highlighted on the
  `/ui/dashboard` banner as “autopilot armed”.
* If autopilot is enabled but any blocker is present (runaway limits exceeded,
  auto-hedge errors, venues unreachable, config invalid, etc.) the bot stays in
  HOLD, logs `autopilot_resume_refused`, and emits
  `AUTOPILOT refused to arm (reason=…)` so the desk can investigate.
* Only enable the flag on trusted hosts. Autopilot bypasses the manual resume
  gate on restarts, but it still honours all existing guardrails and manual
  HOLDs.

The status API and `/ui/dashboard` expose `autopilot_status`,
`last_autopilot_action`, and `last_autopilot_reason` so operators can verify how
the runtime left HOLD.

## Runaway order breakers & status surface

To reduce catastrophic runaway behaviour, the runtime tracks how many orders and
cancels were attempted in the last rolling minute. Configure the new limits via
`.env`:

- `MAX_ORDERS_PER_MIN` (default `300`)
- `MAX_CANCELS_PER_MIN` (default `600`)

Every order path calls into the counters. If a limit is exceeded the runtime
automatically flips `hold_active=True`, blocks the offending request with HTTP
`423`, and records the reason. The status overview includes the live counters,
limits, and the most recent clock-skew measurement so the desk can see why the
bot is paused. These breakers sit on top of the existing hedge math—they do not
change spreads, pricing, or execution strategy, only whether orders are allowed
to leave the process.

## Cross-exchange futures hedge

PropBot now ships with a lightweight cross-exchange futures hedge “engine”. It
compares USDⓈ-margined perpetual prices between Binance Futures and OKX, checks
the spread against an operator-provided threshold, and when authorised executes
paired long/short legs to lock in the basis. Both legs share the same notional
exposure and leverage so the book stays delta-neutral.

> ⚠️ **Derivatives warning:** Perpetual futures use leverage. Review exchange
> margin rules, ensure SAFE_MODE is enabled until dry-run tests succeed, and
> keep firm-wide risk limits enforced before allowing live execution.

### Previewing the spread

Use the existing `/api/arb/preview` endpoint with the new payload to inspect the
current cross-exchange spread and suggested direction:

```bash
curl -s -X POST http://127.0.0.1:8000/api/arb/preview \
  -H 'Content-Type: application/json' \
  -d '{"symbol": "BTCUSDT", "min_spread": 2.0}' | jq
```

The response echoes the symbol, spread, and whether it clears `min_spread`, plus
which venue should host the long and the short legs.

### Executing the hedge

After validating risk limits, post to `/api/arb/execute` with the notional size,
leverage, and minimum acceptable spread. The updated risk manager enforces the
per-position cap (`MAX_NOTIONAL_PER_POSITION_USDT`), concurrent position limit
(`MAX_OPEN_POSITIONS`), aggregate open notional ceiling
(`MAX_TOTAL_NOTIONAL_USDT`), and leverage guard (`MAX_LEVERAGE`):

```bash
curl -s -X POST http://127.0.0.1:8000/api/arb/execute \
  -H 'Content-Type: application/json' \
  -d '{"symbol": "BTCUSDT", "min_spread": 2.5, "notion_usdt": 1500, "leverage": 3}' | jq
```

The response returns both legs with their execution status, average fill price,
and leverage. Successful live trades are appended to
`data/hedge_positions.json` with leg status `open`; simulated runs are tagged as
`simulated` so they can be filtered in the operator UI.

> ⚠️ **Operational discipline:** Always validate the flow in
> `DRY_RUN_MODE=true` first. Only after simulated cycles succeed, the operator
> should inspect `/api/ui/status/overview`, verify that HOLD is engaged and all
> guards are green, and then follow the two-man `resume-request`/`resume-confirm`
> process to clear HOLD before disabling `DRY_RUN_MODE` for live execution.

### Dry-run mode for hedging

Set `DRY_RUN_MODE=true` in the environment to run the entire cross-exchange
pipeline in a “safe” simulation. Manual `/api/arb/execute` calls and the auto
hedge daemon still evaluate opportunities, enforce risk limits, respect HOLD and
two-man approvals, and register activity with the runaway guard, but **no orders
are sent to external venues**. Instead, simulated fills are recorded in
`data/hedge_log.json` and the hedge positions store with `status="simulated"`.
Alerts emitted to Telegram/ops channels explicitly mention DRY_RUN_MODE so
operators see that a training run occurred. The System Status overview and UI
runtime payloads expose a `dry_run_mode` flag, making it obvious when the bot is
in simulation.

### Auto mode

The cross-exchange loop now includes a guarded auto-execution daemon. To enable
it set `AUTO_HEDGE_ENABLED=true` (and optionally tune
`AUTO_HEDGE_SCAN_SECS`/`MAX_AUTO_FAILS_PER_MIN`) before starting the API
service. When active the daemon:

* reuses the existing opportunity scanner every `AUTO_HEDGE_SCAN_SECS` seconds;
* skips execution whenever `hold_active` is set, SAFE_MODE is on, two-man resume
  is pending, runaway counters hit, or any risk breach is present;
* invokes the same `/api/arb/execute` flow as the manual REST path so all
  guardrails (limits, runaway breaker, approvals) remain intact;
* records each automatic fill or rejection in `data/hedge_log.json` with the
  initiator set to `YOUR_NAME_OR_TOKEN`.

Review the log via the new read-only endpoint:

```bash
curl -s -H 'Authorization: Bearer <API_TOKEN>' \
  "http://127.0.0.1:8000/api/ui/hedge/log?limit=50" | jq
```

The system status payload (`/api/ui/status/overview`) now exposes an
`auto_hedge` block showing whether auto mode is enabled, when the last
opportunity was checked, the most recent result, and the number of consecutive
failures. If more than `MAX_AUTO_FAILS_PER_MIN` errors occur inside a rolling
minute the daemon engages HOLD automatically and records the reason. It will
never clear HOLD on its own—the two-man resume flow still applies, and all risk
limits continue to take precedence over profitability.

Successful responses include the executed leg details (long venue, short venue)
and the persisted position snapshot. If the spread collapses below the
threshold or limits are exceeded, the endpoint returns a `400` with the
rejection reason.

### Semi-automatic workflow

The background opportunity scanner (interval controlled by `SCAN_INTERVAL_SEC`)
continuously evaluates Binance vs. OKX spreads and records the best candidate in
`runtime_state.json`. Operators can monitor the latest candidate via:

```bash
curl -s http://127.0.0.1:8000/api/arb/opportunity | jq
```

The payload includes the suggested venues, spread (in bps), recommended notional
(`notional_suggestion`), leverage hint, and a `status` flag:

- `allowed` — the opportunity clears all risk checks and can be executed.
- `blocked_by_risk` — limits prevent execution; inspect the `blocked_reason`.

When the operator is satisfied with the spread and has disabled `SAFE_MODE` and
set the loop out of HOLD, confirm the candidate explicitly:

```bash
curl -s -X POST http://127.0.0.1:8000/api/arb/confirm \
  -H "Content-Type: application/json" \
  -d "{\"opportunity_id\": \"<id from /api/arb/opportunity>\", \"token\": \"$API_TOKEN\"}" | jq
```

`POST /api/arb/confirm` re-validates risk, recalculates the spread, and only
executes when the stored opportunity is still viable. The `token` must match the
operator `API_TOKEN`; without it the trade is rejected. Every confirmed trade is
persisted in `runtime_state.json` and surfaced via `GET /api/ui/positions` so the
desk always has an auditable ledger of open/closed hedges.

## CLI `propbotctl`

The repository ships a thin operator CLI for frequently used status checks and
controls. Run it with the local interpreter (requires the `requests`
dependency):

```bash
codex/add-operator-runbook-documentation-30d5c6
python3 cli/propbotctl.py --base-url https://<host> status
python3 cli/propbotctl.py --base-url https://<host> components

python3 cli/propbotctl.py status
python3 cli/propbotctl.py components
 main
```

Mutating commands and the log export helper require a bearer token that has
access to `/api/ui/control`, `/api/ui/secret`, and `/api/ui/events/export`.
Pass it explicitly via `--token` or set it through the `API_TOKEN` environment
variable prior to invoking the command. **Never commit tokens or secrets to
git.**

```bash
# Pause and resume trading from the terminal
codex/add-operator-runbook-documentation-30d5c6
python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" pause
python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" resume

# Rotate the Binance live secret
python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" rotate-key --value 'new-secret'

# Export recent events to a JSON file
python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" export-log --out ./events_export.json
```


python3 cli/propbotctl.py --token "$API_TOKEN" pause
python3 cli/propbotctl.py --token "$API_TOKEN" resume

# Rotate the Binance live secret
python3 cli/propbotctl.py --token "$API_TOKEN" rotate-key --value 'new-secret'

# Export recent events to a JSON file
python3 cli/propbotctl.py --token "$API_TOKEN" export-log --out ./events_export.json
```

 main
## Release helpers

Use the updated Makefile target to tag releases in sync with Docker packaging:

```bash
make release TAG=0.1.2
```

This creates an annotated `v0.1.2` tag and pushes it to the configured remote,
triggering Docker Release workflows and compose smoke tests.
