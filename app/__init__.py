from __future__ import annotations

from .util.env import ensure_defaults, load_env_file

# Load `.env` once package is imported. Existing variables are preserved.
load_env_file()

# Ensure baseline defaults for testnet-safe operation.
ensure_defaults(
    [
        ("MODE", "testnet"),
        ("SAFE_MODE", "true"),
        ("POST_ONLY", "true"),
        ("REDUCE_ONLY", "true"),
    ]
)
