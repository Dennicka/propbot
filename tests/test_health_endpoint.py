from __future__ import annotations

import time


def test_healthz(client):
    response = client.get("/healthz")
    attempts = 0
    while response.status_code != 200 and attempts < 5:
        time.sleep(0.05)
        response = client.get("/healthz")
        attempts += 1
    assert response.status_code == 200
    assert response.json() == {"ok": True}
