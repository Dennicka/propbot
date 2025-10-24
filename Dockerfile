FROM python:3.12-slim

ARG VCS_REF="local"
ARG VERSION="dev"
ARG REPO_URL="https://github.com/propbot/propbot"

LABEL org.opencontainers.image.title="PropBot" \
    org.opencontainers.image.description="Arbitrage test bot MVP with FastAPI, paper/testnet brokers, and dashboard." \
    org.opencontainers.image.url="${REPO_URL}" \
    org.opencontainers.image.source="${REPO_URL}" \
    org.opencontainers.image.revision="${VCS_REF}" \
    org.opencontainers.image.version="${VERSION}"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN addgroup --system app && adduser --system --ingroup app app
RUN chown app:app /app

COPY requirements.txt ./
RUN pip install -U pip wheel \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

USER app

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
