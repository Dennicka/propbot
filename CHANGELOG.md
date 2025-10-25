# Changelog

## [Unreleased]

## [0.1.0] - 2024-04-30

### Added
- Binance Futures live broker with safe paper/testnet defaults.
- Idempotency keys and token-scoped rate limiting across API endpoints.
- Authentication guard via bearer token for mutating routes.
- Export endpoints and CLI helpers for events and portfolio snapshots.
- Docker packaging with published GHCR images and smoke-test workflows.

### Migrated
- Upgraded the project to Pydantic v2; review new APIs such as `model_dump()`/`model_validate()` and `ConfigDict` when updating models. (No public PR/issue reference available)
