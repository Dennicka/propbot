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

Основной снимок для смены. В payload входят агрегаты runtime и отчётность по
стратегиям:

```json
{
  "open_trades_count": 1,
  "max_open_trades_limit": 5,
  "runtime": {"mode": "HOLD", "safety": {"hold_reason": "maintenance"}},
  "autopilot": {"last_decision": "ready"},
  "pnl": {"unrealized_pnl_usdt": 42.0},
  "budgets": [{"strategy": "alpha", "budget_usdt": 1000.0, "used_usdt": 250.0}],
  "watchdog": {"overall_ok": false, "degraded_reasons": {"binance": "timeout"}},
  "daily_loss_cap": {"max_daily_loss_usdt": 200.0, "losses_usdt": 80.0, "breached": false},
  "strategy_status": {"alpha": {"budget_blocked": true}}
}
```

Эти поля проверяются интеграционными тестами и отображаются на dashboard.
【F:tests/test_ops_report_endpoint.py†L30-L120】

### `/api/ui/ops_report.csv`

CSV-вариант опирается на те же данные и добавляет табличную строку по каждой
стратегии. Заголовок и пример первой строки:

```
timestamp,open_trades_count,max_open_trades_limit,daily_loss_status,watchdog_status,auto_trade,strategy,budget_usdt,used_usdt,remaining_usdt
2024-01-01T00:00:00+00:00,1,5,OK,DEGRADED,OFF,alpha,1000.0,250.0,750.0
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
Измените `EXCLUDE_DRY_RUN_FROM_PNL=false`, чтобы видеть симулированные сделки в
агрегатах. 【F:tests/test_strategy_pnl_endpoint.py†L9-L49】

### `/api/ui/open-trades.csv`

Экспорт открытых позиций — удобная шпаргалка для ручного закрытия на бирже.
Формат жёстко задан тестами: заголовок `trade_id,pair,side,size,entry_price,unrealized_pnl,opened_ts`
и строки для каждой ноги кросс-биржевой позиции. 【F:tests/test_ui_open_trades_csv.py†L8-L35】

## Readiness и метрики

* `GET /live-readiness` возвращает `{"ok": true|false, "reasons": [...]}` и
  меняет статус на `503`, если бот не готов к live (причины:
  `watchdog:auto_hold`, `daily_loss:breach`). 【F:app/services/live_readiness.py†L5-L18】【F:app/routers/live.py†L10-L14】
* Prometheus-метрики публикуются на `/metrics` без дополнительных флагов, в том
  числе бизнес-гистограммы (`propbot_order_cycle_ms`, `propbot_watchdog_ok`,
  `propbot_daily_loss_breached`, `propbot_auto_trade`). 【F:app/server_ws.py†L37-L44】【F:README.md†L76-L87】
* Для SRE-дашборда проверяйте также `/api/ui/status/overview` и `/api/ui/status/components`
  — они дублируют бейджи и счётчики, которые использует runbook.
