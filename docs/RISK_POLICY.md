# Risk Policy

Документ описывает обязательные лимиты и правила risk-guards для запуска PropBot
в paper/testnet/live режимах.

## Дневной лимит убытков

* Значение задаётся через `DAILY_LOSS_CAP_USDT` (обязательно для live). Лимит
  хранится в `app/risk/daily_loss.py` и применяется как в risk accounting, так и в
  UI бейджах. 【F:app/risk/daily_loss.py†L74-L193】
* Нарушение лимита (`breached=true`) приводит к авто-HOLD и блокировке
  автоторговли. `app/services/autopilot_guard.py` и `app/risk/auto_hold.py`
  инициируют HOLD и записывают причину в audit лог. 【F:app/services/autopilot_guard.py†L70-L144】【F:app/risk/auto_hold.py†L26-L96】
* Метрика `propbot_daily_loss_breached` и endpoint `/api/ui/daily_loss_status`
  служат источником истины для дашборда и алертов. 【F:app/services/runtime_badges.py†L34-L125】【F:app/routers/ui.py†L571-L585】

## Совокупный ноционал и позиции

* Глобальный лимит открытого ноционала задаётся переменными
  `MAX_TOTAL_NOTIONAL_USDT`/`MAX_TOTAL_NOTIONAL_USD`. Значение требуется для
  live-профиля и проверяется при запуске (`scripts/run_profile.py`).
* В runtime лимит применяется в `app/risk/core.py` и `app/services/risk.py`.
  Нарушение приводит к отказу новых ордеров и потенциальному авто-HOLD после
  нескольких окон троттлинга. 【F:app/risk/core.py†L360-L463】【F:app/services/risk.py†L180-L314】
* Лимит на число одновременных позиций задаётся `MAX_OPEN_POSITIONS`. Значение
  используется в `app/services/risk_guard.py` и UI отчётах. 【F:app/services/risk_guard.py†L236-L276】【F:app/services/operator_dashboard.py†L408-L666】

## Health guard

* Health guard активируется профилем (`app/config/profiles.py`) и следит за
  маржинальными показателями через `app/risk/guards/health_guard.py`.
* При переходе в состояние `CRITICAL` guard переводит runtime в HOLD с причиной
  `ACCOUNT_HEALTH_CRITICAL`. Оператор обязан оценить маржинальные требования и
  вручную снизить нагрузку. 【F:app/risk/guards/health_guard.py†L112-L181】
* Для live запуска guard должен быть включён; `ensure_live_prerequisites`
  блокирует старт, если флаг отключён в конфиге или env. 【F:app/config/profiles.py†L154-L214】

## Watchdog и auto-HOLD

* Биржевой watchdog (`app/watchdog/broker_watchdog.py`) отслеживает задержки,
  дисконнекты и ошибки. При `state=DOWN` или превышении порогов с флагом
  auto-hold включается HOLD с причиной `EXCHANGE_WATCHDOG::<VENUE>::DOWN`.
* Autopilot guard (`app/services/autopilot_guard.py`) агрегирует состояния
  watchdog и дневного лимита, переводит runtime в HOLD и уведомляет операторов.
* Все авто-HOLD события отображаются в `/api/ui/status`, Prometheus и Telegram
  оповещениях. Перед ручным RESUME убедитесь, что первопричина устранена
  (например, восстановлен стрим или обновлена маржа).

## Операционные требования

* `LIVE_CONFIRM=I_KNOW_WHAT_I_AM_DOING` обязателен для `make run_live`.
* Перед RESUME убедитесь, что дневной лимит не в `BREACH`, watchdog в `OK`, а
  recon не сигнализирует расхождения. В противном случае auto-HOLD сработает
  повторно сразу после возобновления торговли.

