# Go-Live Checklist

Полный пакет действий перед запуском PropBot на боевом окружении. Документ
собирает профильные конфиги, обязательные флаги и лимиты, pre-flight проверки и
операционные процедуры для инцидентов.

## Профили окружений

| Профиль | Конфиг | Назначение | Ключевые параметры |
| --- | --- | --- | --- |
| `paper` | `configs/config.paper.yaml` | Изолированная симуляция без реальных ордеров. По умолчанию включает `safe_mode`, `dry_run` и two-man rule, использует тестнет-соединения и мягкие лимиты на нотионал. 【F:configs/config.paper.yaml†L1-L61】 |
| `testnet` | `configs/config.testnet.yaml` | Боевой стек против биржевых testnet-эндпоинтов. Включены `safe_mode`, `dry_run`, two-man rule и runaway-лимиты; задержки и edge допускают эксперименты с «боевым» флоу без средств. 【F:configs/config.testnet.yaml†L1-L63】 |
| `live` | `configs/config.live.yaml` | Настоящие счета и маршруты Binance/OKX. Строже лимиты (`rate_limit`, `runaway_breaker`, notional caps) и включён kill-caps guard. Перед запуском убедитесь, что `SAFE_MODE=true` и активирована двухоператорная защита. 【F:configs/config.live.yaml†L1-L42】 |

Профиль выбирается через `PROFILE`, `DEFAULT_PROFILE` и `MODE` в `.env`; runtime
нормализует значение до lowercase и строит control state из выбранного бандла. 【F:app/services/runtime.py†L700-L733】

## Флаги и лимиты

### Управление режимом

* `SAFE_MODE` — запрещает реальное размещение ордеров; для live всегда стартуем с `true`. 【F:.env.example†L11-L20】
* `DRY_RUN_ONLY` и `DRY_RUN_MODE` — форсируют симуляцию в петле и частичных хеджах; сбросьте в `false` перед фактической торговлей. 【F:.env.example†L11-L24】
* `TWO_MAN_RULE` — требуются два approvals перед возобновлением RUN и ручными хеджами. Runtime подхватывает флаг из окружения и отражает в control state. 【F:app/services/runtime.py†L713-L733】
* `AUTOPILOT_ENABLE` — разрешает автопилоту снимать HOLD после рестарта, только если блокеры отсутствуют (safe_mode снят, budgets чистые, биржи доступны). 【F:.env.example†L15-L24】【F:app/services/autopilot.py†L23-L72】

### Риск и ограничения

* Нотиональные лимиты: `MAX_OPEN_POSITIONS`, `MAX_TOTAL_NOTIONAL_USDT`, `MAX_POSITION_USDT__*`, `MAX_OPEN_ORDERS__*`. Значения подтягиваются при старте и попадают в `risk.limits`. 【F:.env.example†L25-L46】【F:app/services/runtime.py†L773-L823】
* Runaway breaker: `MAX_ORDERS_PER_MIN`, `MAX_CANCELS_PER_MIN` и YAML-конфиг `guards.runaway_breaker`. Превышение переводит систему в HOLD через runaway guard. 【F:.env.example†L21-L36】【F:configs/config.live.yaml†L5-L10】
* Дневной убыток: `DAILY_LOSS_CAP_USDT` (`ENFORCE_DAILY_LOSS_CAP`, `DAILY_LOSS_CAP_AUTO_HOLD`). Снапшот хранит `enabled`, `blocking` и `breached`; AutopilotGuard переводит режим в HOLD при пробое. 【F:.env.example†L31-L42】【F:app/risk/daily_loss.py†L45-L183】【F:app/services/autopilot_guard.py†L35-L145】
* Стратегические бюджеты: менеджер инициализируется из runtime state и валидации сред через `StrategyBudgetManager`; автопилот не разрешит RUN, если стратегия заблокирована бюджетом. 【F:app/strategy_budget.py†L62-L162】【F:app/services/autopilot.py†L23-L72】

### Наблюдаемость и защита

