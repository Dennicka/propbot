# RUNBOOK — операционные процедуры

## 1. Подготовка и запуск (paper/testnet)
1. Заполнить `.env` (ключи, `EXCHANGE_PROFILE=testnet`, `SAFE_MODE=true`).
2. Подтянуть переменные: `export $(grep -v '^#' .env | xargs)`.
3. Убедиться в зависимостях: `pip install -r requirements.txt`.
4. Запустить `uvicorn app.server_ws:app --host 0.0.0.0 --port 8000`.
5. Проверить `/api/health`, `/live-readiness`, `/api/ui/status/overview`, `/api/deriv/status`.

## 2. Smoke-check
```bash
pytest -q tests/test_smoke.py
curl -s http://127.0.0.1:8000/api/arb/preview | jq
```

## 3. Префлайт и исполнение
- `POST /api/arb/preview` — обязательный префлайт, проверяются connectivity, фильтры, edge (после комиссий и с учётом slippage).
- SAFE_MODE `true` ⇒ `/api/arb/execute` возвращает dry-run план с шагами state-machine и rescue-сценарием.
- Для реального исполнения (testnet/live):
  1. Два оператора подтверждают запуск через UI `/api/ui/approvals` (Two-Man Rule).
  2. Перезапускаем сервис с `SAFE_MODE=false` (или применяем конфиг) только после зелёного префлайта.
  3. `POST /api/arb/execute` — с IOC/Post Only параметрами (см. `configs/config.*.yaml`).
- После сделки — `POST /api/hedge/flatten` для reduceOnly закрытия всех ног.

## 4. Обработка инцидентов
- P0-гард переведён в WARN/HOLD → сервис переходит в HOLD.
- Проверить `/api/ui/status/components` (компонент `runaway_breaker`, `cancel_on_disconnect` и т.д.).
- Для runaway rescue: `POST /api/hedge/flatten`, затем ручная проверка позиций.
- Инциденты логируются в `RuntimeState.incidents` и доступны в `/api/ui/status/components` (Incident Journal).

## 5. Конфиг-пайплайн
1. `POST /api/ui/config/validate` → убедиться, что YAML валиден.
2. `POST /api/ui/config/apply` → сохраняет бэкап и перезагружает runtime.
3. При проблеме — `POST /api/ui/config/rollback`.

## 6. План выпуска
1. `./deploy/release.sh` — выкладывает билд в `/opt/crypto-bot/releases/<ts>` и перезапускает systemd.
2. Перед включением live убедиться, что `.env` содержит боевые ключи и `SAFE_MODE=true`.
3. Наблюдать `/metrics`, `/api/ui/status/overview` ≥15 минут; пройти префлайт и approvals перед `SAFE_MODE=false`.
4. В случае деградации — `./deploy/rollback.sh` → предыдущий релиз.

## 7. Чистка HOLD
- После устранения причины установить соответствующий гард в `OK` через UI/админку.
- Перезапустить preflight (`POST /api/arb/preview`), убедиться что overview=OK.
- Если включён live-режим → собрать approvals заново.
