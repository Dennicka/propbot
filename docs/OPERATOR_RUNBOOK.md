# Operator Runbook

Единый плейбук для операторов PropBot (paper/testnet/live). Документ описывает
сигналы готовности, автоматические защиты и публичные отчёты, на которые
опирается смена во время дежурства.

## Профили и запуск

* **Старт через CLI.** Для paper/testnet/live профилей используем `make run-paper`,
  `make run-testnet` и `make run-live`. Цели Makefile вызывают `python -m app.cli
  run-profile <profile>`, который выставляет набор стандартных env-флагов и
  блокирует запуск при отсутствии guard’ов или secrets. 【F:Makefile†L33-L44】【F:app/config/profiles.py†L70-L185】
* **Логи bootstrap.** При старте в лог попадает профиль, активные лимиты и
  снимок guard’ов (SLO, hedge, recon, watchdog) плюс контрольные флаги HOLD,
  SAFE_MODE и DRY_RUN. Если что-то отключено, логи сразу подскажут. 【F:app/main.py†L62-L109】
* **Переключение профиля.** Меняем профиль только через перезапуск с новым
  параметром `run-profile`. Значение `PROFILE` и конфиг с лимитами подбираются
  автоматически, дополнительно ничего править не нужно. 【F:app/config/profiles.py†L70-L118】【F:app/services/runtime.py†L51-L87】

## Безопасная остановка/запуск

1. **Перед остановкой включи HOLD.** Через UI (`/api/ui/hold`) или CLI/бота
   активируй HOLD с причиной — вызов прокидывает `hold_loop()` и фиксирует
   причину в `runtime.safety`. 【F:app/routers/ui.py†L1256-L1306】
2. **Дождись, пока `hold_active=true`.** Проверить можно по `/api/ui/status` или
   в UI (бейдж `auto_trade=OFF`). Только после этого останавливай процесс (Ctrl+C
   или остановка systemd/Docker).
3. **Старт всегда в HOLD/DRY_RUN.** CLI для всех профилей ставит `SAFE_MODE=true`
   и `DRY_RUN_MODE=true`, поэтому после рестарта система безопасна и ждёт
   двухоператорного resume. 【F:app/config/profiles.py†L94-L118】【F:app/main.py†L96-L109】

## Статусные бейджи

Runtime агрегирует четыре статусных бейджа, которые отображаются на
`/ui/dashboard`, экспортируются в `/api/ui/ops_report` и участвуют в
`/live-readiness`:

| Бейдж        | Значения                  | Что означает |
| ------------ | ------------------------- | ------------ |
| `auto_trade` | `ON` / `OFF`              | Автоторговля активна только когда автопилот в RUN и `DRY_RUN_MODE=false`. |
| `risk_checks`| `ON` / `OFF`              | Включена ли валидация `FeatureFlags.risk_checks_enabled()` в risk core. |
| `daily_loss` | `OK` / `BREACH`           | Статус глобального дневного лимита PnL. |
| `watchdog`   | `OK` / `DEGRADED` / `AUTO_HOLD` | Состояние биржевого вотчдога; `AUTO_HOLD` переводит live-readiness в `false`. |
| `partial_hedges` | `OK` / `PARTIAL` / `REBALANCING` | Состояние хвостов частичных хеджей и работы дозаявок. |

Значения рассчитываются в `app/services/runtime_badges.py` на основе runtime
состояния, флагов риска и снимка watchdog. 【F:app/services/runtime_badges.py†L7-L44】

## Recon v2: что означает OK/MISMATCH/DEGRADED и когда срабатывает AUTO_HOLD

Runtime публикует отдельный блок «Reconciliation status» в `/ui/dashboard`,
`/api/ui/status` и `/api/ui/recon_status`. Поле `summary` отражает текущий
результат сверки позиций между PropBot и биржами.

* `OK` — расхождений нет. `desync_detected=false`, `diff_count=0`, `issue_count=0`.
  UI показывает «Reconciliation status: OK» и «Status: OK».
* `MISMATCH` — найдены несоответствия (отчёты биржи против внутренних записей).
  Любые ненулевые `diff_count`/`issue_count` или флаг `desync_detected=true`
  приводят к этому статусу. На дашборде отображается список Outstanding mismatches
  и подсказка «STATE DESYNC — manual intervention required».
* `DEGRADED` — recon обнаружил деградацию сервиса или включил AUTO_HOLD. Это
  случается, когда адаптеры сообщают `status=degraded` или авто-холд уже активен.
  Для оператора приоритет — дождаться повторного успешного запуска сверки.
* `AUTO_HOLD` — recon принудительно перевёл систему в HOLD из-за критических
  расхождений. В этом режиме автоторговля заблокирована, пока сверка не даст OK.

### AUTO-HOLD по RECON

