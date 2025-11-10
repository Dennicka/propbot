# PropBot Architecture

Этот документ описывает основные компоненты PropBot и их взаимодействие в
paper/testnet/live режимах.

## Высокоуровневая схема

```
Strategies → Router → Risk → Execution adapters → Exchanges
                    ↘︎ Guards ↗︎
```

1. **Стратегии.** Модули в `app/strategy` и `app/strategy_orchestrator.py`
   формируют торговые планы, рассчитывают приоритеты и бюджет по символам.
   Планировщик публикует структуру ордеров, которую downstream-компоненты
   приводят к конкретным биржевым заявкам. 【F:app/strategy_orchestrator.py†L30-L148】
2. **Маршрутизация.** `app/router/order_router.py` подбирает подходящую площадку,
   нормализует параметры и выполняет pre-trade проверки. Роутер работает как для
   ручного REST API, так и для оркестратора стратегий. 【F:app/router/order_router.py†L72-L375】
3. **Риск и гейты.** `app/risk/core.py` и сервисы в `app/services/risk.py`
   применяют лимиты экспозиции, дневные капы, auto-HOLD и метрики error-budget.
   Pre-trade gate (`app/risk/accounting.py`) мгновенно отклоняет заявки, если
   нарушен любой guard. 【F:app/services/risk.py†L47-L314】【F:app/risk/accounting.py†L320-L399】
4. **Биржевые адаптеры.** В каталоге `app/broker` реализованы клиенты Binance/OKX
   с явными таймаутами и логированием ошибок. Они инкапсулируют REST/WebSocket
   детали и выдают унифицированный ответ order-router'у. REST/Futures доступы
   описаны в `app/exchanges/binance_um.py` и `app/exchanges/okx_perp.py`. 【F:app/broker/binance.py†L30-L556】【F:app/exchanges/binance_um.py†L60-L191】【F:app/exchanges/okx_perp.py†L63-L189】
5. **Watchdog и recon.** Фоновые сервисы `app/watchdog/broker_watchdog.py` и
   `app/services/recon_runner.py` контролируют задержки, дисконнекты, сверку
   позиций и при необходимости переводят систему в HOLD. 【F:app/watchdog/broker_watchdog.py†L35-L190】【F:app/services/recon_runner.py†L25-L152】

## Где включены guard'ы

* **Pre-trade guard.** `app/risk/accounting.py` и `app/router/order_router.py`
  проверяют лимиты перед каждой заявкой.
* **Watchdog.** `app/watchdog/broker_watchdog.py` мониторит heartbeat бирж и
  активирует auto-HOLD при затяжных деградациях.
* **Health guard.** `app/risk/guards/health_guard.py` отслеживает маржинальные
  метрики и может перевести runtime в HOLD при критическом margin ratio.
* **Hedge guard.** `app/services/partial_hedge_runner.py` и
  `app/hedge/partial.py` контролируют остатки по кросс-биржевым позициям.
* **Recon.** `app/services/recon_runner.py` сравнивает runtime ledger и биржи,
  фиксирует расхождения и блокирует торговлю при критических ошибках.

## Runtime и внешние интерфейсы

* HTTP API реализовано на FastAPI (`app/main.py`, `app/routers`). Основные UI
  ручки обслуживают `/api/ui/*` для операторов и `/api/public/*` для внешних
  интеграций.
* Websocket сервер `app/server_ws.py` передаёт сводный статус UI, watchlist и
  уведомления об алертах.
* CLI в `scripts/run_profile.py`/`app/cli/main.py` запускает runtime в paper,
  testnet или live режимах и применяет профильные env-переменные.

## Фоновые сервисы

* `app/services/runtime.py` — главный цикл, который оркестрирует стратегии,
  risk accounting, recon, watchdog и оповещения.
* `app/services/autopilot_guard.py` — следит за дневным лимитом убытков и
  состоянием watchdog, переводит систему в HOLD при отклонениях.
* `app/services/ops_report.py` — формирует ежедневные отчёты (JSON/CSV) с
  метриками позиций, PnL и срабатываниями guard'ов.

Эти компоненты работают совместно: стратегии формируют заказы, router отправляет
их на биржи только после risk-check, guard'ы следят за состоянием системы, а
runtime и фоновые сервисы собирают телеметрию и автоматически блокируют торговлю
при угрозах.

