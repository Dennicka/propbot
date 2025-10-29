# PropBot vs spec_archive GAP Report

## [ok] Реализованные элементы
- **Авто-хедж и кросс-биржевой арбитраж**: `AutoHedgeDaemon` запускает сканер возможностей и вызывает `execute_hedged_trade`, сохраняя сделки в сторах позиций и журналах, что обеспечивает базовый цикл кросс-биржевых сделок с учётом HOLD/SAFE_MODE и dry-run-флагов. 【F:app/auto_hedge_daemon.py†L15-L195】【F:services/cross_exchange_arb.py†L1-L210】【F:positions/__init__.py†L1-L88】
- **Риск-лимиты и runaway guard**: Проверки `can_open_new_position`, лимиты governor'а по дневному убытку, экспозиции, clock skew и maintenance-флагам автоматически ставят HOLD и обновляют runtime-снимок. 【F:services/risk_manager.py†L1-L73】【F:app/risk_governor.py†L1-L213】
- **HOLD / SAFE_MODE / DRY_RUN**: Состояние `ControlState` хранит флаги HOLD, SAFE_MODE, DRY_RUN_MODE и другие параметры цикла, которые видны через API и UI и сохраняются в runtime store. 【F:app/services/runtime.py†L106-L213】【F:app/services/status.py†L566-L618】
- **Two-man approval guard**: Резюмирование и брокерская маршрутизация проверяют `two_man_rule`, требуя двух подтверждений перед реальной торговлей, а UI показывает, требуется ли второе одобрение. 【F:app/broker/router.py†L39-L215】【F:app/services/runtime.py†L136-L161】【F:app/services/status.py†L588-L613】
- **Аудит трейдов и операций**: Хедж-журнал `hedge_log`, истории PnL и оповещений, а также runtime snapshot (`runtime_state_store`) сохраняют подробные записи для расследований. 【F:app/services/hedge_log.py†L1-L64】【F:services/daily_reporter.py†L1-L80】【F:app/runtime_state_store.py†L1-L60】
- **Персистентность partial hedges и reconciler**: Позиции и ноги хеджа пишутся в стораже, а reconciler фиксирует расхождения и блокирует новые сделки до ручного вмешательства. 【F:positions/__init__.py†L1-L120】【F:services/reconciler.py†L1-L140】
- **Runtime state store с безопасным рестартом**: Снапшоты runtime читаются/пишутся в `data/runtime_state.json`, что позволяет возобновить работу после рестарта без потери статуса. 【F:app/runtime_state_store.py†L1-L60】【F:app/services/runtime.py†L215-L364】
- **Операторская панель `/ui/dashboard` и API**: HTML-дэшборд и JSON endpoints дают доступ к статусу, формам HOLD/RESUME/kill и метрикам. 【F:app/routers/dashboard.py†L1-L120】【F:app/services/operator_dashboard.py†L520-L735】
- **Risk snapshot сервис**: модуль `app/risk_snapshot.py` агрегирует позиции, экспозицию и статусы автопилота в единый снимок риска для UI и других потребителей. 【F:app/risk_snapshot.py†L1-L74】
- **Realtime риск-сводка в панели**: `/ui/dashboard` теперь встраивает risk snapshot, отображая суммарный notional, разбивку по биржам и outstanding partial hedges, что приближает нас к требованиям spec_archive по много-биржевому мониторингу и подготовке VaR/RPI. 【F:app/services/operator_dashboard.py†L520-L735】
- **Централизованный план оркестратора**: `/ui/dashboard` выводит `compute_next_plan()` из глобального оркестратора, показывая решение по каждой стратегии и статус risk-gates. Это закрывает требования spec_archive про многостратегийный контроль, единый scheduler и прозрачные risk gates для мультивеню арбитража без ручного запуска. 【F:app/services/operator_dashboard.py†L520-L760】【F:app/orchestrator.py†L137-L221】
- **RBAC для операторов**: токены в `SecretsStore` содержат роль (viewer/operator), критические действия (HOLD / RESUME / KILL / raise-limits) доступны только оператору и логируются в `audit_log`, а HTML-дэшборд показывает имя и роль и скрывает опасные элементы от viewer. Это закрывает часть секьюрити-требований spec_archive по операционной сегрегации ролей. 【F:app/routers/dashboard.py†L1-L140】【F:app/services/operator_dashboard.py†L520-L735】
- **Healthz и build metadata**: `/healthz` возвращает `{"ok": true}`, а статус и UI показывают `build_version`, что подтверждает готовность сервисов. 【F:app/main.py†L86-L128】【F:app/services/status.py†L83-L182】

