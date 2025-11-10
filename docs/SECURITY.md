# Security and Secrets Handling

This project enforces a strict separation between code and runtime secrets to
prevent accidental leakage during development or deployment.

## Secrets store

* All exchange credentials and operator tokens are loaded via
  `app.secrets_store.SecretsStore`. The path is provided through the
  `SECRETS_STORE_PATH` environment variable and must point to an encrypted JSON
  bundle stored outside the repository.
* The optional `SECRETS_ENC_KEY` variable enables the lightweight XOR/base64
  obfuscation implemented by the secrets store. The key never lives in the
  repository and should be provisioned alongside the JSON payload.
* Runtime components such as the Binance and OKX clients prefer credentials from
  the secrets store. Environment variables remain a fallback for local testing,
  but real keys must **not** be committed to the repository.

## Live profile requirements

* Launching with `PROFILE=live` is blocked when the secrets store is missing or
  does not contain the required keys. `app.startup_validation.validate_startup`
  logs each blocking error and exits with status code `1`.
* Mandatory secrets for the live profile are defined in
  `configs/profile.live.yaml` and validated by `ensure_required_secrets`. Missing
  entries prevent the service from starting.

## Runtime guards

* Startup validation also checks for placeholder values, unsafe feature flags,
  and critical filesystem paths before allowing the application to continue.
* Risk, router, broker and reconciliation modules avoid placeholder constructs
  (`pass`, `print`, `eval`, etc.). The `tests/test_no_placeholders.py` test keeps
  these paths clean.

## Continuous integration

* The CI workflow (`.github/workflows/ci.yml`) contains a lightweight
  `secret-scan` job that runs `scripts/ci_secret_scan.py` on every push and pull
  request. The scanner searches the tracked files for high-entropy assignments
  and private key blocks and fails the pipeline if a match is detected.
* Unit and acceptance suites run after a successful secret scan to ensure no
  regressions slip through.

Follow these rules whenever preparing a new deployment or rotating credentials.
