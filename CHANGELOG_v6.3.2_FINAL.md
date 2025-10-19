# CHANGELOG_v6.3.2_FINAL.md

## Тип: spec-fix (без изменения намерений), safe defaults

### Added
- Явные DoD для fencing/cancel-on-disconnect/clock-skew/runaway/maintenance/at-rest encryption.
- Контракты и Acceptance для `System Status` и Guardrails.
- Файл `configs/status_thresholds.yaml` для централизованных порогов.

### Changed
- Единая нумерация разделов (§0..§13), добавлены якоря и перекрёстные ссылки.
- Типографика ru-RU; таблицы и JSONC примеры выровнены.
- Порог **|skew| > 200 мс → HOLD**; runaway лимиты 300/600 в мин.

### Fixed
- Несогласованности ms/сек, %/bps — унифицировано.
- Уточнены статусы и DoD в release/canary/rollback.

### Security
- Two‑Man Rule для опасных действий; break-glass/key-escrow; audit‑trail.


- Sync: добавлены `/api/ui/{approvals,limits,universe}` для соответствия исходной спецификации.