## [missing] Нереализованные требования из spec_archive

### Execution Layer
- **Persistent order outbox и Exactly-once состояние**: требуется устойчивый журнал ордеров со статусами NEW→SENT→ACK→... и механизм reconcile при рестарте. Логично внедрить в слой исполнения (`services/cross_exchange_arb.py`, `app/broker/*`) с хранением в БД/таблице и эскалацией инцидента при пропавшем ACK. Риск-требование: блокировка стратегии и тревога `HEDGE_FAILURE`.
- **Smart Order Router с многовеновым сплитом**: спецификация требует динамического сплита объёма по нескольким площадкам с учётом ликвидности/физерайтов. Это следует расширить в `services/execution_router.py`, добавив агрегацию книги и пост-онли маршрутизацию; алерты — деградация SOR или провал по ликвидности.
- **Adaptive order sizing по волатильности/ликвидности**: текущий размер задаётся окружением; нужно обогащение в `services/cross_exchange_arb.py`/`edge_guard` с расчётом волатильности и auto-throttle. При превышении риска — HOLD.
- **Partial hedge fallback на коррелированные инструменты**: нет логики попытки альтернативных ног (PERP vs SPOT). Добавлять в `services/cross_exchange_arb.py`, с алертами `FALLBACK_HEDGE_USED` и HOLD при неудаче.
- **Strict hedge deadlines & stale trade detector**: Требуется отслеживать дедлайны (например 120 мс) и маркировать сделки как STALE с последующим HOLD. Встроить в исполнение и авто-хедж цикл, эскалировать через incident.
- **Trade frequency limiter с auto-disable стратегии**: хотя есть глобальные лимиты, нет отключения конкретной стратегии/сканера и инцидента `POSSIBLE_LOGIC_LOOP`. Нужно расширить `register_order_attempt` и авто-хедж для деактивации и алерта.
- **Latency edge / latency arbitration**: отсутствует анализ лент и опережающей площадки; интегрировать в сканер/маркетдату с предупреждениями при деградации.
- **Dead Order Hunter**: нет периодического REST-опроса и автоматического разрешения зомби-ордеров; добавить сервис `services/dead_order_hunter.py` с тревогой `ZOMBIE_ORDER` и принудительным SAFE_MODE.

### Risk & Capital Layer
- **Adaptive risk throttle и per-strategy дневные стопы**: сейчас лимиты глобальные, без метрик по стратегиям. Нужно модуль аллокации риска в `services/adaptive_risk_advisor.py` с учётом PnL по стратегиям, переводящий отдельные стратегии в HOLD и логирующий `STRATEGY_PNL_STOP`.
- **VaR и stress-testing**: отсутствует VaR-оценка и стресс-тесты ликвидности/маржин. Следует добавить `risk/var_engine` и `stress_tests` с расчётом по snapshot портфеля (`app/services/portfolio`), плюс алерты `VAR_LIMIT` и `STRESS_FAIL`.
- **Exposure caps per asset и concentration guard**: хотя снимок экспозиции строится, нет применения лимитов по символам/стратегиям. Внедрить enforcement в `risk_governor.evaluate`/`edge_guard`, с блокировкой новых сделок и HOLD.
- **Market regime detector и circuit breaker**: нет классификации волатильности/тревожных режимов. Требуется сервис, влияющий на режим (MONITOR/REDUCED) и RPI, с предупреждениями `MARKET_CHAOS`.
- **Capital health score**: отсутствует агрегированная оценка 0–100; добавить в runtime snapshot и UI, использовать в алертах и решениях RPI.

### Strategy Orchestration
- **Плагинная модель стратегий**: текущий код фиксирован на одном кросс-арбитраже. Нужно абстракции для `strategy` модулей с интерфейсом `get_signal/execute`, управляемые диспетчером.
- **Capital allocator и рейтинги стратегий**: отсутствует распределитель капитала, sandbox/canary lifecycle и auto-retirement. Следует реализовать планировщик в новом модуле, интегрировать с runtime и UI, плюс логирование переходов (ALERT `SANDBOX_PROMOTE`/`RETIRE_STRATEGY`).
- **External trader sandbox / revenue share**: нет разделения пулов капитала и расчёта долей. Требует отдельного сервиса и авторизационных guard'ов.

