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

### Права на каталог `data`

Если разворачиваете тестовый или продакшн-контур на удалённом сервере, заранее
создайте рядом с `docker-compose.prod.yml` каталог `./data` и назначьте ему
права на запись для пользователя контейнера (например, `sudo mkdir -p ./data &&
sudo chown 1000:1000 ./data && sudo chmod 770 ./data`). Docker монтирует его в
`/app/data`, поэтому без корректных прав журнал и состояние не сохранятся.

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

For daily operational routines (status checks, HOLD management, secret rotation, exports) see `docs/OPERATOR_RUNBOOK.md`.
Оператор может работать через Telegram-бота или локальный CLI `propbotctl` (для CLI нужен локальный/SSH-доступ и bearer-токен).

## Binance safety note

When switching to `PROFILE=live`, remember that production keys in
`BINANCE_LV_API_KEY` / `BINANCE_LV_API_SECRET` unlock real funds. Keep
`SAFE_MODE=true` until manual approval, verify two-man acknowledgements, and
monitor the System Status overview for HOLD/critical alerts before resuming.
