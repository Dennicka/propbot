# RUNBOOK — операционные процедуры

## 1. Запуск сервиса
1. Выбрать профиль: `export EXCHANGE_PROFILE=paper` (или `testnet` / `live`).
2. SAFE_MODE остаётся `true` до двух подтверждений.
3. Запустить `uvicorn app.server_ws:app --host 0.0.0.0 --port 8000`.
4. Проверить `/api/health`, `/live-readiness`, `/api/ui/status/overview`.

## 2. Smoke-check
```bash
pytest -q tests/test_smoke.py
curl -s http://127.0.0.1:8000/api/arb/preview | jq
```

## 3. Управление SAFE_MODE / Two-Man Rule
- `POST /api/arb/preview` выполняет preflight (без ордеров).
- Для выхода из SAFE_MODE: два оператора вызывают `register_approval` (UI) → `safe_mode=false` через конфиг/ENV → повторный preflight.
- `/api/ui/control-state` отражает approvals и статус гардов.

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
2. Наблюдать `/metrics` и `/api/ui/status/overview` ≥15 минут.
3. В случае деградации — `./deploy/rollback.sh` → предыдущий релиз.

## 7. Чистка HOLD
- После устранения причины установить соответствующий гард в `OK` через UI/админку.
- Перезапустить preflight (`POST /api/arb/preview`), убедиться что overview=OK.
- Если включён live-режим → собрать approvals заново.
