# Operator Runbook (Prod/Testnet)

Рабочая памятка для операторов PropBot без доступа к коду. Все шаги предполагают,
что у вас есть сеть до инстанса и API-токен (если включена авторизация).

## Coverage vs spec_archive

- **Что уже боевое**: текущий бот поддерживает авто-хедж, кросс-биржевой
  арбитраж, HOLD/SAFE_MODE, DRY_RUN_MODE, двухоператорное возобновление,
  персистентные журналы (positions/hedge_log/runtime_state) и операторскую
  панель `/ui/dashboard`. Эти возможности описаны в блоке `[ok]` GAP-отчёта и
  уже используются в production со строгим ручным контролем. 【F:docs/GAP_REPORT.md†L3-L25】
- **Что ещё TODO**: требования из spec_archive по VaR, многостратегийному
  оркестратору, MSR/RPI, автопилоту, RBAC/комплаенсу, защищённым секретам и
  инфраструктурным гарантиям пока не реализованы. При планировании круглосуточной
  торговли без постоянного присутствия оператора ориентируйтесь на раздел
  `[missing]` GAP-отчёта как на чек-лист незакрытых задач. 【F:docs/GAP_REPORT.md†L27-L126】

## Roles & dashboard

- **viewer**: имеет доступ к `/ui/dashboard` после успешной аутентификации и
  видит всю телеметрию — статус демонов (`healthz`), открытую и частичную
  экспозицию, runtime flags (SAFE_MODE/HOLD/DRY_RUN/autopilot и причину HOLD),
  pending approvals, build_version, последние алерты. Управляющие формы HOLD /
  RESUME-request / KILL / raise-limits недоступны: элементы отображаются как
  read-only и действия инициировать нельзя.
- **operator**: помимо чтения статуса может инициировать HOLD,
  RESUME-request, KILL, запрос повышения лимитов и подтверждать второй шаг
  (approve) в двухоператорном флоу. Каждое такое действие попадает в
  `audit_log` и отображается в блоках Recent Ops / Audit.

## Старт в продакшене

### Production bring-up checklist

1. Подготовьте постоянный каталог `/opt/propbot/data` (или другой путь),
   примонтированный в контейнер как `/app/data`. В `data/` находятся
   `runtime_state.json`, `hedge_positions.json`, `pnl_history.json`, `hedge_log.json`,
   `ops_alerts.json` и другие журналы. Потеря каталога = потеря истории, поэтому
   храните его на надёжном диске и включите бэкап.
2. Создайте файл окружения из шаблона: `cp .env.prod.example .env.prod`. Затем
   заполните все секции, удалив плейсхолдеры `TODO`/`change-me` и пустые значения:
   - Биржевые ключи `BINANCE_*`, `OKX_*` (используйте отдельные субаккаунты и
     IP white-list).
   - `REPO`/`TAG` для образа в GHCR, `API_TOKEN`, `APPROVE_TOKEN`, `AUTH_ENABLED=true`.
   - Лимиты риска `MAX_POSITION_USDT`, `MAX_DAILY_LOSS_USDT`, runaway guard
     (`MAX_ORDERS_PER_MIN`, `MAX_CANCELS_PER_MIN`), настройки Telegram.
   - `SAFE_MODE=true`, `DRY_RUN_ONLY=true`, `DRY_RUN_MODE=true` и режим HOLD на
     старте оставляйте включёнными до прохождения двухшагового
     `resume-request`/`resume-confirm`.
   - Пути хранения (`RUNTIME_STATE_PATH`, `POSITIONS_STORE_PATH`,
     `PNL_HISTORY_PATH`, `HEDGE_LOG_PATH`, `OPS_ALERTS_FILE`) указывайте внутри
     примонтированного каталога `./data/`.
3. Запустите контейнер: `docker compose -f docker-compose.prod.yml --env-file
   .env.prod up -d`. Startup validation немедленно остановит сервис, если
   обязательные токены или пути не заданы.
4. Следите за логами: `docker compose logs -f propbot_app_prod`. Ожидаемая
   строка — `PropBot starting with build_version=...`. Любые сообщения
   `[FATAL CONFIG] ...` означают, что нужно поправить `.env.prod` и перезапустить
   `docker compose up -d`.
5. Проверяйте healthcheck через `docker inspect --format '{{json .State.Health}}'
   propbot_app_prod | jq` — статус `healthy` означает, что `/healthz` отдаёт
   `{ "ok": true }`.
6. После старта убедитесь, что защита включена:
   ```bash
   curl -sfS -H "Authorization: Bearer $API_TOKEN" \
     http://<host>:8000/api/ui/status/overview | jq '.flags'
   ```
   Значения `safe_mode`, `hold_active` и `dry_run_mode` должны быть `true`.
   Любое отклонение рассматривайте как инцидент и не отключайте SAFE_MODE, пока
   оба оператора не пройдут двухшаговый флоу.
7. Перед переходом в live пройдите процедуру:
   `resume-request` → `resume-confirm` (со вторым оператором и `APPROVE_TOKEN`) →
   `resume`. Только после этого вручную выключайте `SAFE_MODE`/`DRY_RUN_MODE` и
   переводите режим в `RUN`.
8. Не снимайте лимиты по плечу/ноционалу — runaway guard и риск-блокировки
   используют их для защиты.

## CapitalManager snapshot

- `GET /api/ui/capital` (тот же bearer-токен, что и для прочих UI-ручек) показывает
  текущий снимок CapitalManager: `total_capital_usdt`, `per_strategy_limits` и
  `current_usage`.
- `per_strategy_limits` — заявленные потолки notional'а на стратегию, например
  `{ "cross_exchange_arb": { "max_notional": 50_000 } }`.
- `current_usage` — фактическая загрузка (`open_notional`) по каждой стратегии.
- В ответ включён блок `headroom`: оставшийся запас до лимита
  (`max_notional - open_notional`). Если лимит не задан, значение `null`.
- Это отчёт и планировщик лимитов: CapitalManager **не** вмешивается в исполнение
  ордеров и не блокирует сделки автоматически. Используйте метрики для контроля и
  ручных решений об изменении лимитов.

## Startup validation / go-live safety

- Контейнер теперь выполняет жёсткий preflight: при старте `app/main.py`
  вызывает `startup_validation.validate_startup()`. Если конфигурация
  небезопасна (непрописанные токены, отсутствующие пути `data/`, лимиты со
  значением `0`, попытка live-старта без HOLD), процесс завершится c ошибкой и
  контейнер останется остановленным.
- Перед тем как выключать HOLD и `DRY_RUN_MODE`, убедитесь, что выполнен
  чек-лист:
  - биржевые сети доступны (ping до REST/WebSocket Binance и OKX успешен);
  - `APPROVE_TOKEN` заполнен и хранится отдельно от `API_TOKEN`;
  - пути `RUNTIME_STATE_PATH`, `POSITIONS_STORE_PATH`, `HEDGE_LOG_PATH`,
    `OPS_ALERTS_FILE` указывают на том с правом записи;
  - лимиты риска (`MAX_OPEN_POSITIONS`, `MAX_NOTIONAL_PER_POSITION_USDT`,
    `MAX_TOTAL_NOTIONAL_USDT`, `MAX_LEVERAGE`) проставлены в положительные
    значения;
  - `SAFE_MODE=true`, HOLD активен, `DRY_RUN_MODE=true` для проверки без
    реальных ордеров.
