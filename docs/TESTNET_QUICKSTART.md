# Testnet Quickstart v0.1.1

The v0.1.1 release introduces the System Status API (with automatic HOLD
fail-safe), the Telegram control/alert bot, and the `/api/ui/status/...` panel.
This addendum walks through bootstrapping PropBot against paper or Binance UM
Testnet profiles.

## Option A — Local macOS bootstrap (no Docker)

The commands below match a macOS workstation at `/Users/denis/propbot` and start
PropBot in paper mode with SAFE_MODE enabled.

```bash
/usr/bin/python3 -m venv /Users/denis/propbot/.venv
source /Users/denis/propbot/.venv/bin/activate
/Users/denis/propbot/.venv/bin/pip install -U pip wheel
/Users/denis/propbot/.venv/bin/pip install -r /Users/denis/propbot/requirements.txt
/Users/denis/propbot/.venv/bin/pytest -q
cp /Users/denis/propbot/.env.example /Users/denis/propbot/.env
SAFE_MODE=true PROFILE=paper AUTH_ENABLED=true API_TOKEN=devtoken123 \
  /Users/denis/propbot/.venv/bin/uvicorn app.main:app \
  --host 127.0.0.1 --port 8000 --reload
```

Docs and the web UI are available at `http://127.0.0.1:8000/docs` and
`http://127.0.0.1:8000/` after startup.

## Option B — Docker / Compose

Compose consumes the `TAG` variable to select the container image. Pull the
published GHCR image and start the stack (см. также продакшн-гайд в README,
раздел «🚀 Продакшн развёртывание на Linux сервере» для развёртываний на
удалённых серверах):

```bash
export REPO=my-org
docker pull ghcr.io/${REPO}/propbot:v0.1.1
TAG=v0.1.1 docker compose pull
TAG=v0.1.1 docker compose up -d
curl -s http://127.0.0.1:8000/api/ui/status/overview | jq '{overall, alerts}'
```

Use `TAG=v0.1.1 make up` and `make down` for the Makefile wrappers, or set
`BUILD_LOCAL=1 make up` to rebuild the image locally.

## Environment variables

Copy `.env.example` to `.env` and fill in the placeholders. Highlights for the
testnet profile:

- `PROFILE=testnet` запускает связку против Binance UM testnet. Для paper или
  live режимов воспользуйтесь подсказками внутри `deploy/env.example.prod`.
- `SAFE_MODE=true` блокирует отправку реальных ордеров. Отключайте только после
  ручной проверки и в тестовой среде.
- `DRY_RUN_ONLY=true` заставляет использовать внутренний симулятор. Для реального
  тестнета выставьте `DRY_RUN_ONLY=false`, но оставьте `SAFE_MODE=true`, пока не
  потребуется фактическое выставление заявок.
- `TWO_MAN_RULE=true` требует подтверждения двух операторов перед возобновлением
  торгов из HOLD, защищая от одиночных ошибок.
- `BINANCE_UM_API_KEY_TESTNET` / `BINANCE_UM_API_SECRET_TESTNET` — обязательные
  ключи для API Binance UM testnet. При необходимости переопределите базовый URL
  через `BINANCE_UM_BASE_TESTNET`.
- `ENABLE_PLACE_TEST_ORDERS=true` включает отправку заявок на тестовую биржу,
  когда `SAFE_MODE=false`. Оставьте `false`, если хотите удерживать бота в
  полностью безрисковом режиме.
- `AUTH_ENABLED=true` + `API_TOKEN=<token>` — включает bearer-аутентификацию для
  операций PATCH/POST. Токен также используется в Telegram командах.
- Лимиты риска (`MAX_POSITION_USDT`, `MAX_OPEN_ORDERS`,
  `MAX_DAILY_LOSS_USDT`) и настройки Telegram (`TELEGRAM_*`) подбираются по
  задачам команды.

Полный список переменных и подсказки для продакшена приведены в
`deploy/env.example.prod`. Секреты никогда не коммитятся в репозиторий.

## System Status API & SLO auto-HOLD

Query the new endpoints to verify the runtime state:

```bash
curl -s http://127.0.0.1:8000/api/ui/status/overview | jq '{overall, alerts}'
curl -s http://127.0.0.1:8000/api/ui/state | jq '.flags + {risk_blocked, risk_reasons}'
```

`overall` reports the aggregate health (`OK/WARN/ERROR/HOLD`). When a critical
SLO is breached (for example, recon mismatch or persistent latency breach), the
runtime automatically flips into HOLD, enforces SAFE_MODE, and stops the
loop. All secrets in the payload are redacted as `***redacted***`.

Mutate runtime parameters via `PATCH /api/ui/control` while running paper or
testnet with SAFE_MODE enabled:

```bash
curl -X PATCH http://127.0.0.1:8000/api/ui/control \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"order_notional_usdt": 75, "min_spread_bps": 1.0, "dry_run_only": true}'
```

`GET /api/ui/events` keeps powering the dashboard and the `/api/ui/events/export`
utility.

## Telegram control bot

Enable the Telegram bot by exporting `TELEGRAM_ENABLE=true`, the bot token, and
an authorised chat ID. Once running, the bot:

- pushes status snapshots (PnL, profile, SAFE_MODE, open positions, risk
  breaches) every `TELEGRAM_PUSH_MINUTES`,
- accepts `/pause`, `/resume`, `/status`, and `/close` (`/close_all`) commands
  from the authorised chat,
- redacts secrets from every outgoing message.

The `/close` command triggers `cancel_all_orders` and is only honoured while the
profile is set to `testnet`.

## Binance safety note

When switching to `PROFILE=live`, remember that production keys in
`BINANCE_LV_API_KEY` / `BINANCE_LV_API_SECRET` unlock real funds. Keep
`SAFE_MODE=true` until manual approval, verify two-man acknowledgements, and
monitor the System Status overview for HOLD/critical alerts before resuming.
