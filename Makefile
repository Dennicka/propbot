.PHONY: venv fmt lint typecheck test run-paper kill alembic-init alembic-rev alembic-up

VENV=.venv
PY=$(VENV)/bin/python
PIP=$(VENV)/bin/pip
UVICORN=$(VENV)/bin/uvicorn
PYTEST=$(VENV)/bin/pytest

venv:
	python3 -m venv $(VENV)
	$(PIP) install -U pip wheel
	$(PIP) install -r requirements.txt

fmt:
	$(VENV)/bin/ruff check --fix app || true
	$(VENV)/bin/black app tests

lint:
	$(VENV)/bin/ruff check app
	$(VENV)/bin/black --check app

typecheck:
	$(VENV)/bin/mypy app

test:
	$(PYTEST) -q --maxfail=1

run-paper:
	APP_ENV=local DEFAULT_PROFILE=paper $(UVICORN) app.server_ws:app --host 127.0.0.1 --port 8000

kill:
	pkill -f 'app.server_ws:app' || true

alembic-init:
	$(PY) -m alembic init -t async alembic

alembic-rev:
	$(PY) -m alembic revision -m "auto"

alembic-up:
	$(PY) -m alembic upgrade head