Фоновый `ReconDaemon` сравнивает фактические позиции/балансы с данными в
ledger. Если хотя бы один снэпшот уходит в `status=CRITICAL`, демон переводит
систему в `HOLD` с причиной `RECON_DIVERGENCE` и выставляет метку `auto_hold` в
runtime. Оператор должен:

1. Зафиксировать текущее состояние (`/api/ui/recon_status`, «Recon» виджет на
   дашборде показывает баннер `RECON DIVERGENCE`).
2. Проверить лог `event=recon_snapshot` (в `golden`/ops) — там видно по каким
   инструментам расхождения.
3. Убедиться, что позиции приведены в порядок (закрытие, ручная корректировка
   балансов, повторная синхронизация).
4. Дождаться трёх подряд успешных циклов (`status=OK`). После этого демон сам
   снимет авто-холд и вернёт режим `RUN`. Если требуется преждевременный
   `resume`, используйте стандартную процедуру (двухфакторное подтверждение).

Пока авто-холд активен, счётчик `recon_auto_hold_triggered_total` позволяет
отслеживать частоту срабатываний. 【F:app/recon/daemon.py†L108-L219】

Если фича `RECON_ENABLED=0`, блок отображается как `DISABLED`, но остальные
метрики (последний запуск, количество несостыковок) остаются доступными для
мониторинга. 【F:app/services/runtime.py†L2116-L2195】【F:app/services/operator_dashboard.py†L1038-L1503】

## Autopilot guard

Фоновый `AutopilotGuard` запускается вместе с приложением и раз в несколько
секунд проверяет две критичные защиты:

* дневной loss-cap (`get_daily_loss_cap_state`). При первом переходе в состояние
  `enabled=true`, `blocking=true`, `breached=true` автопилот переводит систему в
  HOLD и логирует событие `reason=DAILY_LOSS_BREACH`.
* биржевой вотчдог. Если любая биржа публикует `status=AUTO_HOLD`, guard
  выключает автоторговлю и фиксирует причину `reason=WATCHDOG_AUTO_HOLD`.

Цикл использует `hold_loop()` для безопасного отключения автоторговли и
дублирует событие в `audit_log` от имени `system`. Интервал чтения задаётся
флагом `AUTOPILOT_GUARD_INTERVAL_SEC` (по умолчанию 5 секунд). 【F:app/services/autopilot_guard.py†L24-L174】

## Exchange Watchdog & Error-Budget

`BrokerWatchdog` агрегирует операционные метрики по площадкам: лаг и разрывы
веб-сокетов, REST-ошибки (5xx/таймауты) и частоту отказов заявок. Для каждой
биржи рассчитываются rolling-метрики и сравниваются с порогами из конфигурации
(`cfg.watchdog.thresholds`). Результат публикуется в `/api/ui/status` в виде
блока `watchdog`, а на дашборде отображается бейдж «Exchange watchdog».

* `state=OK` — показатели в норме, `risk_throttled=false`.
* `state=DEGRADED` — превышены soft-пороги (например, 2+ disconnect в минуту);
  Runtime включает `risk_throttled=true`, ордера продолжают выполняться через
  обычные rate-limit'ы, но UI подчёркивает деградацию. Бейдж на дашборде
  подсвечивается жёлтым, `watchdog.last_reason` указывает, какая метрика
  превысила лимит (например, `binance:DEGRADED:ws_lag_ms_p95_elevated`).
