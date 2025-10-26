# Changelog

## [Unreleased]

## [0.1.2] - 2025-10-26

### Added
- Added `docs/OPERATOR_RUNBOOK.md` with production and testnet operator procedures, including health checks, HOLD workflows, secret rotation, and restart guidance.
- Added local operator CLI `cli/propbotctl.py` covering status, pause/resume, secret rotation, and event export commands.

### Changed
- Simplified `README.md` and `docs/TESTNET_QUICKSTART.md`, pointing routine steps to the runbook and clarifying production data directory expectations.
- Clarified production deployment documentation around `docker-compose.prod.yml` and the persistent `./data` volume.
- Clarified the operator runbook and `/api/ui/control` instructions to emphasise `loop_pair`/`loop_venues` and avoid accidental pair misconfiguration.

## [0.1.1] - 2025-10-26

### Added
- Telegram control and alert bot with pause/resume/status/close commands and operator notifications.
- System Status API with SLO-driven alerts and automatic HOLD/SAFE_MODE escalation.
- Web/API control surface: `/api/ui/status/...`, `/api/ui/state`, `/api/ui/events` endpoints and UI panel updates.

### Changed
- Redacted API responses to mask API tokens and exchange keys.

## [0.1.0] - 2024-04-30

### Added
- Binance Futures live broker with safe paper/testnet defaults.
- Idempotency keys and token-scoped rate limiting across API endpoints.
- Authentication guard via bearer token for mutating routes.
- Export endpoints and CLI helpers for events and portfolio snapshots.
- Docker packaging with published GHCR images and smoke-test workflows.

### Migrated
- Upgraded the project to Pydantic v2; review new APIs such as `model_dump()`/`model_validate()` and `ConfigDict` when updating models. (No public PR/issue reference available)
