import json
from pathlib import Path

from app.runtime_state_store import write_runtime_payload
from positions_store import append_record
from app.services.approvals_store import create_request
from services.daily_reporter import append_report
from services.snapshotter import build_snapshot_payload


def test_snapshot_endpoint_requires_token(monkeypatch, client) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "audit-token")

    response = client.post("/api/ui/snapshot")
    assert response.status_code in {401, 403}


def test_snapshot_endpoint_generates_payload_and_file(monkeypatch, client, tmp_path) -> None:
    monkeypatch.setenv("AUTH_ENABLED", "true")
    monkeypatch.setenv("API_TOKEN", "audit-token")
    snapshot_dir = tmp_path / "snapshots"
    monkeypatch.setenv("SNAPSHOT_DIR", str(snapshot_dir))
    recon_alerts_path = tmp_path / "reconciliation_alerts.json"
    monkeypatch.setenv("RECONCILIATION_ALERTS_PATH", str(recon_alerts_path))

    write_runtime_payload(
        {
            "mode": "HOLD",
            "safe_mode": True,
            "dry_run_mode": False,
            "limits": {"max_notional": 1000},
            "last_hedge_ts": "2024-01-01T00:00:00Z",
            "api_token_echo": "audit-token",
        }
    )

    append_record(
        {
            "id": "pos-1",
            "timestamp": "2024-01-01T00:00:00Z",
            "symbol": "ETHUSDT",
            "long_venue": "binance-um",
            "short_venue": "okx-perp",
            "notional_usdt": 1250.0,
            "entry_spread_bps": 11.0,
            "leverage": 3.0,
            "status": "partial",
            "legs": [
                {
                    "side": "long",
                    "venue": "binance-um",
                    "symbol": "ETHUSDT",
                    "timestamp": "2024-01-01T00:00:00Z",
                    "notional_usdt": 625.0,
                    "status": "partial",
                },
                {
                    "side": "short",
                    "venue": "okx-perp",
                    "symbol": "ETHUSDT",
                    "timestamp": "2024-01-01T00:00:00Z",
                    "notional_usdt": 625.0,
                    "status": "partial",
                },
            ],
        }
    )

    create_request(
        "resume",
        requested_by="alice",
        parameters={"reason": "ops-review"},
    )

    append_report(
        {
            "timestamp": "2024-01-01T00:00:00Z",
            "pnl_realized_total": 12.34,
            "pnl_unrealized_avg": 3.21,
            "exposure_avg": 456.7,
            "slippage_avg_bps": 0.9,
        }
    )

    execution_stats_sample = [{"symbol": "ETHUSDT", "slippage_bps": 0.42, "ts": "2024-01-01T00:00:00Z"}]
    monkeypatch.setattr(
        "services.execution_stats_store.list_recent", lambda limit=250: execution_stats_sample
    )

    recon_alerts_path.write_text(
        json.dumps(
            [
                {
                    "timestamp": "2024-01-01T00:00:00Z",
                    "issue_count": 1,
                    "issues": [
                        {
                            "kind": "position_missing_on_exchange",
                            "venue": "okx-perp",
                            "symbol": "ETHUSDT",
                            "side": "short",
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    response = client.post(
        "/api/ui/snapshot",
        headers={"Authorization": "Bearer audit-token"},
    )
    assert response.status_code == 200
    payload = response.json()

    assert payload["runtime_state"]["mode"] == "HOLD"
    assert payload["approvals"]
    assert payload["positions"]
    assert payload["execution_stats"] == execution_stats_sample
    assert payload["reconciliation_alerts"]
    assert payload["daily_report"]
    assert payload["metadata"]["snapshot_path"]

    assert "audit-token" not in response.text
    assert payload["runtime_state"]["api_token_echo"] == "***redacted***"

    files = list(Path(snapshot_dir).glob("*.json"))
    assert files
    saved_path = Path(payload["metadata"]["snapshot_path"])
    assert saved_path.exists()
    saved_payload = json.loads(saved_path.read_text(encoding="utf-8"))
    assert saved_payload == payload

    built_payload = build_snapshot_payload()
    assert built_payload["runtime_state"]["api_token_echo"] == "***redacted***"
    assert "audit-token" not in json.dumps(built_payload)
