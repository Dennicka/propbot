# Go-Live Checklist

Финальный чеклист перед переключением PropBot в live-режим. Выполняй пункты
последовательно — команда `make run_live` остановится, если профиль настроен
небезопасно.

## 1. Запуск профиля

1. **Запусти сервис через профиль.** Используй цели `make run_paper`,
   `make run_testnet` и `make run_live` — они прокидывают стандартные флаги и
   вызывают единый CLI-энтрипоинт. `run_live` проверяет лимиты и флаг
   `LIVE_CONFIRM=I_KNOW_WHAT_I_AM_DOING`. 【F:Makefile†L33-L51】【F:scripts/run_profile.py†L1-L99】
2. **Проверь логи старта.** При загрузке приложения выводится активный профиль,
   лимиты и состояние guard’ов (SLO, partial/auto hedge, recon, watchdog). Это
   первый индикатор того, что система увидела правильные флаги. 【F:app/main.py†L62-L109】

## 2. Secrets и доступы

1. **Убедись, что secrets-store на месте.** `ensure_live_prerequisites` проверяет
   наличие `SECRETS_STORE_PATH` и что в JSON-файле заполнены ключи Binance и OKX;
   при отсутствии — запуск live профиля блокируется. 【F:app/config/profiles.py†L139-L185】
2. **Two-man approvals.** В стартовом состоянии `SAFE_MODE=true`, `DRY_RUN_MODE=true`
   и HOLD активен — снять их можно только после двух approvals. 【F:app/config/profiles.py†L94-L118】【F:app/main.py†L96-L109】

## 3. Risk & лимиты

1. **Проверь профильные лимиты.** Конфиг `configs/profile.live.yaml` задаёт
   notional caps, drawdown/daily loss и health thresholds — сравни с текущими
   лимитами по аккаунтам. 【F:configs/profile.live.yaml†L1-L69】
2. **Проверь env-лимиты.** Убедись, что `MAX_TOTAL_NOTIONAL_USDT`,
   `MAX_OPEN_POSITIONS` и `DAILY_LOSS_CAP_USDT` заданы (>0). `scripts/run_profile.py`
   блокирует запуск без этих значений. 【F:scripts/run_profile.py†L40-L99】【F:app/config/profiles.py†L154-L214】
3. **Runaway/daily loss.** Kill-caps и runaway breaker включены в live-конфиге,
   а daily loss контролируется guard’ом автопилота — не меняй значения перед
   Go-Live без согласования. 【F:configs/profile.live.yaml†L10-L48】【F:app/services/autopilot_guard.py†L35-L145】

## 4. Guard’ы и наблюдаемость

1. **SLO / Health / Recon / Watchdog.** Для live профиля CLI требует активных
   `FEATURE_SLO`, account health guard, reconciliation runner и exchange
   watchdog. Без них запуск прервётся. 【F:app/config/profiles.py†L145-L185】
2. **Hedge guard.** Должен быть включён хотя бы один хедж-раннер: partial hedge
   (`HEDGE_ENABLED=true`) или авто-хедж (`AUTO_HEDGE_ENABLED=true`). Иначе CLI
   вернёт ошибку «Неактивен ни один hedge-guard». 【F:app/config/profiles.py†L166-L185】
3. **Readiness gate.** Для testnet/live по умолчанию включено ожидание readiness
   gate перед выходом из HOLD — не выключай до проверки сигналов. 【F:app/config/profiles.py†L108-L118】【F:app/main.py†L127-L165】

## 5. Мониторинг и Smoke

1. **Acceptance / smoke.** Перед снятием HOLD прогоните `make acceptance` либо
   `scripts/smoke.sh` против нового деплоя. 【F:Makefile†L18-L32】【F:scripts/smoke.sh†L1-L9】
2. **Dashboards и алерты.** Убедитесь, что Telegram-бот подключен и что на UI в
   разделе `/ui/dashboard` отображаются бейджи `auto_trade=OFF`, `watchdog=OK`,
   `recon=OK` (на старте все guard’ы должны быть зелёными). 【F:app/services/runtime_badges.py†L41-L81】【F:app/services/operator_dashboard.py†L2111-L2201】

## 6. CI и тесты

1. **CI guardrails.** Проверь, что workflow `.github/workflows/ci.yml` зелёный:
   secret scan, lint/type-check, pytest, acceptance и security-sweep должны
   пройти без `continue-on-error`. 【F:.github/workflows/ci.yml†L1-L120】
2. **Pytest и golden.** Локально прогоните `pytest -q` и `make golden-check` —
   golden сценарии обязаны совпадать с эталоном перед live. 【F:Makefile†L20-L44】

После успешного smoke-теста и подтверждения guard’ов можно снимать HOLD и
выключать DRY_RUN_MODE через стандартный two-man процесс.
