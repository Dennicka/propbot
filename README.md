# PropBot

PropBot — это торговый бот с HOLD/safe-mode, golden-master тестами и строгими
risk-гейтами. Репозиторий готов к эксплуатации в paper/testnet/live режимах при
условии соблюдения проверки self-check и CI.

## Требования

* Python 3.11+
* `pip`, `virtualenv` или совместимый менеджер окружений
* Docker (для продового запуска или локальной сборки образа)

## Установка и локальный запуск

1. Клонируйте репозиторий и создайте виртуальное окружение:
   ```bash
   git clone https://github.com/your-org/propbot.git
   cd propbot
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -U pip wheel
   pip install -r requirements.txt
   ```
2. Скопируйте переменные окружения и заполните безопасными значениями:
   ```bash
   cp .env.example .env
   ```
   *Настоятельно рекомендуется хранить реальные ключи в JSON-файле
   `SECRETS_STORE_PATH` и не коммитить их в git.*
3. Подготовьте директории `data/` (runtime state, логи, отчёты) либо укажите
   собственные пути через переменные окружения.

## Локальная проверка перед PR

Запустите единый таргет:
```bash
make verify
```
Цель последовательно выполняет `ruff`, `black --check`, `mypy`, `pytest`,
`pytest -q tests/golden` и (при наличии) `pip-audit`/`bandit`. PR без зелёного
`make verify` не принимаются.

## Self-check

Перед запуском любого профиля выполните:
```bash
python -m app.services.self_check --profile paper
```
Команда проверяет профили, конфиги, переменные окружения, лимиты и доступность
эндпоинтов. При статусе `FAIL` запуск запрещён. Для testnet/live укажите нужный
профиль (`--profile testnet`, `--profile live`). Self-check встроен в CI и
служит финальной операционной проверкой перед стартом.

## Запуск профилей

```bash
python scripts/run_profile.py --profile paper
```
* Paper — `SAFE_MODE=true`, `DRY_RUN_MODE=true`, `AUTH_ENABLED=false`. Бот
  стартует в HOLD без реальных ордеров.
* Testnet — заполните тестовые ключи Binance/OKX в secrets store и убедитесь,
  что self-check зелёный. Запускайте `make run_testnet` или команду выше с
  `--profile testnet`.
* Live — требуется `LIVE_CONFIRM=I_KNOW_WHAT_I_AM_DOING`, ненулевые лимиты
  (`MAX_TOTAL_NOTIONAL_USDT`, `MAX_OPEN_POSITIONS`, `DAILY_LOSS_CAP_USDT`) и
  боевые ключи в secrets store. Self-check должен вернуть `Overall: OK`, а
  сервис запускается только в SAFE_MODE/HOLD с двухоператорным resume.

## Branch protection & CI

Ветка `main` защищена: прямые push запрещены, merge доступен только через PR с
зелёными статусами `lint`, `test`, `golden-master`, `self-check`,
`security-sweep`, `acceptance`, `Docker Release / build`. Подробнее см. в
[docs/BRANCH_PROTECTION.md](docs/BRANCH_PROTECTION.md).

## Документация и runbooks

* [Architecture](docs/ARCHITECTURE.md) — структурное описание сервисов, risk и CI.
* [Risk policy](docs/RISK_POLICY.md) — лимиты, safe-mode и self-check требования.
* [Security](docs/SECURITY.md) — правила хранения ключей и кодовые guardrails.
* [Ops runbook](docs/OPS_RUNBOOK.md) — пошаговый гайд для paper/testnet/live и
  инцидентов.
* [Test plan](docs/TESTPLAN.md) — обязательные тесты и пайплайны перед merge/live.

Следуйте этим инструкциям, чтобы держать репозиторий в состоянии «готов к запуску
на реальные деньги».