### Market Intelligence
- **Multi-venue book aggregation и ликвидити-хитмапы**: нет агрегатора стаканов, оценки глубины и fee-adjusted лучших котировок. Добавить в `app/services/marketdata.py` и хранить в runtime для SOR.
- **Exchange quality scoring, latency/connectivity map**: отсутствуют метрики RTT, WS стабильности, деградации. Нужно фоновый мониторинг и вывод в UI/MSR.
- **Volatility / toxic flow / wash-trading detectors и MSR**: нет аналитики потоков, токсичности и фейковых объёмов. Требуется аналитический слой с алертами `TOXIC_FLOW`, `MARKET_STABILITY_RED`.
- **Venue economics / fee negotiation stats**: нет накопления объёма по площадкам; добавить отчётчик в `execution_stats_store` и отчёт в daily digest.

### Monitoring & Incidents
- **Heartbeats для всех модулей**: кроме авто-хеджа нет watchdog'ов. Нужно централизованный heartbeat registry, который при таймауте переводит режим в REDUCED и создаёт инцидент.
- **Self-check loop & canary session**: отсутствует автоматическое тестирование connectivity при старте и периодические self-audits. Добавить фоновую задачу, логировать `CANARY_FAIL` и блокировать RPI.
- **Maintenance mode**: нет отдельного состояния планового окна; добавить переключатель в runtime и UI с уважением алертов.
- **Structured incident reports & multi-channel escalation**: сейчас оповещения плоские; необходимо структурированные отчёты с reason-кодами и маршрутизацией (INFO/WARN/CRITICAL/PAGE_NOW) на Telegram/email.
- **Session timeline / trade replay / drawdown timeline**: отсутствуют интерактивные отчёты и визуализации; расширить UI и хранилище историй.
- **End-of-day wrap / Night summary**: хотя есть daily_reporter, нет автоматического wrap-up с flattening/архивом и рассылаемым summary.

### Governance / Compliance
- **RBAC роли и signed configs**: двухоператорная схема есть, но отсутствует ролевая модель, подписи конфигов и аудит изменений. Нужно ввести роли (Operator/Reviewer/Viewer), хранение подписей и журнал изменений.
- **Withdrawal whitelist и capital partitions**: нет политики вывода средств и сегментации капиталов; реализовать в сервисах управления балансами, с двухэтапным одобрением и алертами `WITHDRAWAL_REQUEST`.
- **Investor-safe reporting & legal freeze**: добавить экспорт отчётов, возможность freeze логов по запросу compliance.

### Security & Infrastructure
- **Secrets management и key rotation**: секреты сейчас читаются напрямую из `.env`. Нужен менеджер секретов/шифрование, плановая и emergency-ротация с автоматическим HOLD.
- **Brute-force lockout и auth hardening**: отсутствует трекинг неудачных логинов и блокировка токенов; добавить в auth middleware и ops-alertы.
- **Build integrity & rollback**: нет проверки хэшей и автоматического rollback. Требуются подписи артефактов и команды быстрого возврата.
- **Hardware/VPS health мониторинг**: добавить сбор CPU/memory/disk/temp, clock drift и реакции (REDUCED/HOLD + алерты).
- **Network/DDoS resilience и DIRTY_START detection**: внедрить метрики RTT/packet loss, failover на пассивный узел и автоматическое маркирование DIRTY_START при расхождениях балансов, с блокировкой запуска.
- **Secure archival**: отсутствуют хэшированные архивы инцидентов/отчётов; нужно реализовать планировщик архивирования и валидацию целостности.

### DRPC / RPI Layer
- **Global Risk Profile Index**: нет RPI шкалы 0–100, health gating и rate-of-change ограничений. Требуется добавить сущность в runtime, UI и контроль risk governor'ом, с incident `RPI_OVERRIDE_DENIED` при нарушении условий.
- **Audit trail for RPI changes**: вводить журнал с оператором, комментариями и MSR.

### Human Control Layer / Autopilot
- **Autopilot / Assisted / Manual режимы**: отсутствует UI и логика переключения режимов с разными полномочиями и автоматикой.
- **AFK/ночной контроль и What-if симулятор**: нет трекинга присутствия оператора и превентивного снижения риска. Нужно интегрировать в dashboard и risk governor с предупреждениями и автоблокировкой повышения риска во сне.
- **Plain-language status strip и tooltips**: UI не предоставляет детальные подсказки и пояснения, требуемые спецификацией.
- **Night-safe autopilot guarantee**: добавить nightly policies, авто-отчёт и проверку условий безопасного автономного режима.

Эти пробелы покрывают все обязательные блоки спецификации; для запуска на реальных деньгах в 24/7 режиме необходимо спланировать реализацию перечисленных функций с приоритетом на Execution/Risk/Security слои.
