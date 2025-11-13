from pathlib import Path


def test_ops_runbook_exists_and_has_headers():
    p = Path("docs/OPS-RUNBOOK.md")
    assert p.exists(), "docs/OPS-RUNBOOK.md missing"
    s = p.read_text(encoding="utf-8")
    for hdr in [
        "# OPS RUNBOOK (v1)",
        "## 1. Profiles & Modes",
        "## 2. Quick Start (Paper)",
        "## 3. Quick Start (Testnet)",
        "## 4. Live Guard & Confirm",
        "## 5. ENV & Flags",
        "## 6. Metrics & Monitoring",
        "## 7. Troubleshooting (частые фейлы)",
        "## 8. Rollback & Release",
        "## 9. Smoke & Acceptance",
        "## 10. Incident Notes",
    ]:
        assert hdr in s, f"missing section: {hdr}"


def test_env_doc_exists_and_has_required_vars():
    p = Path("docs/ENV.md")
    assert p.exists(), "docs/ENV.md missing"
    s = p.read_text(encoding="utf-8")
    for var in [
        "SAFE_MODE",
        "EXEC_PROFILE",
        "FF_PRETRADE_STRICT",
        "FF_RISK_LIMITS",
        "IDEMPOTENCY_WINDOW_SEC",
        "IDEMPOTENCY_MAX_KEYS",
        "FF_IDEMPOTENCY_OUTBOX",
        "ORDER_TRACKER_TTL",
        "ORDER_TRACKER_MAX",
        "FF_ROUTER_COOLDOWN",
        "ROUTER_COOLDOWN_SEC_DEFAULT",
        "ROUTER_COOLDOWN_REASON_MAP",
        "FF_ORDER_TIMEOUTS",
        "SUBMIT_ACK_TIMEOUT_SEC",
        "FILL_TIMEOUT_SEC",
        "METRICS_PATH",
        "METRICS_BUCKETS_MS",
        "FF_READINESS_AGG_GUARD",
        "READINESS_TTL_SEC",
        "READINESS_REQUIRED",
    ]:
        assert var in s, f"ENV var missing in docs: {var}"
