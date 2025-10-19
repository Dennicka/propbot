# PropBot v6.3.2 (paper)

## Быстрый старт (локально)
```bash
make venv
make alembic-up
make run-paper
# health
curl -s http://127.0.0.1:8000/api/health | jq
# openapi
curl -s http://127.0.0.1:8000/openapi.json | jq '.info'
```

### Профили/переменные
- DEFAULT_PROFILE=paper (дефолт).
- Конфиги: `configs/config.paper.yaml`.

### Что готово
- FastAPI сервис, ключевые UI/Recon/Config/Status endpoints (моки).
- Prometheus /metrics, /metrics/latency.
- Alembic миграция #0001, SQLite (WAL).
- Тесты smoke: `pytest`.

### Язык UI: русский (RU).
