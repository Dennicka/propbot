# Test Plan

Все изменения в проп-боте должны проходить полный набор проверок локально и в
CI. Ниже приведён минимальный список тестов и сценариев, которые необходимо
держать зелёными перед merge и запуском на реальные деньги.

## Unit

* Запускаются через `pytest` без дополнительных флагов. Покрывают risk-гейты,
  стратегию, API, reconciler, watchdog, runtime и безопасность (`tests/test_*`).
* Локально — часть `make verify`. В CI выполняются в джобе `test`. 【F:.github/workflows/ci.yml†L32-L52】

## Integration

* Acceptance-сценарии (`tests/acceptance/*.py`) проверяют последовательности
  resume/hold, smoke и trading флоу. Запускаются через `make acceptance`.
* CI job `acceptance` гоняет smoke/trading (и chaos при включении). Требует
  зелёных lint/test/golden/security/self-check перед стартом. 【F:.github/workflows/ci.yml†L121-L164】

## Golden-master

* Набор в `tests/golden` воспроизводит фиксированные торговые сценарии и
  гарантирует отсутствие дрейфа логики. Запуск: `pytest -q tests/golden`.
* Входит в `make verify` и отдельную джобу `golden-master`. Любое расхождение
  требует осознанного обновления baseline. 【F:.github/workflows/ci.yml†L55-L67】

## Acceptance smoke (операционный)

* `scripts/smoke.sh` и `tests/acceptance/test_smoke.py` убеждаются, что API,
  UI и статусные эндпоинты отвечают. Рекомендуется запускать после каждого
  деплоя (paper/testnet/live) и после ручных вмешательств.

## Security sweep

* `bandit -q -r app services` и `pip-audit -r requirements.txt` ищут статические
  уязвимости и проблемы зависимостей. Запускаются в `make verify` (если доступно)
  и в CI job `security-sweep`. 【F:.github/workflows/ci.yml†L69-L119】
* Дополнительно выполняется `scripts/ci_secret_scan.py` для поиска секретов до
  lint/test этапа.

## Self-check

* `python -m app.services.self_check --profile <paper|testnet|live>` проверяет
  ENV, конфиги, risk caps, наличие секретов и DNS эндпоинтов. Обязателен перед
  запуском любого профиля. В CI выполняется job `self-check`. 【F:.github/workflows/ci.yml†L69-L110】

## Перед merge и релизом

1. `make verify`
2. `python -m app.services.self_check --profile paper`
3. Убедитесь, что все CI статусы (`lint`, `test`, `golden-master`, `self-check`,
   `security-sweep`, `acceptance`, `Docker Release / build`) зелёные.
4. Перед live-выкатом дополнительно прогоните self-check с `--profile live` и
   зафиксируйте результат в runbook.

Строгое соблюдение плана гарантирует, что изменения не нарушат торговые и
операционные инварианты бота.