- Комбинация `DRY_RUN_MODE=true` + HOLD оставляет стратегию в безопасном режиме
  обкатки: ордера не отправляются, но мониторинг и отчёты работают. Используйте
  её для тестов и после перезагрузок.
- Реальную торговлю можно продолжить только вручную после двухшагового флоу
  `/api/ui/resume-request` → `/api/ui/resume-confirm` (c `APPROVE_TOKEN`) →
  `/api/ui/resume`. Любая попытка стартовать контейнер сразу в live режиме
  блокируется валидацией.

## Going live

После старта `docker compose -f docker-compose.prod.yml --env-file .env.prod up`
выполните обязательные шаги перед переходом в реальную торговлю:

1. Проверка здоровья процесса и фоновых демонов:
   ```bash
   curl -sf http://localhost:8000/healthz | jq
   ```
   Ожидаемый ответ — `{ "ok": true }`.
2. Снимите сводку по флагам безопасности:
   ```bash
   curl -sfS -H "Authorization: Bearer $API_TOKEN" \
     http://localhost:8000/api/ui/status/overview | jq '.flags'
   ```
3. Убедитесь, что экспозиция и ноги хеджа соответствуют ожиданиям:
   ```bash
   curl -sfS -H "Authorization: Bearer $API_TOKEN" \
     http://localhost:8000/api/ui/positions | jq '.positions'
   ```
4. Проверьте, что `flags.hold_active=true`, `flags.safe_mode=true` и
   `flags.dry_run_mode=true`. Первый запуск всегда выполняйте с
   `DRY_RUN_MODE=true` и HOLD активным.
5. Для выхода в бой задействуйте двухшаговый флоу `resume-request` →
   `resume-confirm` (с `APPROVE_TOKEN`) → `resume`. Без подтверждения второго
   оператора HOLD остаётся активен.
6. Никогда не выключайте HOLD и `DRY_RUN_MODE` одновременно: сначала снимайте
   HOLD через подтверждённый `resume-confirm`, затем вручную переводите
   `DRY_RUN_MODE` и SAFE_MODE в боевой режим.

## Autopilot mode

- `AUTOPILOT_ENABLE=false` (по умолчанию) — после рестарта бот всегда остаётся в
  HOLD/SAFE_MODE и требует стандартного двухшагового флоу
  `/api/ui/resume-request` → `/api/ui/resume-confirm` с `APPROVE_TOKEN`, а затем
  ручного `/api/ui/resume` или команды из Telegram/CLI.
- `AUTOPILOT_ENABLE=true` — при старте бот проверяет существующие гардрейлы
  (runaway счётчики, состояние auto-hedge, доступность бирж, успешный preflight,
  отсутствие risk breaches). Если всё зелёное, он автоматически восстанавливает
  прежний SAFE_MODE, снимает HOLD и запускает цикл `resume_loop()`.
  Решение фиксируется в audit-журнале с инициатором `autopilot`, в Telegram
  прилетает сообщение `AUTOPILOT: resumed trading after restart (reason=...)`, а
  на `/ui/dashboard` появляется жёлтый блок «autopilot armed» с причиной.
- Если автопилот видит блокеры (runaway сработал, auto-hedge в ошибке, биржа не
  отвечает, конфиг невалиден), он остаётся в HOLD, пишет событие
  `autopilot_resume_refused` и шлёт тревогу `AUTOPILOT refused to arm` с
  расшифровкой причины.
- Включайте `AUTOPILOT_ENABLE` только на доверенных хостах — он обходится без
  живых операторов при рестартах, но все остальные гардрейлы и ручные HOLD остаются
  в силе.

Статус API и `/ui/dashboard` показывают `autopilot_status`,
`last_autopilot_action` и `last_autopilot_reason`, что позволяет быстро понять,
как именно бот вышел из HOLD.

Журнал `data/runtime_state.json` сохраняет причину HOLD и таймштамп
(`safety.hold_reason`, `safety.hold_since`, `safety.last_released_ts`), а также
время последней успешной хедж-операции (`auto_hedge.last_success_ts`).
Используйте этот файл (или соответствующий endpoint UI) для аудита и расследований.

## 24/7 monitoring / alert flow

- Оркестратор стратегий теперь рассылает операторские уведомления при критических
  решениях: если стратегия переведена в `decision=skip` из-за `hold_active`,
  `safe_mode` или лимитов риска (`risk_limit`), а также если она попала в
  `decision=cooldown` после неудачного запуска (`last_result=fail`).
- Эти события автоматически пишутся в ops/audit канал через `ops_alerts.json` и
  Telegram (если включён бот). Формат сообщения —
  `[orchestrator] strategy=<имя> decision=<skip|cooldown> reason=<причина> autopilot=<ON|OFF>`.
- Получив алерт, оператор должен открыть `/ui/dashboard`, посмотреть блок
  **Strategy Orchestrator** и причины блокировки, затем:
  - проверить, активен ли HOLD/SAFE_MODE или сработал `risk_limit`;
  - при необходимости инициировать двухшаговый `resume-request`/`resume-confirm`
    и снять HOLD либо поднять лимит только после одобрения напарника;
  - если стратегия в cooldown из-за ошибки, дождаться тайм-аута и убедиться, что
    причина устранена.
- `AUTOPILOT_ENABLE` может автоматически возобновить торговлю только когда все
  safety-gates зелёные. Если HOLD/SAFE_MODE или лимиты продолжают блокировать
  торговлю, автопилот оставит состояние как есть, а оператор получит алерт от
  оркестратора.
- Роль `viewer` видит статусы и сами уведомления на дэшборде, но не может
  выполнять `RESUME`/`HOLD`/`KILL`. Только `operator` инициирует действия, и
  каждое подтверждение проходит через двухэтапный approve с записью в аудит.

## Risk governor / auto-HOLD

- Risk governor срабатывает перед каждой петлёй и перед реальными ордерами:
  берёт snapshot портфеля, обновляет `safety.risk_snapshot` и сверяет метрики с
  лимитами из `.env`.
- HOLD включается автоматически при:
  - дневном убытке ниже `-MAX_DAILY_LOSS_USD`;
  - суммарной экспозиции выше `MAX_TOTAL_NOTIONAL_USD`
    (поддерживается и `MAX_TOTAL_NOTIONAL_USDT`);
  - нереализованном убытке глубже `MAX_UNREALIZED_LOSS_USD`;
  - clock skew > `CLOCK_SKEW_HOLD_THRESHOLD_MS`;
  - сообщении биржи о maintenance/read-only.
