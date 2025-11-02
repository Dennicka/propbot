def test_system_status_exposes_account_health(monkeypatch, client) -> None:
    sample = {
        "per_exchange": {
            "binance": {
                "state": "CRITICAL",
                "margin_ratio": 0.92,
                "free_collateral": 12.5,
            },
            "okx": {
                "state": "WARN",
                "margin_ratio": 0.81,
                "free_collateral": 150.0,
            },
        },
        "worst_state": "CRITICAL",
        "reason": "ACCOUNT_HEALTH::CRITICAL::BINANCE",
    }
    monkeypatch.setattr("app.health.account_health.get_account_health", lambda: sample)
    monkeypatch.setattr("app.services.status.get_account_health", lambda: sample)
    monkeypatch.setattr("app.api.ui.system_status.get_account_health", lambda: sample)

    response = client.get("/api/ui/system_status")
    assert response.status_code == 200

    payload = response.json()
    assert "account_health" in payload
    health = payload["account_health"]
    assert health["worst_state"] == sample["worst_state"]
    assert health["reason"] == sample["reason"]
    assert set(health["per_exchange"].keys()) == {"binance", "okx"}
    assert isinstance(health["per_exchange"]["binance"]["margin_ratio"], (int, float))
    assert isinstance(health["per_exchange"]["binance"]["free_collateral"], (int, float))
