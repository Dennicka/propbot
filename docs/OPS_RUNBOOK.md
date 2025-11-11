# Operations Runbook

Пошаговые инструкции для оператора prop-бота. Всегда начинайте с paper режима и
двухоператорного контроля.

## 1. Подготовка окружения

1. Склонируйте репозиторий и создайте виртуальное окружение Python 3.11+.
2. Установите зависимости: `pip install -r requirements.txt`.
3. Заполните `.env` (для локального запуска) или `.env.prod` (для контейнера)
   значениями из `.env.example`. Все реальные ключи кладите в secrets store и
   указывайте путь через `SECRETS_STORE_PATH`.
4. Выполните `make verify` — цель запускает lint, pytest, golden, mypy и security
   проверки (bandit/pip-audit при наличии). Без зелёного `make verify` кода в PR
   быть не должно.
5. Запустите self-check: `python -m app.services.self_check --profile paper`.
   Self-check обязан вернуть `Overall: OK`. Если статус `WARN`/`FAIL`, устраните
   проблему до дальнейших действий.

## 2. Запуск профиля

### Paper

1. Убедитесь, что `PROFILE=paper`, `TRADING_PROFILE=paper`, `SAFE_MODE=true`,
   `DRY_RUN_MODE=true`, `AUTH_ENABLED=false` (для локального теста).
2. Запустите `make run_paper` или `python scripts/run_profile.py --profile paper`.
3. Проверьте `/healthz` и `/api/ui/status`. Ожидаемые бейджи: `safe_mode=true`,
   `hold_active=true`, `dry_run_mode=true`.

### Testnet

1. Заполните testnet-ключи в secrets store (`binance_key`, `okx_key`, `passphrase`).
2. Выполните self-check: `python -m app.services.self_check --profile testnet`.
3. Запустите `make run_testnet`. Сервис должен стартовать в HOLD.
4. Проверьте, что recon и watchdog зелёные, а дневной лимит не в breach.

### Live

1. Подготовьте secrets store с боевыми ключами и включённым SAFE_MODE.
2. Self-check: `python -m app.services.self_check --profile live`.
3. `scripts/run_profile.py --profile live` требует переменные
   `LIVE_CONFIRM=I_KNOW_WHAT_I_AM_DOING`, ненулевые caps (`MAX_TOTAL_NOTIONAL_USDT`,
   `MAX_OPEN_POSITIONS`, `DAILY_LOSS_CAP_USDT`) и подтверждённый APPROVE_TOKEN.
4. После старта убедитесь, что сервис в HOLD. Снимайте HOLD только через
   двухшаговый `/api/ui/resume-request` и `/api/ui/resume-confirm` с разными
   операторами.

## 3. Действия при инцидентах

### Тесты или golden-master упали

1. Соберите локально: `make verify`.
2. Посмотрите логи `pytest`/`tests/golden` на CI.
3. Исправьте корневую проблему, обновите snapshot (если golden ожидаемо изменился)
   и перезапустите CI. Без зелёного CI merge запрещён.

### Сработал risk-limit или дневной стоп

1. Проверить `/api/ui/status` и `runtime_badges` — там указана причина HOLD.
2. Зафиксировать событие в журнале и уведомить команду.
3. При необходимости закрыть позиции вручную на бирже.
4. После устранения причин (обнуление экспозиции, подтверждение лимитов)
   выполнить `self_check` и только затем инициировать двухшаговый resume.

### SAFE_MODE/HOLD включился автоматически

1. Причину ищите в `/api/ui/status/alerts` и Prometheus метриках.
2. Проверить watchdog (`/api/ui/watchdog`), recon (`/api/ui/recon`), health guard.
3. Если HOLD вызван деградацией биржи — дождитесь восстановления, затем выполните
   self-check и ручной resume.
4. Если HOLD вызван ошибкой конфигурации — вернитесь к `startup_validation` и
   устраните неправильный ENV/путь/лимит.

### Golden/acceptance рассинхронизированы

1. Сравните отчёты из `tests/golden` и `tests/acceptance`. Golden должен быть
   детерминированным; любые отличия требуют обновления baseline.
2. Для acceptance убедитесь, что тесты не гоняют хаос (`CHAOS_ENABLED=false`).
3. После фикса обязательно прогоните `pytest -q tests/golden` и `make acceptance`.

### Self-check вернул FAIL

1. Не запускайте runtime. Ознакомьтесь с выводом self-check — каждая строка
   `[FAIL]`/`[WARN]` указывает конкретную проблему (секреты, ENV, конфиги).
2. Исправьте ошибку и повторите `python -m app.services.self_check`. Только
   статус `Overall: OK` допускает запуск бота.

## 4. Ротация и обслуживание

* Раз в неделю проверяйте `SecretsStore.needs_rotation()` (есть CLI/endpoint) и
  обновляйте ключи, если срок > 30 дней.
* Экспортируйте отчёты `reports/ops_report*.json/csv` и `logs/*.log` перед
  плановым обслуживанием.
* Для обновления версии: `git pull`, `make verify`, `python -m app.services.self_check`,
  затем перезапуск контейнера через `docker compose up -d`.

Следуя этому runbook-у, оператор гарантирует безопасный старт и корректное
реагирование на инциденты в любом из профилей.
