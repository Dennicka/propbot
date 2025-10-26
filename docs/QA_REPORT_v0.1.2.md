# QA Report for v0.1.2

## Environment
- Base branch: `work`
- Python: Python 3.11.12
- OS: Linux 01cf6b0ab3e9 6.12.13 #1 SMP Thu Mar 13 11:34:50 UTC 2025 x86_64 x86_64 x86_64 GNU/Linux

## Dependency Installation
Command: `pip install -r requirements.txt`
- Result: Success (dependencies already satisfied)
- Excerpt:
  ```
  Requirement already satisfied: fastapi==0.115.0 ...
  Requirement already satisfied: uvicorn==0.30.6 ...
  ```

## Test Suite
Command: `pytest -q`
- Result: Success (70 passed, 5 warnings)
- Notable warnings: FastAPI `on_event` deprecation, pytest-asyncio loop scope notice, pydantic `alias` metadata warning.
- Excerpt:
  ```
  70 passed, 5 warnings in 4.32s
  ```

## Docker Build
Command: `docker build -t propbot:ci .`
- Result: Failed — Docker CLI unavailable (`bash: command not found: docker`).

## Docker Compose Validation
Command: `docker compose -f deploy/docker-compose.prod.yml config`
- Result: Not run — blocked by missing Docker installation.

## Runtime Validation
Prerequisites (temporary `.env` from `deploy/env.example.prod` with `SAFE_MODE=true`, `DRY_RUN_ONLY=true`, `PROFILE=paper`) could not be verified because Docker is unavailable in the execution environment.

Expected requests:
- `GET /health`
- `GET /api/ui/status/overview`
- `GET /api/ui/state`
- `GET /api/ui/events/export` via `cli/propbotctl.py export-log`

Status: Not run — blocked by missing Docker installation.

## Additional Notes
- Attempted to install Docker, but `apt-get update` failed due to repository access restrictions (403 errors). Consequently, Docker installation was not possible.
- Given the inability to start the containers, SAFE_MODE / HOLD status and log inspection could not be performed.

## Summary
- ✅ Dependencies installed
- ✅ Test suite passed (with warnings)
- ❌ Docker tooling unavailable (prevented remaining QA steps)

