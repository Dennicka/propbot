# PropBot Architecture

Кодовая база PropBot построена вокруг чёткой цепочки от стратегий до биржевых
адаптеров и жёстких операционных guard'ов. Этот документ фиксирует структуру,
важные модули и контрольные точки, которые необходимо знать ревьюеру или
оператору.

## Основной поток исполнения

```
Стратегии → Router → Risk → Execution adapters → Биржи
                       ↘︎ Guards ↗︎
```

1. **Стратегии и планировщик.** Модули в `app/strategy` и orchestrator
   `app/strategy_orchestrator.py` собирают рыночные сигналы и формируют набор
   целевых ордеров. Планировщик выдаёт структуру, которую downstream-компоненты
   приводят к конкретным заявкам. 【F:app/strategy_orchestrator.py†L30-L148】
2. **Маршрутизация.** `app/router/order_router.py` нормализует параметры ордера,
   выбирает площадку (включая smart-router), выполняет pre-trade проверки и
   передаёт заявку на исполнение. Роутер используется как REST API, так и
   стратегиями. 【F:app/router/order_router.py†L72-L375】
3. **Risk-gate и trading profiles.** `app/services/risk.py` и
   `app/risk/accounting.py` применяют caps из `app/config/trading_profiles.py`
   и динамических лимитов (notional, дневной стоп, runaway, recon). Если хотя бы
   один guard нарушен, ордер блокируется, а система переходит в HOLD. 【F:app/services/risk.py†L47-L314】【F:app/risk/accounting.py†L320-L399】【F:app/config/trading_profiles.py†L13-L104】
4. **Биржевые адаптеры.** Пакет `app/broker` инкапсулирует клиентов Binance/OKX
   с явными таймаутами, логированием и защитами от повторных попыток.
   Низкоуровневые REST/WebSocket операции живут в `app/exchanges/*` и могут быть
   заменены на симуляторы в SAFE_MODE. 【F:app/broker/binance.py†L30-L556】【F:app/exchanges/binance_um.py†L60-L191】【F:app/exchanges/okx_perp.py†L63-L189】
5. **Safe mode и HOLD.** Управление состоянием (`RUN`, `HOLD`, `SAFE_MODE`) и
   двухоператорный резюм реализованы в `app/services/runtime.py`,
   `app/services/autopilot_guard.py` и `app/services/safe_mode.py`. Любой guard
   (risk, watchdog, recon, health) может включить HOLD, пока оператор не решит
   проблему вручную. 【F:app/services/runtime.py†L45-L260】【F:app/services/autopilot_guard.py†L70-L172】【F:app/services/safe_mode.py†L20-L134】

## Observability и тестирование

* **Golden-master.** Сценарии `tests/golden` фиксируют эталонные торговые потоки
  и гарантируют отсутствие регрессий в расчётах и risk-гейтах. Прогоняется как
  отдельная CI-джоба `golden-master` и локально через `pytest -q tests/golden`.
* **Readiness & recon.** Watchdog `app/watchdog/broker_watchdog.py` и recon
  `app/services/recon_runner.py` оценивают задержки, сверку и состояние бирж.
  Их статусы попадают в `/api/ui/status`, Prometheus и блокируют торговлю при
  деградациях. 【F:app/watchdog/broker_watchdog.py†L35-L190】【F:app/services/recon_runner.py†L25-L152】
* **CI/CD.** Основной пайплайн `ci.yml` запускает lint, unit/integration tests,
  golden-master, security sweep, self-check и acceptance. Отдельный workflow
  `Docker Release / build` собирает мультиарх образ. Все проверки должны быть
  зелёными перед merge. 【F:.github/workflows/ci.yml†L1-L165】【F:.github/workflows/docker-release.yml†L1-L52】

## Внешние интерфейсы

* HTTP API на FastAPI (`app/main.py`, `app/routers`) обслуживает `/api/ui/*` для
  операторов и `/api/public/*` для внешних интеграций.
* WebSocket сервер `app/server_ws.py` выдаёт агрегированный статус, позиции и
  алерты UI.
* CLI `scripts/run_profile.py` переключает paper/testnet/live профили и задаёт
  безопасные env-переменные. 【F:scripts/run_profile.py†L1-L148】

## Ключевые артефакты конфигурации

* `configs/profile.<profile>.yaml` описывает брокеров, risk caps и флаги guard'ов.
  Загрузчик `app/profile_config.py` валидирует значения и обеспечивает наличие
  секретов перед запуском. 【F:app/profile_config.py†L15-L191】
* `configs/config.<profile>.yaml` содержит оркестровку сервисов, расписания,
  политики recon/watchdog и подключается через `app/config/loader.py` с Pydantic
  схемой (`app/config/schema.py`). 【F:app/config/loader.py†L15-L63】【F:app/config/schema.py†L1-L160】

Эти элементы образуют полный runtime-контур: стратегии генерируют сделки,
router и risk их фильтруют, адаптеры исполняют на биржах, а guard'ы и CI
заставляют систему останавливаться при малейших отклонениях.
