# CI Guardrails

Workflow `.github/workflows/ci.yml` блокирует слияние в `main`, если обязательные
проверки не проходят. Этот документ описывает состав pipeline и как повторить
его локально.

## Обязательные стадии

1. **Secret scan (`secret-scan`).** Запускает `python scripts/ci_secret_scan.py` и
   ищет высокоэнтропийные строки, приватные ключи и другие артефакты, которые
   нельзя коммитить. 【F:.github/workflows/ci.yml†L9-L22】【F:scripts/ci_secret_scan.py†L1-L120】
2. **Lint & type-check (`lint`).** Выполняет `ruff check`, `black --check` и
   `mypy` поверх `app/` и `tests/`. Любые стилистические или типовые ошибки
   блокируют pipeline. 【F:.github/workflows/ci.yml†L23-L48】
3. **Unit tests (`test`).** Запускает `pytest -q` и `make golden-check`.
   Golden-сценарии должны совпадать с эталоном, иначе job завершается ошибкой.
   【F:.github/workflows/ci.yml†L49-L68】【F:Makefile†L20-L44】
4. **Acceptance (`acceptance`).** Прогоняет smoke/trading/chaos сценарии через
   `make acceptance_*`. Job зависит от `test` и завершится, если любой из
   сценариев не прошёл. 【F:.github/workflows/ci.yml†L69-L116】
5. **Security sweep (`security-sweep`).** Устанавливает `bandit` и `pip-audit`,
   проверяет исходники `app/` и зависимости `requirements.txt`. Любой найденный
   issue блокирует merge. 【F:.github/workflows/ci.yml†L117-L148】

Во всех стадиях отсутствует `continue-on-error`, поэтому красный статус любого
шагa останавливает workflow.

## Локальный запуск

* `python scripts/ci_secret_scan.py`
* `ruff check app tests && black --check app tests`
* `mypy app`
* `pytest -q`
* `make golden-check`
* `make acceptance`
* `bandit -q -r app services && pip-audit -r requirements.txt`

Перед любым merge убедитесь, что перечисленные команды зелёные — это минимальный
набор, который гарантирует безопасность и регрессионную совместимость релизов.

