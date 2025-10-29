Цель этого ТЗ:
Мы считаем, что TASK 1 (final-core-harden) и TASK 2 (final-governance) уже смержены и реально работают:
- есть рабочие StrategyBudgetManager, StrategyRiskManager (freeze), per-strategy PnL и drawdown,
  persistent state по стратегиям, per-strategy capital budget,
  автопилот с блокировками, ops_report с реальными данными,
  dashboard показывает реальные statусы, роли viewer/auditor/operator,
  two-man rule / approvals flow в критических действиях,
  audit_log с записью всех запросов операторов,
  ни одна ручка без прав не может снять HOLD/RESUME/kill,
  и утечек ключей в UI/логи нет.

Теперь нам нужно добить систему до уровня "максимальный проп-деск", как описано в v3 спецификации.
Задача TASK 3 — это ДОСТРОИТЬ ОСТАВШИЕСЯ БОЛЬШИЕ БЛОКИ, которые ещё не покрыты TASK 1 и TASK 2.

Если где-то описано то, что уже сделано в TASK 1 или TASK 2 — НЕ ПЕРЕДЕЛЫВАТЬ. Просто убедись,
что новые части аккуратно интегрируются в текущую архитектуру. Не ломать успешные тесты.

Ниже идёт детализация. Это нужно сделать в нескольких новых PR, но можно оформить как
ветку codex/max-prop-expansion с серией коммитов, пока ты это всё доводишь.


================================================================================
1. INCIDENT MANAGEMENT & POSTMORTEM GATE
================================================================================

Это НЕ то же самое, что audit_log из TASK 2. Audit = кто что нажал.
Сейчас нам нужно полноценный журнал инцидентов (incidents_log) + культура постмортема.

1.1 incidents_log persistent store
- Создай persistent store (файл JSON/SQLite, например data/incidents_log.json или таблица),
  который хранит массив записей структурой:
    {
      "ts_utc": "...",
      "severity": "P0 | P1 | P2",
      "component": "recon | executor | strategy:<name> | risk_governor | ws_feed:<venue> | ...",
      "summary": "коротко что случилось",
      "action_taken": "HOLD | strategy_degraded | hedge_flatten | cancel_all | ...",
      "operator_ack": null или {"operator_id": "...", "ts_ack_utc": "..."},
      "resolved_ts_utc": null или ISO timestamp,
      "postmortem_root_cause": null или текст
    }

- Инцидент пишется в двух случаях:
  * автоматически кодом (пример: потерян хедж → P0),
  * вручную оператором (можно POST /api/ui/incidents/manual_log c ролью operator,
    чтобы записать P2 "memory pressure" и т.п.).

1.2 severity политика
- P0 = системная угроза капиталу/нейтральности:
  - мы потеряли хедж (atomic executor не смог симметрично исполнить вторую ногу);
  - recon нашёл рассинхрон позиций с биржей (у нас directional риск, которого быть не должно);
  - пробит глобальный дневной loss cap;
  - попытка тампера (ручной модификации state / log);
  - split-brain (см. DR).
  Все P0 → немедленный глобальный HOLD.

- P1 = серьёзная деградация, но капитал контролируется:
  - стратегия ушла в degraded из-за лимита бюджета / drawdown / circuit breaker;
  - критичный провал WS фида по конкретной бирже;
  - rate limit бан на бирже, с которой мы не закрываем открытую позу, но пока не потеряли симметрию.
  P1 НЕ обязан включать глобальный HOLD, но обязан зафиксировать degraded для затронутой стратегии.

- P2 = технические состояния:
  - memory pressure;
  - autopilot заблокировал RESUME из-за риска и т.д.

1.3 связь с HOLD/RESUME
- Если возник P0, код ДОЛЖЕН:
  - перевести систему в HOLD;
  - записать incidents_log entry severity="P0" с action_taken="HOLD";
  - записать audit_log о том, что HOLD был активирован автоматически.
- Нельзя RESUME из HOLD после P0, пока:
  - не заполнено поле postmortem_root_cause для этого инцидента,
  - не выставлен operator_ack,
  - не прошёл двухшаговый approval flow (two-man rule) с ролями operator/operator.

