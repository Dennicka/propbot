#!/usr/bin/env bash
set -uo pipefail

BASE_URL="${SMOKE_HOST:-http://127.0.0.1:8000}"
TIMEOUT="${SMOKE_TIMEOUT:-5}"
ENDPOINTS=(
  "/healthz"
  "/api/ui/status/overview"
  "/api/ui/positions"
  "/api/ui/state"
  "/metrics"
)

trimmed_base="${BASE_URL%/}"
exit_code=0

AUTH_HEADER=()
if [[ -n "${SMOKE_TOKEN:-}" ]]; then
  AUTH_HEADER=(-H "Authorization: Bearer ${SMOKE_TOKEN}")
fi

for path in "${ENDPOINTS[@]}"; do
  url="${trimmed_base}${path}"
  http_code=$(curl -sS -o /dev/null -w "%{http_code}" --max-time "${TIMEOUT}" "${AUTH_HEADER[@]}" "${url}" || echo "000")
  if [[ "${http_code}" == "200" ]]; then
    printf '✅ %s\n' "${url}"
  else
    printf '❌ %s (code %s)\n' "${url}" "${http_code}"
    exit_code=1
  fi
done

exit "${exit_code}"