- В `DRY_RUN_MODE` симулированные сделки не попадают в риск-лимиты, но clock skew
  и maintenance всё равно ставят HOLD.
- Причина фиксируется в `runtime_state.json` и `/api/ui/status/overview`
  (`safety.hold_reason`, `safety.risk_snapshot`). Не снимайте HOLD, пока не
  устранена причина; затем используйте `resume-request` → `resume-confirm`.

### Auto-throttle / аварийный HOLD

- Risk-guard следит за жёсткими нарушениями и может сам поставить HOLD с
  причиной вида `AUTO_THROTTLE/...`:
  - фактическое превышение runaway лимита
    (`MAX_TOTAL_NOTIONAL_USDT`, `MAX_OPEN_POSITIONS`);
  - слишком много последовательных ошибок авто-хеджа
    (`auto_hedge.consecutive_failures` выше порога);
  - зависшие partial hedges (второй лег не выставился дольше порога);
  - live-торговля и серия отказов/банов биржи при размещении ордеров.
- Такой HOLD нельзя снять автоматически: чтобы продолжить торговлю, всегда
  используйте стандартный двухшаговый `resume-request` → `resume-confirm`.

### Edge Guard (adaptive entry filter)

- Перед выставлением новой ноги хеджа бот дополнительно вызывает
  `edge_guard.allowed_to_trade(symbol)`. Этот фильтр оценивает текущее состояние
  риска: активный HOLD/auto-throttle, зависшие partial hedges, качество последних
  исполнений (средний slippage и доля неуспехов), а также тренд unrealised PnL
  относительно текущей экспозиции.
- Если среда токсична — HOLD включён, partial по-прежнему не закрыты, средний
  slippage последних попыток выше допуска или unrealised PnL снижается пять
  снапшотов подряд при высокой загрузке по экспозиции — новые ноги не
  отправляются. Причина отказа логируется как ops/audit-инцидент.
- На дашборде `/ui/dashboard` в секции runtime/risk появляется строка «Edge guard
  status». Там видно, разрешены ли новые сделки и какая причина блокировки, чтобы
  оператор мог быстро принять решение (вытянуть partial, повысить лимит, оставить
  HOLD и т.д.).

### State Desync / Manual Reconciliation

- После сетевых сбоев или ручного вмешательства возможно расхождение между
  персистентными сторями (`positions_store`, partial hedges) и фактическими
  позициями/ордерами на бирже. Специальный reconciler сравнивает хранилища с
  live-снимком биржи и при расхождении:
  - на дашборде `/ui/dashboard` появляется блок **STATE DESYNC — manual
    intervention required** с числом outstanding несовпадений и подсказкой
    «resolve manually before resume»;
  - `edge_guard` и `risk_guard` возвращают reason `desync`, блокируя новые сделки
    до устранения проблемы;
  - Telegram-бот отвечает на команду `/reconcile` кратким списком текущих
    несоответствий, чтобы оператор увидел, что именно осталось на бирже.
- Операторская процедура: закрыть руками остаток на бирже (cancel/market close),
  затем очистить соответствующую запись в сторе (через операторский тул или
  правку `data/hedge_positions.json`). После ручного выравнивания повторно
  запустить `/reconcile` — при чистом состоянии блок исчезает, и можно идти по
  стандартному двухшаговому `resume`.
- Бот принципиально **не** делает автопочинок: ликвидация позиций и правка
  сторов остаются ручной задачей оператора, чтобы избежать ошибок в условиях
  неточных данных. Reconciler лишь обнаруживает десинхронизацию, фиксирует её в
  `data/reconciliation_alerts.json` и блокирует новые сделки до подтверждения,
  что состояние приведено в порядок.

## Ежедневный мониторинг

- `GET /healthz` — проверка живости.
- `GET /api/ui/status/overview` — сводка SAFE_MODE/HOLD, причина HOLD,
  `two_man_resume_required`, runaway guard, `auto_hedge.consecutive_failures`.
- `GET /api/ui/status/components` и `/api/ui/status/slo` — детализация
  алертов (нужен `API_TOKEN`).
- `GET /api/ui/alerts` — журнал HOLD, kill switch, runaway guard, auto-hedge,
  подтверждений RESUME.
- `GET /api/ui/positions` — активные ноги хеджа, экспозиция и unrealized PnL.
- HTML-панель `/ui/dashboard` (через bearer-токен) — сводка build версии,
  HOLD/SAFE_MODE/DRY_RUN, runaway guard, авто-хедж, живой риск, pending approvals
  и формы HOLD/RESUME/kill (через прокси `/ui/dashboard/*`, чтобы не ломать
  JSON-контракты API и двухшаговую защиту).
- Telegram-бот дублирует критичные события (HOLD, runaway guard, kill switch,
  auto-hedge, двухшаговый RESUME).

### Daily report / инвестор апдейт

- Каждую ночь (или вручную через cron) сохраняется агрегат `data/daily_reports.json` —
  сумма реализованного PnL по закрытым хеджам, средний unrealized PnL и экспозиция,
  средний slippage и количество HOLD/throttle за последние 24 часа.
- Эндпоинт `GET /api/ui/daily_report` (Bearer `API_TOKEN`) отдаёт последний снэпшот
  в удобном JSON. Подходит для экспорта инвестору или в ежедневный отчёт без
  копания в сырых логах.
- На `/ui/dashboard` появился read-only блок **Daily PnL / Ops summary** с теми же
  цифрами и таймштампом снимка.
- В Telegram-боте доступна команда `/daily`, которая выводит сводку тем же
  операторам, что уже авторизованы в чат.

### Forensics snapshot / audit export

- Для полной форензики нажмите «Generate snapshot» на `/ui/dashboard` — всплывающая
  подсказка покажет готовую curl-команду. Можно вызвать экспорт напрямую:
  ```bash
  curl -H "Authorization: Bearer $API_TOKEN" \
    https://<host>/api/ui/snapshot | jq
  ```
  Требуется включённый `AUTH_ENABLED` и валидный bearer-токен оператора.
- Ответ одновременно пишет файл `data/snapshots/<timestamp>.json` и возвращает
  тот же JSON. Внутри: текущее runtime состояние (режим, HOLD/safe/dry-run,
  лимиты, последние таймштампы), живые и `partial` позиции, очередь two-man
  approvals, последние execution stats (slippage), актуальные reconciliation
  alerts и свежий daily report.
- Используйте снапшоты для инвесторских апдейтов, ретроспектив и юридической
  фиксации инцидентов — экспорт показывает «что бот видел и делал» без SSH к
  контейнеру.

Следите за ростом `consecutive_failures`, runaway-счётчиков и повторными HOLD —
это сигналы к расследованию.

## Incident response

- Если приходит alert о частичном хедже, runaway-лимите или зависшем авто-хедже,
  немедленно откройте `/ui/dashboard`. Новый блок **Active Alerts / Recent Audit**
  показывает актуальные рисковые предупреждения и последние записи аудита.
