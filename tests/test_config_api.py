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