* Watchdog: включите `WATCHDOG_ENABLED` и, при необходимости, `WATCHDOG_AUTO_HOLD`, чтобы автоматически фиксировать деградации бирж. Снапшоты транслируются в бейджи и могут триггерить HOLD. 【F:.env.example†L49-L60】【F:app/watchdog/exchange_watchdog.py†L43-L167】【F:app/services/autopilot_guard.py†L116-L145】
* Chaos-флаги держите выключенными (`FEATURE_CHAOS=0`) на live, иначе будут искусственные сбои в адаптерах. 【F:.env.example†L63-L71】
* Recon: `RECON_ENABLED`, `RECON_AUTO_HOLD` и `SHOW_RECON_STATUS` управляют сверкой позиций и видимостью виджета/бейджа. 【F:.env.example†L57-L61】【F:app/services/operator_dashboard.py†L539-L549】【F:app/services/recon_runner.py†L83-L114】

## Live Readiness Gate

* Новый агрегатор `LiveReadinessAggregator` собирает сигналы runtime (`pre_trade_gate`, риск-гард, recon, watchdog, market-data) и выдаёт `status`=`GREEN|YELLOW|RED` плюс список причин. 【F:app/readiness/aggregator.py†L26-L262】
* REST ручка `GET /live/readiness` возвращает снимок, а Prometheus экспонирует `readiness_status{status="..."}` и `readiness_reason_total{reason="..."}` для дашбордов. 【F:app/api/ui/readiness.py†L1-L12】【F:app/readiness/aggregator.py†L33-L82】
* Автозапуск live-режима блокируется до `GREEN`, если `WAIT_FOR_LIVE_READINESS_ON_START` включён (по умолчанию для `live`/`testnet`). Таймаут на ожидание берётся из `readiness.startup_timeout_sec`. 【F:app/main.py†L34-L123】【F:configs/config.live.yaml†L57-L64】
* При `RED` в списке причин появится `pretrade_throttled`, `risk_throttled`, `md_staleness`, `watchdog_down` и т.п. — UI отображает бейдж Readiness с подсказкой по активным блокерам. 【F:app/readiness/aggregator.py†L88-L205】【F:app/templates/status.html†L462-L575】

## Pre-flight checks

1. **Конфиг и переменные**. Подтяните `.env`, убедитесь, что `PROFILE=live`, `SAFE_MODE=true`, `TWO_MAN_RULE=true`, `AUTOPILOT_ENABLE=false`, заданы `APPROVE_TOKEN` и API-ключи. 【F:.env.example†L5-L118】
2. **Acceptance suite**. Выполните `make acceptance` — проверяется `healthz`, `live-readiness` и устойчивость к mild chaos. 【F:docs/OPERATOR_RUNBOOK.md†L63-L71】
3. **Smoke + дашборд**. Прогоните `scripts/smoke.sh` против `SMOKE_HOST`, затем проверьте `/ui/dashboard`: бейджи `auto_trade=OFF`, `risk_checks=ON`, `daily_loss=OK`, `watchdog=OK`. 【F:.env.example†L49-L58】【F:app/services/runtime_badges.py†L41-L81】
4. **Сверка**. Запустите `python -m app.tools.replay_runner` (оффлайн отчёт) и убедитесь, что `GET /api/ui/recon/status` без диффов. 【F:docs/OPERATOR_RUNBOOK.md†L92-L123】【F:app/services/recon_runner.py†L83-L114】
5. **Бюджеты и лимиты**. Через `/api/ui/ops_report` убедитесь, что `strategy_budgets` не заблокированы и дневной лимит выставлен. 【F:app/services/ops_report.py†L315-L384】【F:app/services/autopilot.py†L23-L72】
6. **Two-Man approvals**. Проверьте `/api/ui/approvals` — очередь пуста, `two_man_resume_required=true`. 【F:app/services/runtime.py†L713-L733】【F:app/services/status.py†L252-L254】

## Two-Man approvals, daily loss и бюджеты