ЭТО ВАЖНО: TASK 2 уже ввёл two-man rule для критических действий,
но сейчас нужно увязать это с P0.
RESUME в live после P0 = запрещено без:
(а) postmortem_root_cause в incidents_log,
(б) двух разных операторов через approvals,
(в) блокировки автопилота (см. ниже).

1.4 /api/ui/incidents
- GET должен возвращать:
  - активные незакрытые P0/P1/P2 без operator_ack;
  - последние N закрытых.
- POST /api/ui/incidents/ack_and_postmortem:
  - доступно только оператору (role operator),
  - тело: {incident_id, postmortem_root_cause},
  - ставит operator_ack и postmortem_root_cause, и resolved_ts_utc если инцидент больше не активен.
- Dashboard должен показывать:
  - есть ли активный P0 без postmortem? Если да — явно красный блок:
    "TRADE LOCKED: POSTMORTEM REQUIRED".
  - Это сигнал оператору, что RESUME невозможен до постмортема.

1.5 autopilot и постмортем
- Сейчас autopilot_last_decision уже пишется (TASK 1).
- Расширить автопилот: если есть незакрытые P0 без postmortem_root_cause → autopilot
  фиксирует autopilot_last_decision="blocked_by_unresolved_P0" и НЕ переводит систему в RESUME.
- Отобразить это в dashboard: "Autopilot status: blocked_by_unresolved_P0".


================================================================================
2. RUNTIME STATE TAMPER DETECTION (HMAC)
================================================================================

Нужно защититься от ручного редактирования runtime_state_store и audit логов на VPS.

2.1 HMAC подпись runtime_state
- runtime_state (режим HOLD/RESUME/KILL, safe_mode, leader_id, autopilot_last_decision и т.д.)
  должен храниться на диске с полем signature = HMAC_SHA256(secret, canonical_json).
- При загрузке бот проверяет подпись.
- Если подпись не совпала → это tamper_detected:
  - создать P0 инцидент severity="P0", component="control_plane", summary="tamper_detected",
    action_taken="HOLD",
  - принудительный переход в HOLD,
  - запись в audit_log.

Важно:
- secret для HMAC берётся из защищённого секрета (не класть в git).
- Нельзя продолжать работу в RESUME, если этот tamper не разрулен.

2.2 audit.log integrity
- Audit лог из TASK 2 сейчас пишется persistent.
- Добавь контроль целостности:
  - для каждой новой записи хранить cumulative_hash (например SHA256 предыдущего cumulative_hash + новая запись).
  - При старте процесса пересчитать цепочку. Если есть дыра → P0 tamper_detected → HOLD.

2.3 Dashboard / ops_report
- Показывать в ops_report/security блок:
  tamper_status: "OK" или "TAMPER_DETECTED_HOLD".
- Если tamper_status != OK → автоматически удерживать систему в HOLD
  и блокировать autopilot от RESUME.


================================================================================
3. HOLD / RESUME / KILL / KILL-SWITCH / hedge_flatten
================================================================================

TASK 1 + TASK 2 уже ввели управление HOLD/RESUME/UNFREEZE/KILL через dashboard и approvals.
Теперь надо довести логику до индустриального уровня:

3.1 Состояния
- HOLD: никаких НОВЫХ торговых рисков. Стратегии не могут открывать новые позиции.
- RESUME: разрешено торговать.
- KILL: форс-режим "всё закрыть и замереть навсегда", используется в случае катастрофы.
  В KILL не должно быть ни входов, ни попыток автопилота что-либо делать.

3.2 kill-switch endpoint
- Добавить защищённый endpoint (role=operator, two-man rule required) типа /api/admin/kill_switch
  который:
  - логирует audit "manual_kill_switch_requested" и approval flow,
  - при подтверждении вторым оператором выполняет:
    * немедленно пытается закрыть ВСЕ открытые риски (см. hedge_flatten),
    * отменяет ВСЕ ордера,
    * переводит систему в KILL (persistent state),
    * создаёт P0 incident "manual_kill_switch_activated".

