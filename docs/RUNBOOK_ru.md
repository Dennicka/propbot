# RUNBOOK — операционные процедуры

## 1. Подготовка и запуск (paper/testnet)
1. Заполнить `.env` (ключи, `EXCHANGE_PROFILE=testnet`, `SAFE_MODE=true`).
2. Подтянуть переменные: `export $(grep -v '^#' .env | xargs)`.
3. Убедиться в зависимостях: `pip install -r requirements.txt`.
4. Запустить `uvicorn app.server_ws:app --host 0.0.0.0 --port 8000`.
5. Проверить `/api/health`, `/live-readiness`, `/api/ui/status/overview`, `/api/deriv/status`.
6. Зафиксировать `/api/ui/state` → блок `flags` показывает `MODE`, `SAFE_MODE`, `POST_ONLY`, `REDUCE_ONLY`, `ENV`.

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

## Pre-trade risk gate
- Перед тем как выводить систему из SAFE_MODE и отдавать `/api/arb/execute`/
  `/api/arb/confirm`, убедитесь, что `risk_gate(order_intent)` разрешает сделку.
- Проверка активируется только при `RISK_CHECKS_ENABLED=1`
  (`FeatureFlags.risk_checks_enabled()`); без флага gate отвечает
  `{"allowed": true, "reason": "disabled"}` и не блокирует сделки.
- При активном флаге хелпер суммирует `intent_notional` и прирост позиций с
  текущим snapshot'ом (`safety.risk_snapshot`) и проверяет лимиты
  `MAX_TOTAL_NOTIONAL_USDT` и `MAX_OPEN_POSITIONS` через `RiskGovernor`.
- При превышении API вернёт HTTP 200 с телом вида
  `{ "status": "skipped", "reason": "risk.max_notional", "cap": "max_total_notional_usdt" }`
  (или `risk.max_open_positions`) и не отправит ордера. В DRY_RUN режимах
  проверка не срабатывает, можно безопасно тренироваться.

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
- При срабатывании `runaway_guard_v2`:
  - Проверить блок "Runaway guard v2" на `/ui/dashboard` — он показывает текущие счётчики по venue/символу, лимит `max_cancels_per_min`, активный cooldown и метку `last_trigger_ts`.
  - На `/api/ui/status/overview` поле `runaway_guard.v2.last_block` содержит причину блокировки и детали (venue, символ, текущий счётчик).
  - Дождаться окончания cooldown (`cooldown_remaining=0`) и убедиться, что счётчики в окне 60 секунд сброшены.
  - Зафиксировать ручное вмешательство в журнале, создать `resume_request` с причиной (например, "runaway guard cleared") и подтвердить его, чтобы снять HOLD.

## 8. Strategy orchestrator
- Центральный переключатель стратегий доступен через API `/api/ui/strategy/*` и управляет списком активных стратегий.
- Каждое включение или отключение стратегии оператором логируется в `audit_log` с каналом `orchestrator`; указывайте причину в запросе.
- При переводе стратегий обязательно фиксируйте причину в таск-трекере/операционном журнале, чтобы сохранить трассировку решений.

## 9. Лидер-лок и фейловер
- При `FEATURE_LEADER_LOCK=1` только инстанс с `leader=true` из `/healthz` и `/live-readiness` имеет право переводить систему в `RUN`. Второй инстанс автоматически держит HOLD и возвращает `ready=false`.
- `/live-readiness` дополнительно показывает `fencing_id` и `hb_age_sec` (возраст heartbeat). Текущий heartbeat пишется в `data/leader.hb` (`pid`, `fencing_id`, `ts`). У активного лидера `hb_age_sec` держится в коридоре 0–2 сек. Если возраст растёт, лидер завис или остановлен.
- Грациозная передача лидера:
  1. На принимающем узле поднять сервис, но оставить его в HOLD. Проверить `/live-readiness` → `leader=false`, `hb_age_sec` растёт до тех пор, пока лок не будет освобождён.
  2. На текущем лидере перевести бота в HOLD и явно освободить лок:
     ```bash
     python - <<'PY'
     from app.runtime import leader_lock
     leader_lock.release()
     PY
     ```
     После выполнения `/live-readiness` на старом узле покажет `leader=false`, `fencing_id=null`, `hb_age_sec` начнёт расти.
  3. На новом узле дождаться, пока `/live-readiness` вернёт `leader=true` и новый `fencing_id`. Убедиться, что `hb_age_sec` снова малый (<2 сек).
  4. Проверить, что старый узел остаётся в HOLD, и только после этого собирать approvals и выводить новый лидер из HOLD.
- Форсированный перехват (steal) при зависшем лидере:
  1. Убедиться, что старый инстанс остановлен или переведён в HOLD. Считать `hb_age_sec`/`ts` со старого `/live-readiness` или напрямую из `data/leader.hb`.
  2. На новом узле дождаться, когда `hb_age_sec` текущего лидера превысит `LEADER_LOCK_STALE_SEC` (по умолчанию `max(2*TTL, 60)` секунд). Следующий вызов `/live-readiness` на новом узле получит лок, вернёт новый `fencing_id`, `ready=true` и `hb_age_sec≈0`.
  3. Зафиксировать смену в журнале (новый `fencing_id`, рост `hb_age_sec` на старом узле). После восстановления старого хоста удалить `data/leader.lock`/`data/leader.hb`, вручную держать HOLD и убедиться, что он не берёт лидерство обратно.
- После любой смены лидера прогоняем `/live-readiness`, `/healthz` и `/api/ui/status/overview`. Только после зелёных статусов собираем approvals и выводим систему из HOLD.