- Проанализируйте детали в блоке и сопутствующие разделы (runaway guard,
  позиции, auto-hedge). Примите решение: оставить HOLD, инициировать
  `cancel-all`, принудительно закрыть экспозицию или перевести бота в DRY_RUN.
- Каждое действие фиксируйте в журнале: используйте существующие guarded
  эндпоинты HOLD/RESUME/kill, чтобы сохранить запись в аудите.
- Для отчётности выгрузите последние события через
  `GET /api/ui/audit/export` (требуется bearer-токен оператора). Сохраните JSON
  на защищённом хранилище и добавьте его к расследованию инцидента.

### Incident timeline & audit export

- На панели `/ui/dashboard` появился блок **Recent Ops / Incidents**. В нём
  отображаются последние заявки на HOLD/RESUME, авто-throttle, kill switch и
  изменения лимитов. Жёлтые бейджи = pending approvals, красные — автоматический
  HOLD (auto-throttle), зелёные — подтверждённые/применённые действия.
- Для полной выгрузки используйте `GET /api/ui/audit_log` (нужен bearer-токен).
  Эндпоинт возвращает хронологический JSON (последние записи сверху) из
  персистентных журналов `ops_alerts.json`, `ops_approvals.json` и
  `runtime_state.json`.
- Этот JSON — ваша сырая лента инцидента. При эскалации отправляйте его
  менеджменту, инвесторам и службе безопасности вместе с выводами расследования.

## Operator Dashboard (`/ui/dashboard`)

- Доступен только по bearer-токену оператора (`AUTH_ENABLED=true`,
  `Authorization: Bearer $API_TOKEN`). Внешний интернет доступ запрещён.
- Сводит в одном месте build версию, `hold_active` и причину HOLD, SAFE_MODE и
  `dry_run_mode`, runaway guard (лимиты и текущие счётчики), а также состояние
  авто-хеджа (включён ли демон, последний результат, таймштамп успеха,
  `consecutive_failures`).
- Показывает живые и симуляционные хеджи (`open`, `partial`, `simulated`):
  симуляции помечены `SIMULATED` серым, частичные и разбалансированные позиции
  получают красный бейдж `OUTSTANDING RISK`. Для каждой позиции отображаются обе
  ноги с venue/side, entry/mark ценой и текущим PnL. Внизу показан агрегированный
  риск по биржам и суммарный unrealised PnL.
- Runaway guard рядом со счётчиками подсвечивает `NEAR LIMIT`, если остался
  <20% до лимита, чтобы оператор заранее видел, что защита скоро сработает.
- Показывает фактические риск-лимиты (`MAX_OPEN_POSITIONS`,
  `MAX_TOTAL_NOTIONAL_USDT`, per-venue caps) и снимок лимитов из runtime.
- Раздел здоровья повторяет ключевые проверки `/healthz` (auto-hedge daemon,
  opportunity scanner). Если таск остановлен или вернул ошибку, строка подсвечена
  красным «DEAD».
- Блок Pending approvals подгружается из `ops_approvals.json`: видны запросы на
  снятие HOLD/выход из DRY_RUN/поднятие лимитов, кто и когда их инициировал, и
  текущий статус.
- Блок Controls использует формы, бьющие в прокси `/ui/dashboard/hold` /
  `resume` / `kill`. Они конвертируют форму в JSON, вызывают прежние guarded
  эндпоинты и показывают результат прямо в браузере. Настоящее возобновление
  торговли по-прежнему требует второго оператора и `APPROVE_TOKEN` — панель
  явно напоминает об этом, никакого обхода двухшаговой защиты нет.
- Добавлен read-only блок **PnL / Risk**: в одном месте видно текущий unrealised
  PnL, stub поля `realised_pnl_today_usdt` (пока всегда `0.0`, до интеграции
  расчёта фактических закрытий), суммарный notional открытых/partial ног и запас
  CapitalManager по стратегиям. Перед сменой смены операторы могут быстро
  пробежать глазами этот блок вместо разбора логов.

### Strategy Orchestrator

- Панель теперь визуализирует план глобального оркестратора стратегий. Блок
  **Strategy Orchestrator** вычисляет `compute_next_plan()` и показывает список
  зарегистрированных стратегий с решением на следующий тик.
- Значение `run` означает, что стратегия пройдёт risk-gates и готова к запуску
  при следующем проходе оркестратора.
- Статус `cooldown` появляется, если стратегия недавно фейлилась и ещё
  отстаивается. Причина (`recent_fail` и т.п.) подсвечена жёлтым, чтобы оператор
  видел, что идёт охлаждение.
- Статус `skip` сигнализирует, что глобальные условия (HOLD, risk caps и др.)
  запрещают торговлю. Причины `hold_active` и `risk_limit` подсвечены красным —
  это ожидаемо, если включён HOLD или сработал лимит на экспозицию.
- Роль `viewer` видит блок только для мониторинга и получает отметку `READ ONLY`
  прямо над таблицей. Роль `operator` использует план для принятия решений, но
  исполнение по-прежнему запускается вручную (никаких auto-run без одобрения).

### Risk snapshot

- В панели добавлен блок **Risk snapshot**. Он собирает агрегированный риск из
  `build_risk_snapshot()` и показывает суммарный notional по активным и
  частичным ногам, разбивку экспозиции и нереализованного PnL по биржам,
  количество outstanding partial hedges и текущее состояние автопилота.
- Поле `risk_score` сейчас отображается как заглушка «TBD» и будет расширено в
  сторону VaR/MSR/RPI в следующих релизах.
- Роль `viewer` видит только статус и риск (панель явно пишет, что управление
  HOLD/RESUME/KILL недоступно). Роль `operator` использует эти данные для
  принятия решений перед `resume` / `raise-limits` / `kill`, но бизнес-логика
  двухшагового процесса остаётся прежней.

### Liquidity / Balance safety

- Бот самостоятельно следит за доступным балансом/маржой на каждой бирже через
  модуль `services/balances_monitor`. Если свободного USDT меньше стандартного
  хеджа, маржинальное плечо выбрано до потолка или биржа сигналит о приближении
  margin call, флаг `liquidity_blocked` переходит в `true`.
- При `liquidity_blocked=true` новые сделки блокируются edge-guard'ом, а
  runtime автоматически включает HOLD (двухшаговый процесс отключения HOLD
  сохраняется — никаких автоматических resume нет).
- На `/ui/dashboard` появился раздел **Balances / Liquidity**: для каждой биржи
  показываются free/used USDT, статус риск-проверки и пояснение. При блокировке
  секция подсвечивается сообщением «TRADING HALTED FOR SAFETY».
- В Telegram-боте добавлена команда `/liquidity`, которая возвращает тот же
  снимок и пометку, заблокирована ли торговля по ликвидности.
- Чтобы возобновить торговлю, сначала пополните баланс/снизьте плечи, убедитесь
  что `/ui/dashboard` или `/liquidity` возвращают `liquidity_blocked=false`, и
  только затем пройдите стандартный двухшаговый процесс `resume` + `approve`.

