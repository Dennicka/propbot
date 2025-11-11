# Security

Ниже перечислены правила обращения с секретами и требования к коду, которые
обязаны соблюдаться для paper/testnet/live окружений.

## Хранение ключей и доступов

* **Секреты только вне репозитория.** API ключи Binance/OKX, `APPROVE_TOKEN` и
  Telegram токены хранятся в JSON-файле из `SECRETS_STORE_PATH` или в GitHub
  Secrets (для CI). Значения в `.env.example` и `.env.prod.example` — это лишь
  подсказки/плейсхолдеры, их нельзя коммитить с боевыми значениями.
* `app.secrets_store.SecretsStore` поддерживает XOR/base64 обёртку через
  `SECRETS_ENC_KEY`. Ключ шифрования задаётся только через окружение контейнера и
  не попадает в git. 【F:app/secrets_store.py†L12-L148】
* Для `PROFILE=live` self-check и `startup_validation` требуют наличие всех
  секретов, перечисленных в `configs/profile.live.yaml` (binance/okx key/secret/
  passphrase). Без этого сервис не стартует. 【F:app/profile_config.py†L123-L191】【F:app/startup_validation.py†L88-L159】

## Кодовые ограничения

* **Без логирования секретов.** Любые строки с ключами/токенами проходят через
  redaction в `app/security.py` и должны логироваться только в masked-формате.
  В логах запрещены прямые выводы `API_TOKEN`, `passphrase`, `secret` и т.п.
* **Только Decimal для денег.** Модули risk/accounting/ledger используют
  `decimal.Decimal` для расчётов; новые функции обязаны следовать этому правилу,
  чтобы избежать накопления плавающих ошибок. 【F:app/risk/core.py†L33-L121】
* **Явные таймауты.** REST/WebSocket клиенты выставляют таймауты и повторные
  попытки. Новые вызовы должны использовать существующие врапперы (`httpx`,
  `requests`) с `timeout=...`, запрещено оставлять сетевые вызовы по умолчанию.
* **Никакого eval/exec.** `tests/test_code_hygiene.py` блокирует появление
  `eval`, `exec`, `subprocess.Popen(shell=True)`, небезопасных исключений и
  отсутствующих таймаутов. Любое отклонение ломает CI. 【F:tests/test_code_hygiene.py†L1-L183】
* **Только timezone-aware время.** Используйте `datetime.now(timezone.utc)` и
  `pendulum`-помощники, запрещены наивные `datetime.now()` — на это есть
  проверки в коде и тестах (`test_security_sweep.py`).

## Pipeline и проверки

* GitHub Actions workflow `security-sweep` запускает `bandit` и `pip-audit` для
  статического анализа и проверки зависимостей. Любая уязвимость или небезопасное
  использование API блокирует merge. 【F:.github/workflows/ci.yml†L69-L119】
* Скрипт `scripts/ci_secret_scan.py` выполняется на раннем этапе (`secret-scan`)
  и ищет ключи/пароли высокой энтропии. Любой матч требует ручной проверки перед
  продолжением пайплайна. 【F:.github/workflows/ci.yml†L9-L24】
* `python -m app.services.self_check` интегрирован в CI и проверяет профиль,
  переменные окружения, лимиты и наличие секретов до запуска acceptance.

## Практические рекомендации

1. При подготовке релиза заполняйте `.env.prod` только на целевом хосте и
   добавляйте его в `.gitignore`.
2. Регулярно ротируйте ключи и отмечайте дату в секции `meta` secrets store —
   `SecretsStore.needs_rotation` подскажет о просроченных ключах. 【F:app/secrets_store.py†L150-L214】
3. Никогда не отправляйте логи с секретами в внешние системы; для отладок
   используйте redact-инструменты и проверяйте, что safe-mode включён.
4. Перед любым live-выкатом убедитесь, что self-check, `make verify` и весь CI
   зелёные — это единственный допустимый путь к merge.

Соблюдение этих правил критично: нарушение любого пункта приравнивается к блокеру
для merge или запуска бота на реальные деньги.
