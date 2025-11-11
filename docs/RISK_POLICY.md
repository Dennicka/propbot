# Risk Policy

Документ фиксирует обязательные лимиты и автоматические срабатывания, которые
должны быть включены перед запуском PropBot в paper/testnet/live режимах.

## Trading profiles и notional caps

* Trading profile определяется `TRADING_PROFILE` или конфигами
  `app/config/trading_profiles.py`. Для каждого профиля заданы жёсткие лимиты на
  notional per order/per symbol/global и дневной убыток. 【F:app/config/trading_profiles.py†L13-L104】
* `configs/profile.<profile>.yaml` дополняет caps профиля атрибутами
  `risk_limits.max_single_position_usd`, `max_total_notional_usd`,
  `daily_loss_cap_usd`, `max_drawdown_bps`. Все значения должны быть > 0 и не
  противоречить лимитам trading profile (self-check проверяет порядок и границы).
* Для overrides по символам и venue используйте переменные окружения
  `MAX_POSITION_USDT__*`, `MAX_OPEN_ORDERS__*`, `MAX_NOTIONAL_PER_POSITION_USDT`.
  Они применяются risk-gate'ом в `app/risk/accounting.py`. 【F:app/risk/accounting.py†L83-L169】

## Daily loss и drawdown

* `DAILY_LOSS_CAP_USDT` и `DAILY_LOSS_CAP_ENABLED` активируют дневной стоп по
  реализованному PnL. При breach autopilot переводит систему в HOLD, а статус
  `daily_loss` отображается в UI и Prometheus. 【F:app/services/autopilot_guard.py†L70-L172】【F:app/services/runtime_badges.py†L34-L125】
* `risk_limits.daily_loss_cap_usd` в профильном YAML должен быть меньше или равен
  `daily_loss_limit` из trading profile. Это значение контролируется self-check
  и `startup_validation`.
* `risk_limits.max_drawdown_bps` задаёт относительный стоп (bps). Если дневной
  дроудаун превышает порог, risk governor инициирует HOLD. 【F:app/services/risk.py†L180-L314】

## Safe mode и HOLD

* `SAFE_MODE=true` и `DRY_RUN_MODE=true` обязательны для первого запуска на любом
  окружении. Ручной RESUME разрешён только после двухоператорного подтверждения.
* Любой guard (risk caps, recon, watchdog, health guard) может вызвать
  `engage_safety_hold(...)`, что фиксируется в audit логе и блокирует новые
  заявки. 【F:app/services/safe_mode.py†L20-L134】【F:app/services/risk_guard.py†L236-L276】
* Статус HOLD отображается в `/api/ui/status`, `/ui/dashboard`, Prometheus и
  Telegram оповещениях. Оператор обязан устранить причину и пройти self-check
  перед повторным RESUME.

## Self-check и стартовые требования

* Перед запуском необходимо выполнить `python -m app.services.self_check` для
  активного профиля. Чек проверяет лимиты, наличие секретов, конфиги и
  валидность ENV. При статусе `FAIL` запуск запрещён.
* `startup_validation.validate_startup()` вызывается при старте runtime и
  блокирует unsafe конфигурации: отсутствие `APPROVE_TOKEN`, нулевые caps,
  плейсхолдеры из `.env.prod.example`, несуществующие пути и live-режим без HOLD.
  【F:app/startup_validation.py†L15-L214】

## Политика безопасности торговли

* Торговля запрещена, если:
  - дневной лимит в состоянии breach;
  - recon сигнализирует расхождение выше допуска; 【F:app/services/recon_runner.py†L25-L152】
  - watchdog переводит биржу в состояние `DOWN` и включён auto-hold; 【F:app/watchdog/broker_watchdog.py†L35-L190】
  - self-check возвращает `FAIL` или `WARN` для критичных пунктов (секреты,
    environment, risk);
  - SAFE_MODE отключён без документированного окна обслуживания.
* Любые override лимитов документируются в runbook и требуют ручной двойной
  проверки двумя операторами.

Эта политика обеспечивает, что даже в live-режиме бот торгует только в рамках
чётко определённых лимитов и автоматически останавливается при малейших
отклонениях.
