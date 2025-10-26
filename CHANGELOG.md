# Changelog

## [Unreleased]

- Nothing yet.

## [0.1.2] - 2025-10-26

### Highlights
- Binance live broker.
- Risk limits with HOLD / SAFE_MODE automation.
- Telegram control bot (pause/resume/HOLD/status).
- System Status API with SLO coverage and WebSocket feed.
- Production Docker Compose profile and operator runbook.
- `propbotctl.py` CLI with bearer-token auth and secure `export-log`.

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
