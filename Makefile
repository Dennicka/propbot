.PHONY: venv fmt lint typecheck test run kill golden-check \
        alembic-init alembic-rev alembic-up dryrun.once dryrun.loop \
        docker-login docker-build docker-push docker-run-image docker-release \
        up down logs curl-health release \
        acceptance_smoke acceptance_trading acceptance_chaos \
        run_paper run_testnet run_live run-paper run-testnet run-live \
        verify

VENV=.venv
PY=$(VENV)/bin/python
PIP=$(VENV)/bin/pip
UVICORN=$(VENV)/bin/uvicorn
PYTEST=python -m pytest
REMOTE ?= origin
TAG ?= 0.1.2
export TAG

CI_TESTING ?= 0
export CI_TESTING

venv:
	python3 -m venv $(VENV)
	$(PIP) install -U pip wheel
	$(PIP) install -r requirements.txt

fmt:
	python -m black .
	python -m ruff --fix .

lint:
	$(VENV)/bin/ruff check app
	$(VENV)/bin/black --check app

typecheck:
	$(VENV)/bin/mypy app

test:
	$(PYTEST) -q --maxfail=1

golden-check:
	PYTHONPATH=. python -m app.cli_golden check

golden-replay:
	PYTHONPATH=. python -m app.golden.replay

verify:
	ruff check .
	black --check .
	mypy --config-file mypy.ini app
	pytest
	pytest -q tests/golden
	@if command -v pip-audit >/dev/null 2>&1; then \
		pip-audit -r requirements.txt; \
	elif python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('pip_audit') else 1)" >/dev/null 2>&1; then \
		python -m pip_audit -r requirements.txt; \
	else \
		echo "pip-audit not installed, skipping"; \
	fi
	@if command -v bandit >/dev/null 2>&1; then \
		bandit -q -r app services; \
	elif python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('bandit') else 1)" >/dev/null 2>&1; then \
		python -m bandit -q -r app services; \
	else \
		echo "bandit not installed, skipping"; \
	fi

# Acceptance targets:
#   make acceptance_smoke   → quick smoke tests in CI/local env
#   make acceptance_trading → trading profile acceptance scenario
#   make acceptance_chaos   → chaos / resilience flows suite
acceptance_smoke:
	$(PYTEST) -q tests/acceptance/test_smoke.py

acceptance_trading:
	$(PYTEST) -q tests/acceptance/test_acceptance_trading.py

acceptance_chaos:
	$(PYTEST) -q tests/acceptance/test_chaos_flows.py

acceptance: acceptance_smoke acceptance_trading acceptance_chaos

smoke: acceptance_smoke

smoke_health:
	$(PYTEST) -q tests/acceptance/test_health_smoke.py

run:
	$(UVICORN) app.main:app --host 127.0.0.1 --port 8000

RUN_PROFILE=$(PY) scripts/run_profile.py

run_paper:
	$(RUN_PROFILE) --profile paper

run_testnet:
	$(RUN_PROFILE) --profile testnet

run_live:
	$(RUN_PROFILE) --profile live

run-paper: run_paper

run-testnet: run_testnet

run-live: run_live

dryrun.once:
	$(PY) -m app.cli exec --profile paper

dryrun.loop:
	$(PY) -m app.cli exec --profile paper --loop

kill:
	pkill -f 'app.server_ws:app' || true

alembic-init:
	$(PY) -m alembic init -t async alembic

alembic-rev:
	$(PY) -m alembic revision -m "auto"

alembic-up:
	$(PY) -m alembic upgrade head

docker-login:
	@: $${GHCR_USERNAME:?set GHCR_USERNAME for docker login}
	@: $${GHCR_TOKEN:?set GHCR_TOKEN for docker login}
	echo "$$GHCR_TOKEN" | docker login ghcr.io -u "$$GHCR_USERNAME" --password-stdin

docker-build:
	docker build -t $${IMAGE:-propbot:local} .

docker-push:
	docker push $${IMAGE:?set IMAGE to ghcr.io/<owner>/propbot:<tag>}

docker-run-image:
	docker run --rm -p 8000:8000 $${IMAGE:-ghcr.io/${REPO:?set REPO to your GitHub org}/propbot:${TAG:-main}}

docker-release:
	docker buildx build --platform linux/amd64,linux/arm64 -t $${IMAGE:?set IMAGE to ghcr.io/<owner>/propbot:<tag>} --push .

up:
	if [ "$(BUILD_LOCAL)" = "1" ]; then \
	  IMAGE=$${IMAGE:-propbot:local} PULL_POLICY=never docker compose up -d --build; \
	else \
	  docker compose up -d; \
	fi

down:
	docker compose down

logs:
	docker compose logs -f app

curl-health:
	curl -i http://localhost:8000/healthz

release:
	@: $${TAG:?set TAG to the version number, e.g. make release TAG=0.1.2}
	@if [ -n "$$(git status --porcelain)" ]; then \
	        echo "Working tree must be clean before tagging"; \
	        exit 1; \
	fi
	@if git rev-parse "v$(TAG)" >/dev/null 2>&1; then \
	        echo "Tag v$(TAG) already exists"; \
	        exit 1; \
	fi
	@echo "Tagging release v$(TAG)"
	git tag -a v$(TAG) -m "Release v$(TAG)"
	git push $(REMOTE) v$(TAG)
	@echo "Release tag v$(TAG) pushed to $(REMOTE)."