### Best venue routing & execution quality

- Хедж-бот теперь автоматически выбирает venue для каждой ноги хеджа через
  модуль `services/execution_router`. Маршрутизатор сравнивает котировки и
  актуальные тейкер-фии из runtime, проверяет доступный баланс и выбирает
  venue с лучшей эффективной ценой. Если ликвидности не хватает, выбор
  помечается как `liquidity_ok=false`.
- Каждая попытка хеджа (даже неуспешная) сохраняется в `data/execution_stats.json`:
  таймштамп, venue, сторона, плановая цена vs. фактическое исполнение,
  рассчитанная просадка в bps и статус успеха. Файл ротируется автоматически
  (последние ~500 записей), поэтому включите его в бэкапы.
- В панели `/ui/dashboard` появился блок **Execution Quality**: видно
  суммарный success rate по последним ногам, историю с расчётом slippage и
  разбивку по venue. Биржи с высокой долей сбоев (30%+ отказов) подсвечиваются
  красным — это сигнал остановить авто-хедж, проверить API-ограничения и
  поднять инцидент.
- Перед отключением DRY_RUN и в бою проверяйте блок исполнения: резкий рост
  slippage или серия фейлов на одной бирже = повод поставить HOLD и разобрать
  ситуацию до возобновления торговли.

Панель подходит для ручных health-check'ов во время дежурства: прокрутите её и

## Risk Advisor

- На панели и в `GET /api/ui/risk_advice` появился блок **Risk Advisor**. Он анализирует
  последние снэпшоты `pnl_history_store`, тренд по unrealised PnL, количество
  зависших partial hedges и факты срабатывания `AUTO_THROTTLE/...`.
- Модуль выдаёт только рекомендации: например, «ослабить лимит на ~10%» при
  стабильной прибыли или «ужесточить лимиты / держать DRY_RUN_MODE» при убытках
  и автотроттле. Никаких автоматических изменений лимитов не происходит.
- Если операторы решат следовать подсказке, используйте только существующий
  двухшаговый процесс (`/api/ui/risk/limit-request` → `/api/ui/risk/limit-approve`
  и аналогичные флоу на поднятие/понижение лимитов или выход из DRY_RUN). Любые
  изменения вне утверждённого двухманового процесса запрещены.
- Сам блок — это напоминание: чтобы применить новое значение, нужен ручной
  аппрув второго оператора. Используйте подсказку как сигнал к расследованию, а
  не как автоматический триггер.
убедитесь, что HOLD/SAFE_MODE в ожидаемом состоянии, runaway guard не уткнулся в
лимиты, авто-хедж не накопил ошибки, живой риск не превышает лимиты, а pending
approvals не зависли без внимания.

## Crash / Restart recovery

1. После любого рестарта бот запускается в SAFE_MODE/HOLD, даже если до сбоя шла
   торговля.
2. Проверьте `/api/ui/status/overview`, `/api/ui/positions`, `/api/ui/alerts` —
   `runtime_state.json` восстановит лимиты и причину HOLD, но автоторговля не
   начнётся.
3. Сверьте runaway guard и риск-лимиты, убедитесь, что экспозиция соответствует
   ожиданиям. При необходимости изучите `data/hedge_log.json` и
   `data/ops_alerts.json`.
4. Возобновление торговли всегда через двухшаговый флоу: `resume-request`
   → ожидание второго оператора → `resume-confirm` с `APPROVE_TOKEN` → `resume`.
5. Файлы в `data/` редактируйте вручную только в аварийных случаях; храните
   бэкапы для аудита.

## Частичные хеджи (`partial`)

- Если HOLD, лимит runaway guard или SAFE_MODE сработали после исполнения
  первой ноги сделки, в `/api/ui/positions` появится позиция со статусом
  `partial`.
- Такая запись сохраняется в `data/hedge_positions.json`, поднимается после
  рестарта и учитывается в экспозиции и `unrealized_pnl_usdt` — риск считается
  открытым.
- Оператор обязан вручную закрыть оставшуюся ногу на бирже, затем
  восстановить нормальный баланс через стандартные процедуры закрытия позиции.
- Это штатный аварийный сценарий: сам факт появления `partial` не считается
  багом, но требует быстрого ручного вмешательства.

## Two-man approval flow

Критические действия (снятие HOLD, повышение риск-лимитов и выход из `DRY_RUN_MODE`) выполняются только по двухшаговой схеме с аудитом:

- **Запрос.** Оператор A инициирует действие и указывает причину (через UI/API или ops Telegram-бота):
  - `/resume <reason>` — запросить снятие HOLD.
  - `/raise_limit <limit> <scope|-> <value> <reason>` — поднять лимит (`max_position_usdt`, `max_open_orders`, `max_daily_loss_usdt`). Используйте конкретный символ/venue или `-` для значения по умолчанию.
  - `/exit_dry_run <reason>` — запросить отключение `DRY_RUN_MODE` и переход к реальным ордерам.
  - `/hold <reason>` — мгновенно включает HOLD (без ожидания подтверждения).
  Запрос фиксируется в `data/ops_approvals.json` и журнале событий (`resume_requested`, `risk_limit_raise_requested`, `exit_dry_run_requested`). Ops notifier и Telegram-бот публикуют сообщение вида «Оператор A запросил снять HOLD (причина …), ждём подтверждения».
- **Подтверждение.** Оператор B получает идентификатор заявки (показывается в ответе и в `/status`) и подтверждает действие командой `/approve <request_id> <APPROVE_TOKEN>`. После второго шага:
  - HOLD снимается (`resume_confirmed`),
  - лимит обновляется (`risk_limit_raise_approved`),
  - `DRY_RUN_MODE` выключается (`exit_dry_run_approved`).
  Telegram-бот отправляет уведомление о подтверждении.
- **Надёжность.** Pending-заявки переживают рестарты: список хранится в `ops_approvals.json` и поднимается при старте. Команда `/status` показывает `hold_active`, причину HOLD, `safe_mode`, `dry_run_mode`, состояние авто-хеджа (включая последнюю попытку и счётчик фейлов) и активные запросы на подтверждение.

Вся активность остаётся в аудитах (`/api/ui/events`, `data/ops_approvals.json`) и в Telegram-канале. Действующая защита `resume-confirm` с `APPROVE_TOKEN` сохранена: без корректного токена операции не выполняются.

## Safety / Controls

- **HOLD** — ручная или автоматическая остановка петли (включает SAFE_MODE).
- **SAFE_MODE** — запрет на выставление ордеров, мониторинг продолжается.
- **Kill switch** — мгновенное отключение, всегда приводит к HOLD и SAFE_MODE.
- **Runaway guard** — лимиты на заявки/отмены в минуту; при превышении включают
  HOLD и фиксируют причину в `runtime_state.json`.