3.3 hedge_flatten()
- Реализовать функцию hedge_flatten():
  - пройти по всем текущим открытым позициям/хвостам/частичным ногам,
  - максимально быстро закрыть нетто-экспозицию по всем биржам.
  - Логику закрытия оформить в одном месте (execution/close_all_positions.py или аналог),
    чтобы это была НЕ заглушка.
  - После вызова hedge_flatten() система обязана перейти минимум в HOLD.
  - Это действие создаёт P0 incident severity="P0", summary="hedge_flatten_called".
  - Это действие пишется в audit_log.

3.4 Правило после P0
- Для любого P0 incident, в том числе вызванного hedge_flatten, возвращение к RESUME
  должно требовать:
  - postmortem_root_cause (см. раздел 1),
  - two-man rule (TASK 2),
  - роль operator,
  - никакого автопилота (autopilot должен отказывать).

3.5 safe_mode
- В runtime_state должен быть флаг safe_mode.
- Пока safe_mode включён:
  - стратегии должны понижать объёмы, использовать только пассивные/пост-онли входы
    или вообще быть частично ограничены в конкретных стратегиях (например maker).
- Запретить автопилоту снимать safe_mode.
- Снятие safe_mode = критическое действие (two-man rule + audit запись).


================================================================================
4. EXECUTION LAYER: ATOMIC TWO-LEG EXECUTOR И SLA
================================================================================

В TASK 1 акцент делался на бюджетах/фризах стратегий.
Сейчас нужно довести сам исполняющий движок до проп-уровня.

4.1 Частичное исполнение и немедленный хедж
- executor должен поддерживать сценарий:
  - leg_A частично исполнилась (например 30% размера),
  - НЕМЕДЛЕННО отправить leg_B (противоположную ногу) на соответствующую биржу,
    чтобы не висеть directional.
- Каждое такое мини-событие надо записывать в ledger и audit trail исполнения:
    {
      trade_parent_id: "...",
      partial_fill_pct: 0.30,
      hedge_sent: true,
      hedge_latency_ms: ...
    }

4.2 SLA метрики
- executor обязан измерять и хранить (в памяти + экспонировать через /api/ui/status/slo):
  - order_cycle_ms_p95 (время от решения до подтверждённого fill),
  - hedge_latency_ms_p95 (время между первой ногой и хеджем второй ноги),
  - reject_rate (отклонённые заявки биржей),
  - ws_gap_ms_p95 по каждой бирже (качество маркета).
- Если hedge_latency_ms_p95 превышает лимит, executor должен:
  - поднять P0 incident "hedge_latency_violation",
  - перевести систему в HOLD.
- Эти значения должны отображаться в /api/ui/status/slo (см. раздел 8).

4.3 Smart order routing
- executor должен уметь выбирать биржу для второй ноги на основании:
  - стаканной глубины,
  - комиссий,
  - загруженности пер-venue капа риска,
  - rate_limit health.
- Решение маршрутизации для каждой сделки должно логироваться:
  audit_log: { action: "route_decision", venue: "...", reason: "depth+fee+cap_ok" }.
- Если ВСЕ биржи плохие (ликвидность нет, rate limit бан, кап переполнен) → executor ДОЛЖЕН
  отменить сделку ДО того, как будет отправлена первая нога.
  Записать incident P1 "route_blocked_all_venues_bad".

4.4 cancel_all()
- executor должен иметь cancel_all_open_orders() для всех бирж.
- kill-switch вызывает cancel_all() + hedge_flatten().


================================================================================
5. RISK GOVERNOR: CAPS, CIRCUIT BREAKERS, STRESS TEST
================================================================================

TASK 1 покрыл budgets и per-strategy freeze, но нам нужно расширение глобального риск-контроля.

