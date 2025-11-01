# Operator Runbook

Единый плейбук для операторов PropBot (paper/testnet/live). Документ описывает
сигналы готовности, автоматические защиты и публичные отчёты, на которые
опирается смена во время дежурства.

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

* `GET /live-readiness` возвращает `{"ok": true|false, "reasons": [...]}` и
  меняет статус на `503`, если бот не готов к live (причины:
  `watchdog:auto_hold`, `daily_loss:breach`). 【F:app/services/live_readiness.py†L5-L18】【F:app/routers/live.py†L10-L14】
* Prometheus-метрики публикуются на `/metrics` без дополнительных флагов, в том
  числе бизнес-гистограммы (`propbot_order_cycle_ms`, `propbot_watchdog_ok`,
  `propbot_daily_loss_breached`, `propbot_auto_trade`). 【F:app/server_ws.py†L37-L44】【F:README.md†L76-L87】
* Для SRE-дашборда проверяйте также `/api/ui/status/overview` и `/api/ui/status/components`
  — они дублируют бейджи и счётчики, которые использует runbook.
