from __future__ import annotations

from pathlib import Path

import yaml

CONFIG_PATH = Path("configs/config.paper.yaml")


def test_config_validate_apply_rollback(client) -> None:
    original = CONFIG_PATH.read_text(encoding="utf-8")

    # positive validation
    resp = client.post("/api/ui/config/validate", json={"yaml_text": original})
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    # negative validation (missing profile)
    bad_resp = client.post("/api/ui/config/validate", json={"yaml_text": "{}"})
    assert bad_resp.status_code == 400

    data = yaml.safe_load(original)
    data["risk"]["max_day_drawdown_bps"] = 123
    new_yaml = yaml.safe_dump(data)

    apply_resp = client.post("/api/ui/config/apply", json={"yaml_text": new_yaml})
    assert apply_resp.status_code == 200
    token = apply_resp.json()["rollback_token"]
    assert CONFIG_PATH.read_text(encoding="utf-8") == new_yaml

    rollback_resp = client.post("/api/ui/config/rollback", json={"token": token})
    assert rollback_resp.status_code == 200
    assert CONFIG_PATH.read_text(encoding="utf-8") == original

    state_resp = client.get("/api/ui/state")
    assert state_resp.status_code == 200
    payload = state_resp.json()
    assert "flags" in payload
    flags = payload["flags"]
    expected_keys = {
        "MODE",
        "SAFE_MODE",
        "POST_ONLY",
        "REDUCE_ONLY",
        "ENV",
        "DRY_RUN",
        "ORDER_NOTIONAL_USDT",
        "MAX_SLIPPAGE_BPS",
        "TAKER_FEE_BPS_BINANCE",
        "TAKER_FEE_BPS_OKX",
        "TWO_MAN_RULE",
    }
    assert expected_keys.issubset(flags.keys())
    assert isinstance(flags["MODE"], str)
    assert isinstance(flags["ENV"], str)
    assert isinstance(flags["SAFE_MODE"], bool)
    assert isinstance(flags["POST_ONLY"], bool)
    assert isinstance(flags["REDUCE_ONLY"], bool)
    assert isinstance(flags["TWO_MAN_RULE"], bool)
    assert isinstance(flags["DRY_RUN"], bool)
    assert isinstance(flags["ORDER_NOTIONAL_USDT"], float)
    assert isinstance(flags["MAX_SLIPPAGE_BPS"], int)
    assert isinstance(flags["TAKER_FEE_BPS_BINANCE"], int)
    assert isinstance(flags["TAKER_FEE_BPS_OKX"], int)