5.1 Global risk caps
Добавить глобальные лимиты в risk governor:
- MAX_TOTAL_NOTIONAL_USDT
- MAX_OPEN_POSITIONS
- MAX_EXPOSURE_PER_SYMBOL (BTCUSDT, ETHUSDT и т.д.)
- MAX_EXPOSURE_PER_VENUE (не держать весь риск на одной бирже)
- MAX_LEVERAGE_PER_VENUE (например, даже если биржа разрешает x100, мы сами режем до x3)
- DAILY_LOSS_CAP (абсолют или bps от equity)

Эти лимиты должны проверяться:
- при попытке новой сделки (до роута executor’а),
- периодически в фоне на основании открытых позиций.

Если лимит нарушен:
- глобальный HOLD,
- создать P0 incident "risk_cap_breached" c component="risk_governor",
- audit_log запись.

5.2 Circuit breakers по волатильности
- Для каждой пары (symbol, venue_pair) считать std_spread и волатильность.
- Если std_spread > SAFE_THRESHOLD или канал данных лагает (ws_gap_ms_p95 слишком большой):
  - пометить соответствующие стратегии как degraded,
  - записать P1 incident "circuit_breaker_triggered",
  - запретить этим стратегиям открывать новые сделки.
- Возврат из degraded возможен только вручную оператором (unfreeze/unpause)
  через уже существующий two-man rule поток (TASK 2).
- Это НЕ должен быть таймер на авто-возврат. Только ручной возврат.

5.3 Stress test / stress_tested_equity
- Реализовать калькуляцию stress_tested_equity:
  - смоделировать мгновенный гэп цены против нас на X bps,
  - оценить гипотетический убыток по всем открытым ножкам.
- Опубликовать это поле в ops_report и в /api/ui/status/overview.
- Если гипотетический убыток > STRESS_LIMIT:
  - блокировать новые сделки по символам/биржам, которые создают эту нагрузку,
  - записать P1 incident "stress_limit_warning".
  - (Глобальный HOLD не обязателен сразу, это не всегда P0, но это красный сигнал.)

5.4 Autopilot integration
- Автопилот НЕ имеет права автоматом RESUME,
  если:
    - breached DAILY_LOSS_CAP,
    - есть активный circuit_breaker,
    - есть P0 без postmortem_root_cause,
    - stress_limit_warning активен и помечен как "hard_block_autopilot".
- Это состояние autopilot должен честно фиксировать, а dashboard должен честно показывать.


================================================================================
6. RECON DAEMON: НЕЙТРАЛЬНОСТЬ И СОГЛАСОВАННОСТЬ С БИРЖАМИ
================================================================================

6.1 Recon daemon
- Создать (или доработать) recon-процесс/таск, который с периодичностью N секунд:
  - сравнивает ledger позиции/ордера/балансы с реальностью бирж,
  - проверяет, что все "рыночно-нейтральные" позиции действительно захеджированы (нет голой directional дельты).
- Если находит несоответствие:
  - создаёт P0 incident "recon_mismatch" с component="recon",
  - глобальный HOLD,
  - audit_log запись.

6.2 UI / status
- В /api/ui/status/components добавить секцию "recon":
  {
    "status": "OK|ERROR",
    "last_check_ts": "...",
    "last_error": "..." // если ERROR
  }

6.3 Autopilot
- Автопилот НЕ имеет права переводить систему в RESUME,
  если recon последний статус != OK.


================================================================================
7. DR / STANDBY NODE / SPLIT-BRAIN SAFETY
================================================================================

Сейчас в TASK 1 и 2 у нас нет полноценной аварийной реплики. Добавляем.

7.1 Standby instance
- Сделать второй runtime режим процесса: standby / follower / dry_run.
  Он:
  - читает маркет-дату,
  - сохраняет ledger_snapshot периодически (rolling backup),
  - НЕ отправляет ордера вообще.
- Этот процесс должен иметь endpoint /standby/status:
  {
    "synced": true/false,
    "can_be_promoted": true/false,
    "last_sync_ts_utc": "...",
  }