- **Two-man rule** — `resume-confirm` с `APPROVE_TOKEN`, без него HOLD не будет
  снят.

 codex/add-operator-runbook-documentation-30d5c6
 ⚠️ **LIVE-торговля:** связка `PROFILE=live` и `DRY_RUN_ONLY=false` означает реальные заявки на бирже. Всегда запускайте сервис в HOLD (`mode=HOLD`) и с `SAFE_MODE=true`, проверяйте лимиты и пары (`loop_pair`/`loop_venues`), баланс и ключи, и только после ручной проверки переводите бота в `RUN` и снимаете `SAFE_MODE`.

Перед началом торговли реальными средствами заполните `APPROVE_TOKEN` и убедитесь, что процедура `resume-request` → `resume-confirm` отработана. Без второго оператора HOLD не снимается.

 main
## 1. Ежедневная проверка здоровья

1. Откройте документацию Swagger по адресу `https://<host>/docs`.
   - Убедитесь, что страница отвечает и список ручек прогружается.
2. Запросите агрегированное состояние:
   - `curl -s -H "Authorization: Bearer $API_TOKEN" https://<host>/api/ui/status/overview | jq`.
   - Поле `overall` принимает значения `OK`, `WARN`, `ERROR` или `HOLD`.
   - В блоке `safety` проверяйте `hold_active`, `resume_request` (если ожидание второго оператора ещё не подтверждено), текущие
     счётчики runaway-лимитов (`counters.orders_placed_last_min`, `counters.cancels_last_min`) и clock-skew (`clock_skew_s`).
3. Посмотрите раскладку по компонентам:
   - `curl -s -H "Authorization: Bearer $API_TOKEN" https://<host>/api/ui/status/components | jq '.components[] | {id, status, summary}'`.
   - Проверяйте статус P0-гардов (`recon`, `rate_limit`, `runaway_breaker`, …) и метрики.
4. Интерпретация статусов:
   - **OK** — компонент в норме, вмешательство не требуется.
   - **WARN** — деградация, но торговый цикл всё ещё разрешён. Наблюдайте и при необходимости включайте HOLD вручную.
   - **ERROR** — критическая ошибка, компонент не выполняет SLO. Требуются действия оператора.
   - **HOLD** — торговый цикл остановлен (авто-HOLD или операторская пауза).
5. SLO-алерты:
   - Список активных нарушений отображается в `overview.alerts[*]`.
   - Любой P0-алерт (например, `recon mismatch`, `runaway_breaker`) автоматически ставит авто-HOLD: бот включает SAFE_MODE и прекращает выставлять заявки.
6. Если `overall=HOLD`, уточните причину через `components` или журнал инцидентов `/api/ui/status/components` → `incidents`.

## 2. Управление риском и паузой

1. Режимы:
   - **Обычная торговля** — `SAFE_MODE=false`, `overall` не в HOLD, торговый цикл активен.
   - **SAFE_MODE=true** — ордера не отправляются, но цикл и мониторинг продолжают работу (используйте для dry-run).
   - **HOLD** — система ставит петлю на паузу; SAFE_MODE включается автоматически, пока HOLD не снят.
2. Авто-HOLD:
   - Бот сам переводит себя в HOLD при P0-ошибке, критическом SLO-алерте или провале preflight-а.
   - В UI и Телеграме приходит уведомление о причине и таймштампе.
3. Ручная пауза и продолжение через Телеграм:
   - `/hold <reason>` (алиас `/pause`) — включает SAFE_MODE и HOLD, фиксирует причину.
   - `/resume <reason>` — создаёт заявку на снятие HOLD (ожидает подтверждения `/approve <request_id> <APPROVE_TOKEN>`).
   - `/raise_limit <limit> <scope|-> <value> <reason>` — запросить повышение риск-лимита (подтверждается `/approve`).
   - `/exit_dry_run <reason>` — запросить отключение `DRY_RUN_MODE` (подтверждается `/approve`).
   - `/approve <request_id> <APPROVE_TOKEN>` — второй оператор подтверждает pending-заявку.
   - `/status` — текущий обзор состояния (hold_active, safe_mode, dry_run_mode, авто-хедж, pending approvals).
   - Команды работают только из авторизованного чата `TELEGRAM_CHAT_ID`.
4. **Новая двухэтапная процедура возобновления (Two-Man Rule):**
   1. Первый оператор после устранения причины холда выполняет `POST /api/ui/resume-request` с причинами (`{"reason": "почему
      безопасно", "requested_by": "имя"}`). HOLD остаётся активен, но фиксируется таймштамп и автор запроса.
   2. Второй оператор предоставляет секрет `APPROVE_TOKEN` через `POST /api/ui/resume-confirm` (`{"token": "<секрет>", "actor":
      "имя"}`). При неверном токене вернётся `401` и HOLD не снимется.
   3. После успешного подтверждения `hold_active` станет `false`, и можно (при `SAFE_MODE=false`) вызвать `POST /api/ui/resume`
      или команду CLI/Telegram для перевода режима в `RUN`.
   - `APPROVE_TOKEN` обязан быть заполнен в `.env` для продакшена и храниться отдельно от обычного `API_TOKEN`.
5. Ручная пауза через CLI `propbotctl`:
   - `python3 cli/propbotctl.py --base-url https://<host> status` — быстрый обзор без открытия Swagger.
   - `python3 cli/propbotctl.py --base-url https://<host> components` — таблица статусов компонентов.
   - `python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" pause` — постановка HOLD (payload `{"mode": "HOLD"}`).
   - `python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" resume` — попытка выхода из HOLD (payload
     `{"mode": "RUN"}`); сработает только после подтверждённого `resume-confirm`.
   - Команда `export-log` использует тот же токен, что и `pause`/`resume`; без bearer-аутентификации выгрузка событий заблокирована.
   - Bearer-токен передавайте через `--token` или переменную окружения `API_TOKEN`. Никогда не коммитьте токен в git.
6. Принудительная пауза через REST (если CLI недоступен):
   - `curl -X POST https://<host>/api/ui/hold -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
     -d '{"reason": "manual_hold", "requested_by": "оператор"}'`.
   - `curl -X POST https://<host>/api/ui/resume-request -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
     -d '{"reason": "готово к возобновлению", "requested_by": "оператор"}'`.
   - `curl -X POST https://<host>/api/ui/resume-confirm -H "Authorization: Bearer $API_TOKEN" -H "Content-Type: application/json" \
     -d '{"token": "<APPROVE_TOKEN>", "actor": "второй оператор"}'`.
   - После успешного подтверждения и переключения `SAFE_MODE=false` выполните `curl -X POST https://<host>/api/ui/resume -H
     "Authorization: Bearer $API_TOKEN"` для установки `mode=RUN`.
7. После устранения причины HOLD убедитесь, что критические компоненты вернулись в `OK`, и только после этого инициируйте
   `resume-request`/`resume-confirm`. Если `hold_active=true` вернулся автоматически, изучите причину в `status.overview.safety`.

### Канал операторских оповещений