1. **Инициировать возобновление**: первый оператор выполняет `POST /api/ui/resume-request` с причиной и своим именем; событие логируется и сохраняет `request_id`. 【F:app/routers/ui.py†L1202-L1235】
2. **Подтвердить**: второй оператор вызывает `POST /api/ui/resume-confirm` с `APPROVE_TOKEN` и `request_id`. Без токена или при несоответствии HOLD остаётся активным. 【F:app/routers/ui.py†L1295-L1335】
3. **Снять HOLD**: после двух approvals оператор (или автопилот при включённом `AUTOPILOT_ENABLE`) вызывает `POST /api/ui/resume`. Перед запуском убедитесь, что `SAFE_MODE=false`. 【F:app/routers/ui.py†L1338-L1353】
4. **Контролировать бюджеты**: если автопилот возвращает `strategy_budget_blocked:*`, проверьте `GET /api/ui/strategy/status` и скорректируйте лимиты через runtime state/JSON, затем повторите approvals. 【F:app/services/autopilot.py†L23-L72】【F:app/services/operator_dashboard.py†L610-L650】
5. **Дневной лимит**: мониторьте `/api/ui/status` и `/api/ui/ops_report` — при `daily_loss` = `BREACH` AutopilotGuard переведёт систему в HOLD, и возврат в RUN возможен только после ручного подтверждения и, при необходимости, reset лимита. 【F:app/services/runtime_badges.py†L41-L81】【F:app/services/autopilot_guard.py†L91-L145】

## Playbooks

### Recon-инцидент

1. **Детект**: бейдж `reconciliation` на дашборде/`/api/ui/recon/status` показывает `MISMATCH` или `AUTO_HOLD`, `diff_count>0`. 【F:app/services/operator_dashboard.py†L539-L574】【F:app/services/recon_runner.py†L83-L114】
2. **Диагностика**: выгрузите список `diffs` через `/api/ui/recon/status` или `app/services/recon_runner.ReconRunner.run_once()` (ручной прогон). 【F:app/services/recon_runner.py†L83-L114】
3. **Действия**:
   * Проверить балансы/позиции на биржах, сверить `ledger`.
   * Если auto-HOLD активен, `engage_safety_hold` уже вызван раннером; держите HOLD до устранения. 【F:app/services/recon_runner.py†L105-L169】
4. **Восстановление**: после корректировок запустите `python - <<'PY'` с `get_runner().run_once()` либо дождитесь фонового интервала. Убедитесь, что `diff_count=0`, статус `OK`, после чего проходите процедуру Two-Man resume. 【F:app/services/recon_runner.py†L83-L127】【F:app/routers/ui.py†L1202-L1353】

### Watchdog-инцидент

1. **Детект**: бейдж `watchdog=DEGRADED/AUTO_HOLD`, `/api/ui/watchdog/status` фиксирует `auto_hold`. 【F:app/services/runtime_badges.py†L41-L81】【F:app/watchdog/exchange_watchdog.py†L85-L129】
2. **Диагностика**: проверить `watchdog_reason` и последние переходы (`get_recent_transitions`). Уточнить доступность REST/WebSocket биржи.
3. **Митигировать**: при `AUTO_HOLD` AutopilotGuard отключит автоторговлю; удерживайте HOLD, пока связь не восстановится. При долгом даунтайме задействуйте kill-switch/hedge вручную. 【F:app/services/autopilot_guard.py†L116-L172】
4. **Восстановление**: после нормализации маркеров вручную переснимите watchdog-снимок или дождитесь автоматического обновления, убедитесь, что статус `OK`, затем выполните стандартный Two-Man resume. 【F:app/watchdog/exchange_watchdog.py†L169-L200】【F:app/routers/ui.py†L1202-L1353】

### Rollback

1. **Снять снапшот**: при активном `INCIDENT_MODE_ENABLED` выполнить `POST /api/ui/incident/snapshot` c комментарием — файл сохраняется под `data/snapshots/incident_*.json`. 【F:app/routers/ui_incident.py†L48-L66】
2. **Запросить откат**: `POST /api/ui/incident/rollback` с `confirm=false`. Создаётся approval-запрос; запомните `request_id`. 【F:app/routers/ui_incident.py†L68-L90】
3. **Подтвердить**: получить одобрение через `/api/ui/approvals`, затем повторить `POST /api/ui/incident/rollback` с `confirm=true`, `request_id` и исходным `path`. Endpoint сверяет путь и возвращает восстановленный снапшот. 【F:app/routers/ui_incident.py†L90-L121】
4. **Возврат в RUN**: после отката выполните checklist (budgets, watchdog, recon) и повторите двухоператорное возобновление. 【F:app/routers/ui.py†L1202-L1353】