7.2 promote_standby flow
- Добавить endpoint /admin/promote_standby:
  - доступ только operator,
  - выполняется через two-man rule flow (TASK 2 approvals),
  - при подтверждении:
    * ставит runtime_state.current_leader = standby_id,
    * переключает основной процесс в HOLD,
    * логирует audit "PROMOTE_STANDBY",
    * создаёт P0 или P1 incident (severity=P0 если был живой лидер → риск split brain),
    * требует manual reconcile до RESUME.

- После promote_standby система автоматически входит в HOLD.
  RESUME возможен только после ручного reconcile и стандартного двухшагового подтверждения.

7.3 Split brain detection
- Если два инстанса одновременно считают себя лидером:
  - немедленно перевести систему в KILL,
  - создать P0 incident "split_brain_detected",
  - запретить автопилоту любые действия,
  - dashboard ВСЕГДА должен показывать fat red banner "SPLIT-BRAIN / KILL".

7.4 Dashboard
- Показывать:
  leader_id,
  standby_id,
  standby_synced?,
  split_brain_detected?.

7.5 README_DEPLOY.md
- Создать README_DEPLOY.md, где будет:
  - как поднять основной сервис,
  - как поднять standby как dry_run,
  - как проверить /standby/status,
  - как работает promote_standby,
  - почему split_brain = немедленный KILL.