- Все действия оператора и авто-защит (HOLD/RESUME, kill switch, cancel-all,
  авто-хедж, runaway-гард) пишутся в `data/ops_alerts.json`. Файл содержит
  чувствительные детали расследований, поэтому храните его на защищённой
  машине.
- При `TELEGRAM_ENABLE=true` и заполненных `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`
  уведомления дублируются в авторизованный Telegram-чат. Ошибки сети не
  блокируют основной цикл: при недоступности Bot API записи всё равно попадают
  в JSON.
- Для просмотра истории через API используйте защищённый эндпоинт
  `GET /api/ui/alerts` с bearer-токеном (`Authorization: Bearer $API_TOKEN`).
  Возвращается список последних событий с таймштампами и категориями. Не
  публикуйте этот поток наружу — он предназначен только для дежурной команды.

### Хедж-позиции и PnL

- Все кросс-биржевые хеджи (биржи, стороны, нотация в USDT, цены входа,
  плечо, таймштамп открытия и статус) пишутся в `data/hedge_positions.json`.
  Путь можно переопределить переменной окружения `POSITIONS_STORE_PATH`. Файл —
  обычный JSON-массив, поэтому в экстренной ситуации его можно просмотреть
  напрямую (`jq '.' data/hedge_positions.json`).
- Успешные боевые сделки фиксируются со статусом `open`, в каждой ноге
  сохраняются `avg_price`, размер (в USDT и базе) и плечо. Симуляции имеют
  статус `simulated` — так проще фильтровать тестовые записи в UI и журналах.
- Если перед запуском установить `DRY_RUN_MODE=true`, то ручные и автоматические
  хеджи выполняются в безопасной симуляции: реальные ордера не уходят на биржи,
  но все лимиты риска и runaway guard продолжают работать. В `hedge_positions`
  и `hedge_log` записи помечаются `status="simulated"`, алерты в Telegram
  содержат пометку «DRY_RUN_MODE», а `/api/ui/status/overview`/`/state` явно
  показывают `dry_run_mode=true`.
- Для оперативной проверки используйте защищённый эндпоинт
  `GET /api/ui/positions` (тот же bearer-токен, что и для `/api/ui/alerts`).
  Ответ содержит массив позиций с обеими ногами (`legs[*]`), рассчитанный
  нереализованный PnL по каждой ноге и суммарный `unrealized_pnl_usdt`, а также
  агрегированную экспозицию по биржам (`exposure.long_notional`,
  `short_notional`, `net_usdt`). Если марк-прайсы временно недоступны (например,
  в тестовом контуре без связи с биржей), сервис возвращает `mark_price`,
  равный цене входа, и PnL будет 0 — это ожидаемая заглушка.

### PnL / Exposure trend

- История снимков (timestamp, агрегированная экспозиция по биржам, суммарный
  unrealised PnL и количество открытых/частичных позиций) пишется в JSON по
  пути `PNL_HISTORY_PATH`. По умолчанию это `data/pnl_history.json`, путь можно
  переопределить в `.env`, как и для других операторских стораджей.
- Для экспорта последних значений используйте защищённый эндпоинт
  `GET /api/ui/pnl_history?limit=N`. Ответ — словарь с ключом `snapshots`, где
  массив отсортирован от свежего к старому. Так оператор может быстро выгрузить
  тренд в Excel/Grafana без доступа к файловой системе хоста.
- Снимок разделяет реальные и симуляционные ноги: DRY_RUN/`dry_run_mode`
  экспозиция идёт в блок `simulated` и не попадает в боевой `total_exposure_usd`.
  На дашборде блок «Risk & PnL trend» подсвечивает динамику unrealised PnL и
  суммарного риска между двумя последними снимками (зелёная стрелка — лучше,
  красная — хуже) и выводит счётчик открытых/частичных и симуляционных позиций.

## 3. Ротация секретов

1. Сформируйте JSON с новыми ключами (пример для Binance testnet):
   ```bash
   cat <<JSON > /tmp/keys.json
   {
     "BINANCE_UM_API_KEY_TESTNET": "<новый ключ>",
     "BINANCE_UM_API_SECRET_TESTNET": "<новый секрет>"
   }
   JSON
   ```
2. Отправьте обновление через REST или CLI:
   - REST:
     ```bash
     curl -X POST https://<host>/api/ui/secret \
       -H "Authorization: Bearer $API_TOKEN" \
       -H "Content-Type: application/json" \
       --data @/tmp/keys.json
     ```
   - CLI: `python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" rotate-key --value 'новый-секрет'` (значение можно передать из `stdin`, если не хотите хранить его в shell истории).
3. Убедитесь, что ответ содержит статус `ok` и новые значения не появляются в логах (секреты всегда редактируются).
4. Всегда удаляйте временные файлы (`rm /tmp/keys.json`).
5. **Важно:** секреты НЕЛЬЗЯ коммитить или хранить в git/облаке. Используйте менеджер секретов или защищённые vault-решения.

## 4. Лимиты риска и параметры

1. Просмотр текущих лимитов: `curl -s https://<host>/api/ui/state -H "Authorization: Bearer $API_TOKEN" | jq '.risk + {flags: .flags, dry_run: .dry_run}'`.
2. Проверка управляющего состояния: `curl -s https://<host>/api/ui/control-state -H "Authorization: Bearer $API_TOKEN" | jq`.
3. Изменение параметров на лету (доступно в `paper`/`testnet` при `SAFE_MODE=true`):
   ```bash
   curl -X PATCH https://<host>/api/ui/control \
     -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
    -d '{"order_notional_usdt": 100, "min_spread_bps": 1.2, "dry_run_only": true, "loop_pair": "BTCUSDT", "loop_venues": ["binance-um"]}'
   ```
   - Параметры `dry_run_only`, `order_notional_usdt`, `min_spread_bps`, `poll_interval_sec`, список пар/бирж обновляются без рестарта.
   - После PATCH выполните `GET /api/ui/control-state` и убедитесь, что изменения применены.
   _⚠️ Поля `pair` и `venues` больше не принимаются — сервер их игнорирует, и бот продолжит работать со старыми значениями `loop_pair`/`loop_venues`._
4. Параметры, требующие перезапуска:
   - `PROFILE`, `SAFE_MODE` на уровне `.env`, ключи API (если бот должен прочитать их при старте), `TWO_MAN_RULE` при изменении значения, окружение `MODE`.
   - Измените `.env`, затем выполните аккуратный рестарт (см. раздел 6).
5. Если `risk_blocked=true`, изучите `risk_reasons` в ответе `/api/ui/state` и устраните нарушения (например, превышение `MAX_POSITION_USDT`).
6. Новые runaway-лимиты задаются переменными `.env` `MAX_ORDERS_PER_MIN` и `MAX_CANCELS_PER_MIN`. При превышении лимита бот ставит HOLD автоматически и возвращает `HTTP 423` с причиной. Сбросите счётчики (ожиданием ~1 минуты) и повторите `resume-request`/`resume-confirm` только после выяснения причин всплеска.