* `state=DOWN` — жёсткий порог (6 disconnect/min, spike по lag, серия
  reject'ов). Если `cfg.watchdog.auto_hold_on_down=true`, Runtime переводит
  систему в HOLD с причиной `EXCHANGE_WATCHDOG::<VENUE>::DOWN`, блокирует
  отправку новых ордеров (`block_on_down=true`) и фиксирует событие в журнале.

`BrokerWatchdog` ведёт error-budget с окном `cfg.watchdog.error_budget_window_s`
и автоматически снимает троттлинг после двух последовательных стабильных окон.
Все метрики экспортируются в Prometheus (`propbot_watchdog_*`) и доступны для
алертинга: `watchdog_state{state="DOWN"}` — страница SRE, `auto_hold_total`
используется для postmortem отчётов.

## Risk Throttling & Auto-HOLD

Параллельно с вотчдогом работает risk governor, который считает rolling-окно
успешности размещения ордеров (`window_sec` по умолчанию 1 час) и состояние
бирж. В `/ui/dashboard` отображается баннер **RISK_THROTTLED** когда:

* `success_rate_1h < min_success_rate` (по дефолту 98.5%) — слишком много
  отказов/отклонений;
* `order_error_rate > max_order_error_rate` (1% по дефолту);
* или вотчдог опустился ниже `min_broker_state` (например, DEGRADED).

Баннер содержит человекочитаемую причину и текущий success rate (с округлением
до двух знаков). В API `/api/ui/status` и `/api/ui/status/overview` добавлен
блок `risk`, где UI и алерты считывают состояние троттлинга.

Если несколько окон подряд (`hold_after_windows`, по умолчанию 2) остаются в
состоянии throttle, governor инициирует авто-HOLD c причиной `RISK::<reason>`.
После двух стабильных окон (success rate выше порога, ошибок ниже порога)
троттлинг снимается автоматически. Метрики доступны в Prometheus под
префиксом `propbot_risk_*` (`risk_success_rate_1h`, `risk_order_error_rate_1h`,
`risk_throttled{reason=...}` и счётчик окон `risk_windows_total`).

## Pre-Trade Gate и дросселирование

Pre-Trade Gate — это тонкая прослойка перед risk-governor, которая мгновенно
останавливает отправку ордеров, когда error-budget, watchdog или risk governor
переводят систему в состояние `throttled`. В отличие от HOLD, gate действует на
уровне заявки: ордер не доходит до биржи и не проходит даже pre-trade валидацию.

* Источник блокировки прописывается в `reason` (`ERROR_BUDGET::...`,
  `HIGH_ORDER_ERRORS`, `WATCHDOG::binance:DEGRADED` и т.п.). При повторных вызовах
  с тем же reason gate не логирует дубликаты.
* Состояние доступно через `GET /api/ui/pretrade_gate` (и совместимый алиас
  `/api/ui/pretrade/status`). Ответ: `{ "throttled": bool, "reason": str|null }`
  + поле `updated_ts` — unix-timestamp последнего изменения. Endpoint требует
  operator-токен.
* На дашборде блокируется пайплайн «Order pipeline» и подсвечивается последняя
  причина в секции «Pre-trade blocks» (данные берутся из `runtime.safety.last_pretrade_block`).
* Gate автоматически сбрасывается, когда watchdog/error-budget возвращаются в
  норму и risk governor снимает троттлинг (в UI reason становится `null`).

Для ручной проверки можно вызвать `runtime.get_pre_trade_gate_status()` из REPL или
посмотреть snapshot в `/api/ui/state` — там появляется блок `pre_trade_gate`.

## Exposure Caps

Лимиты экспозиции (глобальные, по направлению и по бирже) задаются в
`exposure_caps` конфигурации и применяются перед risk governor. Runtime берёт
текущие позиции из ledger, строит `ExposureCapsSnapshot` и блокирует только те
ордера, которые увеличивают абсолютную позицию сверх ближайшего лимита. 【F:app/risk/exposure_caps.py†L1-L399】

* При превышении pre-trade возвращает `EXPOSURE_CAPS::GLOBAL|SIDE|VENUE`,
  запись фиксируется в `/api/ui/system_status`, Prometheus (`propbot_exposure_*`)
  и журнале `exposure_caps_block`. UI дублирует событие в секции Risk/Status и
  подсвечивает бейдж «EXPOSURE THROTTLED». 【F:app/router/order_router.py†L274-L375】【F:app/services/status.py†L680-L705】【F:app/services/operator_dashboard.py†L2406-L2440】
* Reduce-only ордера (сокращающие позицию) проходят даже при превышенных капах —
  используйте ручные закрытия для ликвидации лишних плечей. 【F:app/router/order_router.py†L248-L333】
* Для разблокировки либо уменьшите позицию вручную, либо скорректируйте капы в
  `configs/config.*.yaml` и перезагрузите runtime. Поднимайте лимиты только после
  согласования с риск-менеджером. После обновления конфигурации убедитесь, что
  `/api/ui/system_status` и дашборд показывают актуальные значения. 【F:configs/config.live.yaml†L19-L33】

## Account Health & Reduce-Only Mode

Account health guard собирает маржинальные снапшоты с брокерских адаптеров и
классифицирует каждую биржу по трём уровням: `OK` (запас свободного
коллатерала выше `health.free_collateral_warn_usd` и margin-ratio меньше
`health.margin_ratio_warn`), `WARN` (достигнут мягкий порог) и `CRITICAL`
(жёсткие пороги по margin-ratio или свободному коллатералу). 【F:app/health/account_health.py†L87-L125】【F:app/config/schema.py†L286-L327】

* При `WARN` guard включает `risk_throttled` с причиной
  `ACCOUNT_HEALTH_WARN`, но не трогает pre-trade gate и HOLD. 【F:app/risk/guards/health_guard.py†L120-L156】
* При `CRITICAL` guard блокирует pre-trade gate с причиной
  `ACCOUNT_HEALTH_CRITICAL`, переводит runtime в HOLD с
  `ACCOUNT_HEALTH::CRITICAL::<EXCHANGE>` и продлевает риск-троттлинг. Router в
  этом режиме пропускает только reduce-only заявки: любые ордера, увеличивающие
  абсолютную позицию, блокируются, а reduce-only отмечаются флагом, если биржа
  его поддерживает. 【F:app/risk/guards/health_guard.py†L158-L210】【F:app/router/order_router.py†L56-L111】
* После двух последовательных окон `OK` guard автоматически снимает
  троттлинг, очищает pre-trade gate и, если HOLD был выставлен им же,
  инициирует `autopilot_apply_resume`. 【F:app/risk/guards/health_guard.py†L212-L274】

### Что делать оператору

1. **Понять причину.** На `/ui/dashboard` появится бейдж «ACCOUNT HEALTH» и,
   при критике, красный баннер с причиной — например,
   `ACCOUNT_HEALTH::CRITICAL::BINANCE`. 【F:app/services/operator_dashboard.py†L1038-L1116】
2. **Пополнить счёт.** Если margin-ratio достиг жёсткого порога, приоритет —
   пополнение USDT/USDC на соответствующей бирже.
3. **Снизить экспозицию.** Закрыть часть позиций вручную или через reduce-only
   заявки (они разрешены router'ом даже при критике) до возвращения в `WARN/OK`.
4. **Мониторить окна.** Guard снимает HOLD и троттлинг автоматически после двух
   стабильных окон `OK`; вручную дергать resume не требуется, если причина
   осталась `ACCOUNT_HEALTH`. Проверяйте `/api/ui/system_status` и бейдж на
   дашборде, чтобы убедиться в возвращении к `OK`.

## Операционные сценарии

### Включить автоторговлю

1. Проверьте, что система в HOLD и `SAFE_MODE=false` (бейджи `/ui/dashboard`, `GET /api/ui/status`). 【F:app/services/runtime.py†L713-L733】【F:app/services/runtime_badges.py†L41-L81】
2. Первый оператор выполняет `POST /api/ui/resume-request` с причиной и именем; сервис возвращает `request_id`. 【F:app/routers/ui.py†L1202-L1235】
3. Второй оператор подтверждает `POST /api/ui/resume-confirm` с `APPROVE_TOKEN`, `actor` и `request_id`. 【F:app/routers/ui.py†L1295-L1335】
4. После двух approvals выполните `POST /api/ui/resume`; endpoint проверит, что HOLD снят, и вызовет `resume_loop`, переключив режим в `RUN`. 【F:app/routers/ui.py†L1338-L1353】【F:app/services/loop.py†L83-L199】

### Выполнить partial-hedge

1. Получите план через `GET /api/ui/hedge/plan` — ответ содержит ордера и агрегаты. 【F:app/routers/ui_partial_hedge.py†L20-L34】
2. Убедитесь, что HOLD выключен, `safe_mode=false`, и собрано ≥2 approvals; иначе раннер вернёт `blocked`. 【F:app/services/partial_hedge_runner.py†L285-L300】
3. Запустите `POST /api/ui/hedge/execute` с `{ "confirm": true }` и operator-токеном. Ответ содержит результат исполнения и актуальный статус раннера. 【F:app/routers/ui_partial_hedge.py†L37-L50】

### Снять снапшот

1. При активном `INCIDENT_MODE_ENABLED` вызовите `POST /api/ui/incident/snapshot` c необязательным `note`. Endpoint проверит токен, сохранит `data/snapshots/incident_*.json` и вернёт путь. 【F:app/routers/ui_incident.py†L48-L66】
2. Сохраните путь в журнале смены.

### Откатиться на снапшот

1. Создайте заявку: `POST /api/ui/incident/rollback` с `confirm=false`, указав `path`. 【F:app/routers/ui_incident.py†L68-L90】
2. Найдите `request_id` в `/api/ui/approvals` и убедитесь, что action = `incident_rollback`.
3. Повторите `POST /api/ui/incident/rollback` с `confirm=true`, `request_id` и тем же `path` — обработчик сверит путь и вернёт `status="applied"` с данными снапшота. 【F:app/routers/ui_incident.py†L90-L121】
4. Перед переходом в RUN выполните процедуру двухоператорного resume. 【F:app/routers/ui.py†L1202-L1353】

### Запустить сверку

1. Запустите одиночный прогон:

   ```bash
   python - <<'PY'
   from app.services.recon_runner import get_runner
   import asyncio

   asyncio.run(get_runner().run_once())
   PY
   ```

   Метод `run_once` обновит snapshot и вернёт `diffs`. 【F:app/services/recon_runner.py†L83-L127】
2. Проверяйте результаты через `GET /api/ui/recon/status` или виджет «Reconciliation» на дашборде (`diff_count`, `issues`, `auto_hold`). 【F:app/routers/ui_recon.py†L10-L21】【F:app/services/operator_dashboard.py†L539-L574】

## Chaos-инжекторы

Для проверки отказоустойчивости без модификации кода используется блок хаос-флагов. При `FEATURE_CHAOS=1` и заданном `CHAOS_PROFILE` (см. `configs/fault_profiles.yaml`) активируются имитации:

- `CHAOS_WS_DROP_P` — вероятность (0..1) отбросить обновление в `MarketDataAggregator.update_from_ws`, что позволяет увидеть деградацию стримов. 【F:app/services/marketdata.py†L39-L53】
- `CHAOS_REST_TIMEOUT_P` — вероятность выбросить `RuntimeError` перед REST-запросом в биржевых клиентах (включая SAFE_MODE-адаптеры). 【F:exchanges/binance_futures.py†L86-L130】【F:app/exchanges/binance_um.py†L73-L116】
- `CHAOS_ORDER_DELAY_MS` — искусственная задержка (мс) перед `place_order`/`cancel_all`, полезно для воспроизведения «залипаний» matching engine. 【F:app/utils/chaos.py†L86-L124】

Параметры читаются из `.env`, раздела `chaos` в YAML-конфиге (`AppConfig.chaos`) или из профилей. Текущий профиль и итоговые величины можно посмотреть через `GET /api/ui/chaos`, а на `/ui/dashboard` выводится строка «Chaos profile: ...». Пока `FEATURE_CHAOS=0`, значения игнорируются и прод-окружение/CI работают штатно. 【F:app/config/schema.py†L117-L141】【F:app/services/runtime.py†L594-L630】【F:app/routers/ui.py†L162-L176】

### Acceptance-check перед релизом

Команда `make acceptance` (=`pytest -m acceptance -q`) выполняет минимальный прогон: проверяет, что `/healthz` и `/live-readiness` отвечают `200` без хаоса, а профиль `CHAOS_PROFILE=mild` оставляет бота в HOLD/safe_mode и не переводит вотчдог в деградацию. Убедитесь, что отчёт зелёный перед выкладкой.

## Offline backtest отчёты

Для оффлайн-проверки качества исполнения используйте мини-бектест:

```bash
python -m app.tools.replay_runner --file data/replay/sample.jsonl
```

Скрипт агрегирует `attempts`, `fills`, hit-ratio, gross/net PnL и средний slippage, затем сохраняет отчёт в `data/reports/backtest_YYYYMMDD_HHMM.{json,csv}` (каталог меняется через `--outdir`). 【F:app/tools/replay_runner.py†L1-L221】

Последний отчёт доступен по API `GET /api/ui/backtest/last` и отображается блоком «Last Backtest Summary» на `/ui/dashboard`. 【F:app/routers/ui.py†L140-L167】【F:app/services/operator_dashboard.py†L379-L463】

## Funding router и частичные хеджи

- `FEATURE_FUNDING_ROUTER=1` включает расчёт эффективной комиссии в планировщике арбитража: для каждой стороны сделки учитывается
  `taker_fee ± funding*horizon` на горизонте следующего окна. Данные берутся через `get_funding_info`/`get_fees` деривативных клиентов.
- `FEATURE_REBALANCER=1` запускает демон `PartialHedgeRebalancer`, который дозаявляет недостающую ногу в частичных позициях. Интервалы и лимиты
  управляются переменными `REBALANCER_INTERVAL_SEC`, `REBALANCER_RETRY_DELAY_SEC`, `REBALANCER_BATCH_NOTIONAL_USD`, `REBALANCER_MAX_RETRY`.
- Каждая попытка фиксируется в `positions_store` и `ledger`, UI отображает лейбл `PARTIAL/REBALANCING`, количество попыток и последнее сообщение об ошибке
  в `/api/ui/status` и на `/ui/dashboard`.
- Для остаточной дельты запущен планировщик `app/services/partial_hedge_runner.PartialHedgeRunner`. Флаги `HEDGE_ENABLED`, `HEDGE_INTERVAL_SEC`, `HEDGE_MIN_NOTIONAL_USDT`, `HEDGE_MAX_NOTIONAL_USDT_PER_ORDER`, `HEDGE_DRY_RUN` задают периодичность и лимиты. В режиме dry-run (`HEDGE_DRY_RUN=1`) ордера не отправляются — доступен только просмотр плана.
- План частичного хеджа можно запросить через `GET /api/ui/hedge/plan`; ответ содержит список ордеров, агрегаты по символам и текущее состояние раннера. На `/ui/dashboard` отображается виджет «Partial Hedge» с таблицей заявок и кнопкой Execute.
- Ручное исполнение делается `POST /api/ui/hedge/execute` с `{ "confirm": true }` и требует соблюдения Two-Man Rule — если включено двухоператорное подтверждение, нужно минимум две активные approvals. Неудачи вида `insufficient balance`/`price out of bounds` накапливаются; при трёх подряд срабатывает `engage_safety_hold("partial_hedge:auto_hold")`.

## TCA Preview и подбор маршрута

- Флаг `FEATURE_TCA_ROUTER=1` включает расширенную модель стоимости (`app/tca/cost_model.py`) для выбора площадок: maker/taker комиссии (с VIP-ребейтами), выбранный `TierTable.pick_tier()` и линейно-квадратичный `ImpactModel` агрегируются в `effective_cost()` и используются в `app/routing/funding_router.choose_best_pair`.
- Секция `tca` в `configs/*.yaml` задаёт дефолтный горизонт (`horizon_min`), коэффициент модели impact (`impact.k`) и таблицу `tiers[venue]` (массива `{tier, maker_bps, taker_bps, rebate_bps, notional_from}`). Передавайте накопленный оборот за 30 дней через `rolling30d=` в API/дэшборд, чтобы выбрать нужный VIP-уровень. Если параметр не задан — используется базовый tier (обычно VIP0).
- Для оперативной проверки используйте `GET /api/ui/tca/preview?pair=BTCUSDT&qty=1&rolling30d=500000&book_liq=1200000`. В ответе — список направлений (`long venue → short venue`) с bps/USDT, выбранный режим исполнения, рассчитанный tier, impact и разложение на комиссии/funding/impact.
- На `/ui/dashboard` появляется блок «TCA Preview» (при активном флаге). Таблица показывает оба направления, выделяет лучшее и выводит tier/impact по каждой ноге. `impact_bps`/`impact_usdt` отражают слиппедж на выбранном объёме относительно доступной ликвидности (`book_liq`). Горизонт по умолчанию берётся из `tca.horizon_min`, но может быть переопределён запросом — увеличивайте его для сценариев удержания позиций через несколько funding-окон.
- Флаг `FEATURE_SMART_ROUTER=1` включает скоринг `app/router/smart_router.SmartRouter` при выборе биржи для одиночного ордера. Стоимость складывается из `effective_cost()` (комиссии + funding + impact), дополнительной поправки `impact_bps*price` и штрафа за задержку (целевое значение берётся из `derivatives.arbitrage.max_latency_ms`, коэффициент — из переменной окружения `SMART_ROUTER_LATENCY_BPS_PER_MS`, по умолчанию 0.01 bps за мс). Превью доступно через `GET /api/ui/router/preview?symbol=BTCUSDT&side=buy&qty=1`, а на `/ui/dashboard` появляется мини-таблица «Router preview» с подсвеченным лучшим venue и покомпонентным breakdown.

## Close-all: семантика и идемпотентность

Эндпоинт `/api/ui/trades/close-all` и Telegram-команда `/close_all` используют
один поток `close_all_trades`:

* позиции подтягиваются из SQLite-леджера, нормализуются в
  `TradeInstruction` (символ, биржа, side, объём, notional). 【F:app/services/trades.py†L1-L103】
* каждый запрос получает отпечаток (`fingerprint`) из venue/symbol/side/qty и
  сохраняется в `_close_all_tracker`; повторный вызов с теми же данными возвращает
  пустой ответ и не размещает ордера. 【F:app/services/trades.py†L112-L164】【F:tests/test_ui_close_all_idempotent.py†L16-L102】
* в `dry_run=true` close-all симулирует результат, что позволяет проверить
  отчётность без реальных сделок. В рабочем режиме создаются рыночные ордера с
  `reduce_only=true` и idempotency-ключом `close_all:<sha256>`.

## Restart-resume под `FEATURE_JOURNAL`

После планового/аварийного рестарта инстанс автоматически подтягивает
незакрытые позиции и незавершённые заявки из SQLite-леджера, записывает событие
`restart.resume` и обновляет runtime-состояние. Оператору нужно подтвердить, что
журнал инициализировался корректно:

1. Проверьте `GET /healthz` — поля `journal_ok` и `resume_ok` должны быть `true`.
   При `journal_ok=false` журнал не принимает записи; при `resume_ok=false` runtime
   не получил снимок и нужно посмотреть логи старта.
2. Посмотрите последнюю запись `restart.resume` в таблице `order_journal`
   (через `sqlite3 data/ledger.db 'SELECT * FROM order_journal ORDER BY id DESC LIMIT 5;'`).
   В payload отображаются ордера, отмеченные как `RESUMED`.
3. При ручных отменах используйте `correlation_id` (например, UUID) во всех
   вызовах `/api/ui/cancel_all`. Повторный запрос с тем же ID вернёт сохранённый
   результат и создаст событие `cancel_all.duplicate` — можно безопасно
   перезапускать kill switch и Telegram-команды без риска задвоить операции.

## Отчётность и экспорты

### `/api/ui/ops_report` (JSON)

Основной снимок для смены. В payload входят агрегаты runtime, PnL и отчётность по
стратегиям, включая блок `pnl_attribution` с разложением по стратегиям и
биржам:

```json
{
  "open_trades_count": 1,
  "max_open_trades_limit": 5,
  "runtime": {"mode": "HOLD", "safety": {"hold_reason": "maintenance"}},
  "autopilot": {"last_decision": "ready"},
  "pnl": {"unrealized_pnl_usdt": 42.0},
  "pnl_attribution": {
    "totals": {"realized": 42.0, "unrealized": 3.0, "fees": 0.5, "rebates": 0.1, "funding": 1.0, "net": 45.6}
  },
  "budgets": [{"strategy": "alpha", "budget_usdt": 1000.0, "used_usdt": 250.0}],
  "watchdog": {"overall_ok": false, "degraded_reasons": {"binance": "timeout"}},
  "daily_loss_cap": {"max_daily_loss_usdt": 200.0, "losses_usdt": 80.0, "breached": false},
  "strategy_status": {"alpha": {"budget_blocked": true}}
}
```

Эти поля проверяются интеграционными тестами и отображаются на dashboard.
【F:tests/test_ops_report_endpoint.py†L30-L120】

### `/api/ui/ops_report.csv`

CSV-вариант опирается на те же данные и для каждой стратегии добавляет строку с
пустыми колонками атрибуции, за которыми следуют записи `pnl_attribution`. Заголовок и
пример:

```
timestamp,open_trades_count,max_open_trades_limit,daily_loss_status,watchdog_status,auto_trade,strategy,budget_usdt,used_usdt,remaining_usdt,attrib_scope,attrib_name,attrib_realized,attrib_unrealized,attrib_fees,attrib_rebates,attrib_funding,attrib_net
2024-01-01T00:00:00+00:00,1,5,OK,DEGRADED,OFF,alpha,1000.0,250.0,750.0,,,,,,,
2024-01-01T00:00:00+00:00,1,5,OK,DEGRADED,OFF,,,,,totals,totals,45.6,3.0,0.5,0.1,1.0,49.2
```

Файл пригоден для Excel/Sheets и поставляется с контент-тайпом `text/csv`.
【F:tests/test_ops_report_endpoint.py†L123-L158】【F:tests/test_ops_report_parity_csv.py†L40-L58】

### `/api/ui/strategy_pnl`

JSON-отчёт по стратегиям содержит `realized_today`, `realized_7d`,
`max_drawdown_7d` и флаг `simulated_excluded`. По умолчанию сделки в
`DRY_RUN_MODE` исключаются, что отражает поле `simulated_excluded=true`.
```json
{
  "simulated_excluded": true,
  "strategies": [
    {"name": "beta", "realized_today": -10.0, "realized_7d": -10.0, "max_drawdown_7d": 10.0},
    {"name": "alpha", "realized_today": 100.0, "realized_7d": 50.0, "max_drawdown_7d": 50.0}
  ]
}
```

Для ежедневных срезов используйте `app.pnl.reporting.make_daily_report`: функция
принимает готовый `PnLLedger` и возвращает словарь с аггрегатами по каждому
символу и итоговыми суммами (`realized`, `fees`, `funding`, `net`). Скрипт или
cron-джоба может собирать ledger через `build_ledger_from_history(...)` и писать
JSON в архив без прямого I/O в боевом коде.

### `/api/ui/pnl_attrib`

Отдельный срез PnL attribution. Требует тот же bearer-токен, что и остальные
`/api/ui` ручки, и возвращает агрегаты по стратегиям и биржам, разбитые на
компоненты (`realized`, `unrealized`, `fees`, `rebates`, `funding`, `net`).
Комиссии и ребейты вычисляются через текущие TCA tiers (см. конфиг `tca.tiers`),
а funding-платежи подтягиваются из событий ledger/адаптеров. Все слагаемые
суммируются на единой базе после фильтрации DRY-RUN: `net = realized +
unrealized + fees + rebates + funding`, при этом комиссии публикуются со знаком
«минус», а ребейты — со знаком «плюс». Поле `meta` подсказывает, исключены ли
симуляционные сделки, и сколько сырых событий попало в срез. Используйте этот
эндпоинт, чтобы сверять суммарные комиссии/фандинг с биржевыми отчётами.
Измените `EXCLUDE_DRY_RUN_FROM_PNL=false`, чтобы видеть симулированные сделки в
агрегатах. Текущее состояние флага возвращается отдельно в поле
`simulated_excluded`, которое также попадает в `/ui/dashboard`, `/api/ui/ops_report`
и CSV-экспорт (`attrib_simulated_excluded`). Если суммы из runtime расходятся с
`StrategyPnlTracker`, сервис добавит строку `tracker-adjustment`; она считается на
том же отфильтрованном наборе сделок, поэтому dry-run PnL не вычитается повторно и
виден только при отключённом флаге. 【F:tests/test_pnl_attrib_endpoint.py†L1-L41】

### `/api/ui/open-trades.csv`

Экспорт открытых позиций — удобная шпаргалка для ручного закрытия на бирже.
Формат жёстко задан тестами: заголовок `trade_id,pair,side,size,entry_price,unrealized_pnl,opened_ts`
и строки для каждой ноги кросс-биржевой позиции. 【F:tests/test_ui_open_trades_csv.py†L8-L35】

## Incident snapshots

Режим ручного отката runtime по снапшотам закрыт флагом окружения
`INCIDENT_MODE_ENABLED` (по умолчанию `false`). Перед плановыми тестами или в
инцидентных сценариях SRE включает флаг и подтверждает доступность API.

### Когда снимать снапшот

* Перед экспериментами с control state, бюджетами или risk caps в live.
* Перед запуском временных mitigation-сценариев, которые меняют watchdog или
  лимиты.
* После стабилизации инцидента, чтобы зафиксировать «известно хорошее» состояние.

Чтобы создать снапшот, выполните `POST /api/ui/incident/snapshot` с
необязательным телом `{"note": "короткое описание"}`. Эндпоинт требует
operator-токен и возвращает путь вида `data/snapshots/incident_*.json`. Директория
хранит максимум 50 файлов; старые удаляются по LRU.

### Как откатывать состояние

1. Запросить откат: `POST /api/ui/incident/rollback` с телом
   `{"path": "...", "confirm": false}`. Создаётся заявка в approvals (`action =
   incident_rollback`).
2. Получить второй фактор через `/api/ui/approvals` и запомнить `request_id`.
3. Применить откат: `POST /api/ui/incident/rollback` с телом
   `{"path": "...", "confirm": true, "request_id": "..."}`. Ответ содержит
   `status="applied"` и сериализованный снапшот.

Откат восстанавливает control state (включая two-man rule, dry-run и auto loop),
runtime risk limits, strategy budgets и снимок watchdog. Счётчик открытых сделок
в JSON — справочная метрика для операторов.

## Readiness и метрики

* `GET /live/readiness` — основной агрегатор готовности. Возвращает `{"status": "GREEN|YELLOW|RED", "reasons": [...], "details": {...}}`, где причины включают `pretrade_throttled`, `md_staleness`, `watchdog_down`, `router_not_ready` и др. 【F:app/api/ui/readiness.py†L1-L12】【F:app/readiness/aggregator.py†L26-L205】
* `/live-readiness` оставлен для обратной совместимости (watchdog/daily loss). Используйте новый эндпоинт для UI и алертов, он же экспортирует метрики `readiness_status{status="..."}` и `readiness_reason_total{reason="..."}`. 【F:app/routers/live.py†L10-L14】【F:app/readiness/aggregator.py†L33-L82】
* На старте процесс ждёт `status=GREEN`, если `WAIT_FOR_LIVE_READINESS_ON_START=true`; таймаут задаётся `readiness.startup_timeout_sec` (YAML). При таймауте бот остаётся в HOLD. 【F:app/main.py†L92-L123】【F:configs/config.live.yaml†L57-L64】
* В `/ui/status` отображается бейдж Readiness; наведите, чтобы увидеть активные причины. 【F:app/templates/status.html†L462-L575】
* Prometheus-метрики публикуются на `/metrics` без дополнительных флагов, в том
  числе бизнес-гистограммы (`propbot_order_cycle_ms`, `propbot_watchdog_ok`,
  `propbot_daily_loss_breached`, `propbot_auto_trade`). 【F:app/server_ws.py†L37-L44】【F:README.md†L76-L87】
* Для SRE-дашборда проверяйте также `/api/ui/status/overview` и `/api/ui/status/components`
  — они дублируют бейджи и счётчики, которые использует runbook.

## Idempotency & Restart Recovery

Журнал заявок (`order_intents`) хранит все попытки в таблице SQLite
`data/orders.db`. На каждую заявку UI/бот передаёт `request_id`
(если не указан — OrderRouter сгенерирует ULID-подобный `rid-*`).
Перед сетевым вызовом в таблице фиксируется `state=PENDING → SENT`,
а после ACK — `state=ACKED` с `broker_order_id`. Повторная отправка с тем
же `request_id` вернёт прежний `broker_order_id` без повторной заявки в
биржу. Состояния цепочки replacement (`replaced_by`) можно посмотреть через
`GET /api/ui/intents/<intent_id>` — ответ содержит связанный `request_id`,
аккаунт и текущий `state`. 【F:app/router/order_router.py†L27-L235】【F:app/routers/ui_exec.py†L9-L75】

Метрики Prometheus:

* `order_idempotency_hit_total{operation=submit|cancel|replace}` — счётчик
  подавленных повторов. Значение >5% за 5 минут стоит отследить с алертом.
* `order_intent_total{state=...}` — накопительный счётчик финальных
  состояний (ACKED/REJECTED/etc.).
* `open_order_intents` — gauge открытых intent'ов.
* `order_replace_chain_length{intent_id=...}` — gauge цепочки replace;
  скачок указывает на длинную замену в ручном режиме.
* `order_submit_latency_ms` — latency-гистограмма вызова брокера.

При рестарте вызывайте `OrderRouter.recover_inflight()` — метод ищет все
`state in (PENDING,SENT)` intents, подтягивает `broker_order_id` через
`get_order_by_client_id` и добивает состояние до `ACKED`. Оркестратор
`app/services/reconciler_orchestrator.py` делает это автоматически при запуске
runtime (если safe_mode выключен). 【F:app/router/order_router.py†L237-L276】【F:app/services/reconciler_orchestrator.py†L1-L15】
