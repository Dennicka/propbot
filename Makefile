.PHONY: venv fmt lint typecheck test run kill \
        alembic-init alembic-rev alembic-up dryrun.once dryrun.loop \
        docker-login docker-build docker-push docker-run-image docker-release \
        up down logs curl-health release

VENV=.venv
PY=$(VENV)/bin/python
PIP=$(VENV)/bin/pip
UVICORN=$(VENV)/bin/uvicorn
PYTEST=$(VENV)/bin/pytest
REMOTE ?= origin
TAG ?= 0.1.1
export TAG

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

run:
        $(UVICORN) app.main:app --host 127.0.0.1 --port 8000

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
        @: $${TAG:?set TAG to the version number, e.g. make release TAG=0.1.1}
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