### Автоматический режим (Auto Mode)

- Включается через переменную окружения `AUTO_HEDGE_ENABLED=true` перед стартом сервиса. При необходимости подстройте `AUTO_HEDGE_SCAN_SECS` (частота опроса сканера) и `MAX_AUTO_FAILS_PER_MIN` (сколько ошибок подряд допускается без авто-HOLD).
- Демон повторно использует текущий opportunity scanner. Перед каждой попыткой он проверяет `hold_active`, SAFE_MODE, runaway-лимиты, активные risk breaches и наличие незавершённого two-man resume. Если что-то из защит не прошло, автоторговля пропускает цикл и оставляет HOLD нетронутым.
- Исполнение проходит через тот же путь, что и ручной REST (`/api/arb/execute`), поэтому все существующие лимиты, runaway guard и approvals продолжают действовать. Демон **не** снимает HOLD самостоятельно — возобновление по-прежнему требует `resume-request`/`resume-confirm`.
- Для мониторинга добавлен блок `auto_hedge` в `/api/ui/status/overview` (`auto_enabled`, `last_opportunity_checked_ts`, `last_execution_result`, `consecutive_failures`, `on_hold`). Если количество ошибок за последнюю минуту превысит `MAX_AUTO_FAILS_PER_MIN`, бот сам переведёт себя в HOLD и запишет причину в статус.
- Каждая автоматическая сделка или отказ журналируется в `data/hedge_log.json` с инициатором `YOUR_NAME_OR_TOKEN`. Лог доступен по эндпоинту `GET /api/ui/hedge/log` (требует того же bearer-токена, что и остальные операторские ручки).
- Даже в авто-режиме приоритет остаётся за лимитами риска, runaway-защитами и ручным HOLD: если торговля выглядит аномальной, оставляйте HOLD до выяснения причин.

## 5. Экспорт журнала событий

Экспорт доступен только при передаче действительного bearer-токена (CLI использует тот же `API_TOKEN`, что и команды `pause/resume`).

1. Быстрый экспорт через curl:
   ```bash
   curl -s https://<host>/api/ui/events/export \
     -H "Authorization: Bearer $API_TOKEN" \
     -G --data-urlencode "format=csv" --data-urlencode "limit=500" \
     -o propbot-events.csv
   ```
2. Через CLI (рекомендуется при больших объёмах):
   ```bash
   python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" export-log --out logs/propbot-events.json
   ```
3. Сохраняйте выгрузку в защищённом каталоге и делитесь её только с инженерами расследования.

## 6. Аккуратное выключение и рестарт

1. Перед остановкой убедитесь, что бот в HOLD (`/pause` в Telegram, `propbotctl pause` или `PATCH /api/ui/control` → `{"mode":"HOLD","dry_run_only":true}`).
2. Проверьте, что открытых позиций нет: `GET /api/ui/state` → блок `risk.positions` должен быть пустой.
3. Сохраните журнал событий, если нужно (см. раздел 5).
 codex/add-operator-runbook-documentation-30d5c6
4. Зафиксируйте текущее состояние через CLI: `python3 cli/propbotctl.py --base-url https://<host> status` — убедитесь, что `overall.status=HOLD` и нет неожиданных алертов.
5. Остановите контейнер:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env down
   ```
6. При рестарте обновите образ/конфиг и поднимите сервис:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d
   ```
7. После старта выполните проверки из раздела 1. Для быстрого сигнала используйте CLI: `python3 cli/propbotctl.py --base-url https://<host> status` и `python3 cli/propbotctl.py --base-url https://<host> components`. Затем подтвердите через Swagger, что `overall=OK` и HOLD снят вручную, если требовалось.

## 7. Прод-деплой через Docker Compose

1. На чистом Linux-сервере установите Docker и Docker Compose plugin.
2. Склонируйте репозиторий и перейдите в каталог `deploy/`.
3. Создайте рядом каталог для данных и задайте права контейнеру:
   ```bash
   sudo mkdir -p ../data
   sudo chown 1000:1000 ../data
   sudo chmod 770 ../data
   ```
   Каталог будет примонтирован как `/app/data` и хранит `runtime_state.json`, `ledger.db`, экспортированные логи и снапшоты.
4. Скопируйте `deploy/env.example.prod` в `.env` и заполните значения (API токены, ключи, профиль, Telegram, лимиты).
5. Для первого запуска оставьте `SAFE_MODE=true`, `DRY_RUN_ONLY=true` (или `SAFE_MODE=true` + HOLD для тестнета/лайва) — убедитесь, что `mode=HOLD` через `propbotctl status`.
6. Запустите сервис:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d
   ```
7. Проверьте `/docs`, `propbotctl status --base-url https://<host>` и убедитесь, что сервис активен, но остаётся в HOLD.
8. Когда проверки завершены, снимите HOLD через `propbotctl resume --base-url https://<host> --token "$API_TOKEN"` или Telegram (Two-Man Rule должен быть выполнен, если включён).

## 8. Продакшн-данные и файловая система

- В `deploy/docker-compose.prod.yml` каталог `../data` монтируется внутрь контейнера как `/app/data`.
- Это постоянное хранилище для базы (`ledger.db`), снимков состояния (`runtime_state.json`), экспортов и временных файлов оркестратора.

4. Остановите контейнер:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env down
   ```
5. При рестарте обновите образ/конфиг и поднимите сервис:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d
   ```
6. После старта выполните проверки из раздела 1 (Swagger, `/status/overview`, `/status/components`). Убедитесь, что `overall=OK` и HOLD снят вручную, если требовалось.

## 7. Продакшн-данные и файловая система

- В `deploy/docker-compose.prod.yml` каталог `../data` монтируется внутрь контейнера как `/app/data`.
- Это постоянное хранилище для базы (`ledger.db`), снимков состояния (`runtime_state.json`) и экспортов.
 main
- Проверьте, что папка `./data` существует на хосте и имеет права на запись для пользователя/группы, под которыми запускается Docker (`chown`/`chmod` при необходимости).
- Не удаляйте содержимое `./data` без бэкапа — там находятся рабочие журналы и состояние бота.

## Чеклист оператора после рестарта

- Бот поднялся и находится в SAFE_MODE/HOLD.
- Оператор проверил статусные ручки, алерты и лимиты риска.
- Оператор выполнил ручной двухшаговый RESUME (`resume-request` → `resume-confirm`).
- Только после подтверждения бот может снова торговать.

## Go-Live checklist

- все секреты и токены заданы (биржи, Telegram ops, APPROVE_TOKEN и прочие доступы);
- `DRY_RUN_MODE=true` на первой загрузке;
- контейнер не упал на старте, startup validation прошла успешно;
- `/healthz` отвечает `ok`;
- `/api/ui/status*/overview` показывает SAFE_MODE/HOLD и `build_version`;
- `/api/ui/positions` отражает открытые хеджи и текущий PnL;
- оператор вручную проходит двухшаговый RESUME перед реальной торговлей.