================================================================================
8. OBSERVABILITY ENDPOINTS /api/ui/status/*
================================================================================

TASK 1 дал ops_report и dashboard. Теперь нужно довести API наблюдаемости до стандарта проп-деска.

Сделать следующие read-only ручки (RBAC: viewer/auditor/operator могут читать):

8.1 /api/ui/status/overview
Должно вернуть JSON со сводкой:
- overall_state: "OK|WARN|ERROR|HOLD|KILL"
- control_state: { mode(HOLD/RESUME/KILL), safe_mode, reason(if HOLD/KILL), since }
- pnl_snapshot:
    realized_pnl_today,
    unrealized_pnl,
    pnl_from_spread,
    pnl_from_funding,
    pnl_from_basis,
    pnl_from_rebate,
    fees_paid,
    slippage_cost,
    max_day_drawdown_bps,
    stress_tested_equity
- exposure_snapshot:
    total_notional,
    exposure_per_venue,
    exposure_per_symbol,
    leverage_estimate_per_venue
- strategies_status: массив
  [
    {
      strategy,
      state: "active|degraded|frozen|paused",
      budget_used,
      budget_limit,
      realized_pnl_today,
      drawdown_today,
      frozen (True/False),
      freeze_reason,
      degraded_reason,
      last_trade_ts
    },
    ...
  ]
- risk_flags:
    daily_loss_cap_status: "OK|BREACHED"
    circuit_breaker_triggered: true/false
    stress_warning: true/false
    unresolved_P0_blocking: true/false
- infra:
    leader_id,
    standby_id,
    standby_synced?,
    split_brain_detected?
- autopilot:
    autopilot_last_decision,
    autopilot_status ("ready"|"blocked_by_risk"|"blocked_by_unresolved_P0"|"blocked_by_stress" ...)

8.2 /api/ui/status/components
Вернуть детальный healthcheck:
- recon: {status,last_check_ts,last_error}
- risk_governor: {status,last_violation}
- orchestrator: {status,last_activity_ts}
- executor/atomic_two_leg: {status, hedge_latency_ms_p95, order_cycle_ms_p95, reject_rate}
- market_data_layer: {status, ws_gap_ms_p95_overall}
- per_exchange:
  exchanges[venue] = {
    status: "OK|WARN|ERROR",
    last_error,
    rate_limit_utilization,
    ws_gap_ms_p95
  }
- per_strategy:
  strategies[strategy] = {
    state,
    frozen,
    degraded_reason,
    last_trade_ts
  }
- standby_sync: {synced, last_sync_ts}
- backup_snapshotter: {last_backup_ts, status}

8.3 /api/ui/status/slo
Вернуть тех метрики производительности:
- ws_gap_ms distribution (p50/p95),
- order_cycle_ms distribution (p50/p95),
- hedge_latency_ms distribution (p50/p95),
- rate_limit hits per venue,
- process_uptime_sec,
- last_restart_ts_utc,
- memory_usage_mb,
- memory_pressure_flag (true/false).

Если memory_pressure_flag == true, автоматом создать P2 incident "memory_pressure".
Если memory_pressure влияет на hedge_latency_ms_p95 (превышен лимит) → эскалировать до P0 и HOLD.

8.4 /api/ui/stream/status (WebSocket)
- Вебсокет-протокол или SSE.
- Пушить события:
  - ENTER_HOLD
  - ENTER_KILL
  - PROMOTE_STANDBY
  - CIRCUIT_BREAKER_TRIGGERED
  - P0_INCIDENT
- Фронтенд dashboard может показывать алерты в реальном времени.


================================================================================
9. LEDGER DURABILITY / ROLLING BACKUP / PNL ATTRIBUTION
================================================================================

Часть PnL/бюджетов уже есть из TASK 1. Теперь мы усиливаем хранилище и прозрачность.

9.1 Rolling backup
- Ledger (ордера, fills, позиции, балансы, strategy_pnl_state) должен периодически
  (каждые N минут) снапшотиться в data/backups/ledger_snapshot_rolling.sqlite (или аналог).
- В /api/ui/status/components.backup_snapshotter показать last_backup_ts и статус.

9.2 PnL attribution
- Дополнить per-strategy и глобальную PnL статистику полями:
  - pnl_from_spread
  - pnl_from_funding
  - pnl_from_basis
  - pnl_from_rebate
  - fees_paid
  - slippage_cost
Эти поля должны попадать и в ops_report, и в /api/ui/status/overview.pnl_snapshot.

9.3 stress_tested_equity
- Значение stress_tested_equity (из раздела 5.3) должно сохраняться в снапшот,
  и быть доступно в ops_report и /api/ui/status/overview как часть pnl_snapshot.

9.4 Directionality check
- Ledger/recon должны отмечать, если мы сейчас directional без хеджа
  (то есть открыта только одна сторона).
  Это должно попадать в ops_report в секцию risk_flags:
    "has_unhedged_directional_exposure": true/false
- Если true → это должно уже быть P0 и HOLD (см. recon), но всё равно полезно видеть.


================================================================================
10. MARKET DATA LAKE / FORENSIC REPLAY / BACKTEST
================================================================================

Нам нужен research-контур, чтобы:
- разбирать аварии,
- проверять эффективность стратегий на истории.

10.1 Исторический сбор маркет-даты
- Market data layer должен сохранять rolling снапшоты:
  - midprices,
  - spreads между биржами,
  - funding_rate прогнозы,
  - basis_bps,
  - std_spread,
  - ws_gap_ms_p95,
  - hedge_latency_ms_p95 (history),
  - circuit_breaker_triggered flags.
- Формат: parquet или sqlite файлы в data/market_history/.
- Это НЕ должен быть супертяжёлый high-frequency tick каждую миллисекунду.
  Нам важно видеть рыночное состояние вокруг сделок и инцидентов.

10.2 backtest / replay_runner.py
- Сделать модуль backtest/replay_runner.py, который:
  - берёт historical snapshot из data/market_history/,
  - прогоняет стратегию (например spread arb),
  - эмулирует входы/выходы,
  - оценивает pnl_from_spread / funding / rebate,
  - считает drawdown и hit rate.
- Это НЕ должен быть просто файл с TODO. Этот раннер должен реально:
  - принимать какую стратегию гоняем,
  - выдавать структуру результата (pnl, max_drawdown, winrate),
  - и иметь pytest, который проверяет что раннер вообще запускается на простом моковом наборе истории.

10.3 Forensic replay после P0
- Когда фиксируется P0 incident, мы должны иметь возможность воспроизвести последние N минут:
  - Какие рыночные данные видел код,
  - Какое решение стратегия предложила,
  - Что сказал risk_check,
  - Как исполнил executor.
- Для этого на момент P0 incident мы должны сериализовать в data/incidents_forensic/<incident_id>/:
  - snapshot входных данных стратегии,
  - её plan/propose_trade(),
  - результат risk_check(),
  - шаги исполнения из executor (включая hedge_latency_ms).
- В incidents_log для этого P0 надо сохранить ссылки на эти артефакты.

- Dashboard /api/ui/incidents для P0 должен показывать: "forensic data captured: yes/no".


================================================================================
11. SECRETS / MFA / SECURITY_NOTES.md
================================================================================

TASK 2 уже запрещает утечки ключей в UI и ops_report.
Теперь усиливаем хранение секретов и вводим MFA как доктрину.

11.1 secrets_store
- Сделать шифрованное хранилище секретов (AES-256 локально).
- Ключ расшифровки подаётся через ENV/systemd файл с chmod 600 (не в git).
- Все вызовы get_secret() обязаны:
  - НЕ логировать значение,
  - но логировать сам факт доступа в audit_log (action="access_secret", which_key="binance_futures_api_key", actor="system").
- В audit_snapshot/ops_report никогда не показывать сами ключи, только имена.

11.2 MFA
- Для самых критичных действий:
  - kill-switch,
  - promote_standby (перевод DR в лидера),
  - снятие HOLD после P0,
  - выключение safe_mode в live.
- Уже есть two-man rule (TASK 2).
  Теперь добавь второй фактор подтверждения: APPROVE_TOKEN_2FA (как минимум отдельный статический секрет/пин, не тот же токен что аутентифицирует оператора).
- Флоу:
  - оператор A создаёт запрос (HOLD->RESUME, promote_standby, kill-switch release, disable safe_mode),
  - оператор B подтверждает и указывает APPROVE_TOKEN_2FA,
  - только после этого действие считается выполненным.
- Это должно логироваться в audit_log и попадать в ops_report как "pending approvals / pending critical actions".

11.3 SECURITY_NOTES.md
- Создать SECURITY_NOTES.md и описать:
  - роли viewer/auditor/operator,
  - почему viewer и auditor read-only,
  - почему operator требует двухшаговые подтверждения,
  - почему нужен второй оператор,
  - почему нужен второй фактор (2FA),
  - где и как хранятся ключи бирж,
  - почему прямой bypass (снять HOLD в один клик) категорически запрещён,
  - политика split_brain и KILL,
  - ответственность оператора за нарушение.

Отразить, что любое нарушение (ручной обход процессов безопасности, попытка влезть напрямую в runtime_state без подписи) = P0 + HOLD + дисциплинарка. Это не шутка, это проп-деск.


================================================================================
12. ДОКУМЕНТАЦИЯ / RUNBOOK / DEPLOY
================================================================================

TASK 1 уже потребовал обновить README.md и OPERATOR_RUNBOOK.md.
Нужно расширить документацию до полноты проп-деска.

12.1 OPERATOR_RUNBOOK.md (дополнить)
- Добавить инструкции по:
  - P0/P1/P2 и что с ними делать.
  - Что такое postmortem_root_cause и почему без него нельзя в RESUME.
  - Как читать /api/ui/status/overview, /components, /slo.
  - Как реагировать на "TRADE LOCKED: POSTMORTEM REQUIRED".
  - Как использовать promote_standby (шаги, approvals, MFA).
  - Как безопасно дернуть kill-switch и что после него.
  - Как читать autopilot_status и что значит "blocked_by_unresolved_P0" или "blocked_by_stress".
  - Как действовать при split_brain_detected ("всегда KILL").

12.2 README_DEPLOY.md (новый)
- Создать README_DEPLOY.md:
  - как поднять основной сервис (лидер),
  - как поднять standby сервис (dry_run),
  - systemd юниты (propbot.service и propbot-standby.service),
  - где должны лежать env файлы (права 600),
  - как работает canary rollout / rollback:
    * выкладка в /opt/propbot/releases/<timestamp>/
    * проверка canary (/healthz, /live-readiness, smoke-тесты pytest)
    * переключение симлинка /opt/propbot/current
  - как проверить /live-readiness перед выкладкой.

12.3 /live-readiness endpoint
- Реализовать /live-readiness (GET) возвращающий 200 ТОЛЬКО если:
  - recon.status == OK,
  - нет активного P0 без postmortem_root_cause,
  - нет split_brain_detected,
  - не пробит daily_loss_cap,
  - control_state.mode == RESUME,
  - safe_mode допустим по политике,
  - нет memory_pressure_flag критического уровня.
- Если /live-readiness != 200 → canary НЕ должен выкатываться в прод.
- Описать это поведение в README_DEPLOY.md.

12.4 GAP_REPORT.md
- Создать/обновить GAP_REPORT.md (или аналогичный файл) как "текущие отличия от целевого уровня проп-деска".
  - Список вещей которые всё ещё не покрыты или в процессе.
  - Это поможет видеть прогресс завершения всех требований TASK 3.


================================================================================
13. ФИНАЛЬНЫЙ РЕЗУЛЬТАТ ПОСЛЕ TASK 3
================================================================================

После выполнения TASK 3 система должна уметь:

1. Ломаться безопасно:
   - Любая серьёзная проблема (P0) → авто-HOLD,
     пишется incidents_log,
     autopilot сам не перезапускает торги,
     RESUME запрещён без postmortem + two-man rule + MFA.
   - Есть kill-switch, который выводит нас в KILL (форсированная ликвидация риска).
   - Есть hedge_flatten(), и она НЕ заглушка: она реально закрывает все экспозиции.

2. Защищать капитал активно:
   - Risk governor следит за глобальными лимитами по notional, venue, symbol, плечу и дневному лосс капу.
   - Circuit breaker автоматически останавливает конкретные стратегии при бешеной волатильности / плохом канале данных.
   - Stress-тест предупреждает заранее, если гипотетический гэп нас убьёт.

3. Подтверждать, что мы реально рыночно-нейтральны:
   - recon сверяет наши позиции с биржами и проверяет отсутствие незахеджированных хвостов.
   - Любой рассинхрон = P0 + HOLD.
   - Это пишется в incidents_log и видно оператору.

4. Жить с DR-режимом:
   - Есть standby-нода, которая постоянно в dry_run,
     готова стать лидером через promote_standby (two-man rule + MFA).
   - Есть split_brain защита: если два лидера одновременно → KILL.
   - Dashboard и /api/ui/status/overview показывают leader_id, standby_id, split_brain_detected.

5. Быть наблюдаемой как проп-деск:
   - Есть /api/ui/status/overview, /components, /slo, /incidents, /ops_report.
   - Везде отражены реальные данные: бюджеты стратегий, их фризы, drawdown, circuit breaker, autopilot статус, P0/P1/P2.
   - WebSocket /api/ui/stream/status пушит критические события в реальном времени.

6. Иметь forensic культуру:
   - Каждый P0 сохраняет forensic snapshot входных данных стратегий и шагов исполнения.
   - Есть backtest/replay_runner.py для анализа стратегий на историческом рынке.
   - Есть data/market_history/ с сохранёнными снапшотами (midprices/spreads/funding/basis/std_spread/ws_gap_ms_p95 и т.д.).

7. Быть операционно оформленной:
   - Есть OPERATOR_RUNBOOK.md с реальными процедурами ночного дежурства (включая postmortem).
   - Есть README_DEPLOY.md для production выкладки и canary.
   - Есть SECURITY_NOTES.md с правилами ролей, two-man rule, MFA, хранением secrets_store.
   - Есть GAP_REPORT.md как карта оставшихся дыр.

Итого:
- TASK 1 дал "ядро не врёт": бюджеты, фризы, автопилот, ops_report, dashboard.
- TASK 2 дал "governance": роли, two-man rule, audit.
- TASK 3 закрывает последние критические блоки из нашей большой V3-спеки:
  аварийная защита капитала, DR/standby, split-brain, инцидент-лог с постмортемом,
  SLA на хедж, стресс-тесты риска, circuit breakers, forensic, canary/live-readiness,
  MFA поверх two-man rule.

После TASK 3 ты реально не «просто бот».
Это маленький проп-деск уровня фонда.
