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
5. Убедитесь, что `/healthz` отвечает `{"ok": true}`.
6. Для реальной торговли выполните ручной двухшаговый RESUME: сначала
   `POST /api/ui/resume-request`, затем `POST /api/ui/resume-confirm` с
   `APPROVE_TOKEN`.

⚠️ Без ручного двухшагового RESUME хеджер остаётся в SAFE_MODE/HOLD и не начнёт
реально торговать, даже если контейнер уже запущен.

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
  - `BINANCE_API_KEY` / `BINANCE_API_SECRET` — primary credentials used by the
    new Binance USDⓈ-M hedge client.
  - `OKX_API_KEY` / `OKX_API_SECRET` / `OKX_API_PASSPHRASE` — OKX perpetual
    hedge client credentials (use a restricted sub-account and IP whitelist).

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
