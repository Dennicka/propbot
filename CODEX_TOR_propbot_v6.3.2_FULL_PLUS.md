# PropBot v6.3.2 — FULL PLUS ToR for Codex (No‑Questions Mode)
**Owner:** Denis • **Date:** 2025-10-19 15:34 UTC  
**Mode:** IMPLEMENTATION ONLY — do not ask clarifying questions; apply safe defaults and document assumptions in PR.

---

## 0) Inputs (set before start)
- **SPEC_URL** → raw link to `prop_bot_super_spec_v6.3.2.md`
- **REPO_URL** → Git repo with the existing FastAPI skeleton (`propbot-v6.3.2-project`)
- **BASE_BRANCH** → `main`
- **WORK_BRANCH** → `epic/indie-pro-upgrade-v2`

**Non‑interactive rules**
- Small commits (≤300 LOC), logically grouped, each guarded by tests where feasible.
- If something is missing in the repo — **create it**. If conflict occurs — **resolve**, rebase on `main`, push.
- PR is not done until **Merge button is enabled** and **all gates** are green.

---

## 1) Final Deliverables (what must exist at PR)
1. **Working service** (paper profile) with endpoints:
   - `GET /api/health`, `GET /live-readiness`, `GET /metrics`, `GET /metrics/latency`, `GET /api/opportunities`
   - `GET /api/ui/status/{overview,components,slo}`
   - `POST /api/ui/config/{validate,apply,rollback}`
   - `GET /api/ui/{execution,pnl,exposure,control-state,limits,universe,approvals}`
   - `GET /api/ui/recon/*` (at least stubs)
   - `WS /api/ui/stream`
2. **P0 guardrails wired** (config‑driven; mocks accepted): cancel‑on‑disconnect, rate‑limit governor, clock‑skew guard, snapshot+diff continuity, kill‑caps, runaway breaker, maintenance calendar, key‑escrow/two‑man placeholders.
3. **System Status**: ≥20 components with groups P0/P1/P2/P3; overall state; SLO panel populated from thresholds.
4. **Configs**: `configs/config.paper.yaml`, `configs/config.testnet.yaml`, `configs/config.live.yaml`, `configs/status_thresholds.yaml`.
5. **Docs**: `docs/README_ru.md`, `docs/RUNBOOK_ru.md`, `docs/OPENAPI.md`, `docs/SLO_and_Monitoring.md`, `docs/VPS_HANDBOOK_ru.md`.
6. **Deploy scripts**: `deploy/release.sh`, `deploy/rollback.sh`, valid `deploy/crypto-bot.service`, `deploy/crypto-bot@.service`.
7. **Tests** (pytest): `tests/test_smoke.py`, `tests/test_status_api.py`, `tests/test_config_api.py`, `tests/test_guardrails_p0.py`, `tests/test_live_readiness.py`, `tests/test_merge_safety.py`.
8. **CI**: GitHub Actions workflow that runs tests + coverage and blocks merge until green (coverage ≥60%).
9. **PR artifacts**: PR from `epic/indie-pro-upgrade-v2` to `main` with body template (below), logs/screenshots, CHANGELOG, REPORT. Mergeable (no conflicts).

---

## 2) Playbook (step‑by‑step)

1. **Repo prep**
   ```bash
   git clone REPO_URL propbot && cd propbot
   git checkout -b epic/indie-pro-upgrade-v2
   python -V  # 3.12.x
   make venv
   ```
2. **Run baseline**
   ```bash
   make alembic-up || true
   make run-paper &  # background
   curl -s http://127.0.0.1:8000/api/health
   ```
3. **Add missing configs** — create files from templates in §6.
4. **Routers** — ensure all endpoints exist; fill stubs to return valid shapes from §3; wire into `app/server_ws.py`.
5. **System Status backend** — implement aggregator in `app/services/status.py`; read SLO thresholds; expose ≥20 components; tie guardrail signals.
6. **Guardrails** — implement toggles and state flags; propagate to status + `control-state`; acceptance in §5.
7. **Metrics** — ensure `/metrics` (Prometheus) and add `/metrics/latency` histogram endpoints.
8. **Docs** — generate/update from §7.
9. **Tests** — add tests from §8; run `pytest -q`; increase coverage ≥60%.
10. **CI** — add workflow from §9; ensure status checks appear in PR.
11. **Deploy** — add scripts from §10.
12. **PR** — create with body in §11; attach logs (and screenshots if possible); rebase/resolve conflicts; ensure Merge enabled.

---

## 3) API Contracts (shapes)

### 3.1 Status
**GET `/api/ui/status/overview`**
```json
{
  "ts": "2025-01-01T00:00:00Z",
  "overall": "OK",
  "scores": {"P0": 1.0, "P1": 1.0, "P2": 1.0, "P3": 1.0}
}
```

**GET `/api/ui/status/components`** (≥20 items)
```json
{
  "ts": "2025-01-01T00:00:00Z",
  "components": [
    {"id":"journal","title":"Journal/Outbox","group":"P0","status":"OK","summary":"...","metrics":{"p95":0}},
    {"id":"rate_limit","title":"Rate-limit Governor","group":"P0","status":"OK","summary":"...","metrics":{"place_per_min":300,"cancel_per_min":600}}
  ]
}
```

**GET `/api/ui/status/slo`**
```json
{
  "ts": "2025-01-01T00:00:00Z",
  "slo": {
    "ws_gap_ms_p95": 0,
    "order_cycle_ms_p95": 0,
    "reject_rate": 0,
    "cancel_fail_rate": 0,
    "recon_mismatch": 0,
    "max_day_drawdown_bps": 0,
    "budget_remaining": 0
  }
}
```

### 3.2 Config pipeline
- **POST `/api/ui/config/validate`** → `{"ok": true}` if schema valid; else 400 with reasons.
- **POST `/api/ui/config/apply`** → stores backup, writes config, returns `{"ok": true, "rollback_token": "<token>"}`.
- **POST `/api/ui/config/rollback`** → restore by token or latest; `{"ok": true}`.

### 3.3 Misc
- All UI GET endpoints return 200 with minimal valid payload (stubs allowed).
- **WS `/api/ui/stream`** — send status tick each second (ts/overall).

---

## 4) Guardrails (P0) — behavior & acceptance

- **cancel_on_disconnect**: if enabled and a simulated disconnect is triggered, set `overall="HOLD"`, raise component `connection` to WARN/ERROR; acceptance: toggling changes overview + control-state.
- **rate_limit**: counters per minute (`place_per_min`, `cancel_per_min`); if threshold exceeded ⇒ component WARN and refuse simulated action; acceptance: unit test flips status.
- **clock_skew_guard_ms**: if skew > threshold ⇒ component ERROR + HOLD; acceptance: inject skew in test.
- **snapshot_diff_check**: if gap detected ⇒ component WARN + “reinit required”; acceptance: mock gap.
- **kill_caps**: if cap breached ⇒ set flatten/HOLD flags; acceptance: test toggles.
- **runaway_breaker**: quarantine route upon runaway; acceptance: simulate excess ops.
- **maintenance_calendar**: during window ⇒ HOLD; acceptance: set window that includes now.

All guard signals must be visible in **Status components** and reflected in **overview**.

---

## 5) SLO & Monitoring
- Read thresholds from `configs/status_thresholds.yaml`.
- Compute OK/WARN based on p95 metrics (can be mocked).
- `/metrics/latency` returns histogram buckets; include request timing for selected endpoints.

---

## 6) Config Templates (create)

**`configs/config.testnet.yaml`**
```yaml
profile: testnet
api:
  base_url: http://127.0.0.1:8000
guards:
  cancel_on_disconnect: true
  rate_limit: { place_per_min: 300, cancel_per_min: 600 }
  clock_skew_guard_ms: 200
  snapshot_diff_check: true
  kill_caps: { enabled: true, flatten_on_breach: true }
  runaway_breaker: { place_per_min: 300, cancel_per_min: 600 }
  maintenance_calendar: []
risk:
  notional_caps:
    per_symbol_usd: 1000
    total_usd: 5000
```

**`configs/config.live.yaml`**
```yaml
profile: live
api:
  base_url: http://127.0.0.1:8000
guards:
  cancel_on_disconnect: true
  rate_limit: { place_per_min: 120, cancel_per_min: 240 }
  clock_skew_guard_ms: 150
  snapshot_diff_check: true
  kill_caps: { enabled: true, flatten_on_breach: true }
  runaway_breaker: { place_per_min: 120, cancel_per_min: 240 }
  maintenance_calendar: []
risk:
  notional_caps:
    per_symbol_usd: 500
    total_usd: 2500
```

---

## 7) Docs (create/update)

**`docs/README_ru.md`** — краткий обзор, структура, запуск (paper/testnet), основные команды.  
**`docs/RUNBOOK_ru.md`** — процедуры: локальный старт, smoke, toggles, как читать Status/SLO, где логи.  
**`docs/OPENAPI.md`** — список эндпоинтов с краткими схемами (повтор §3).  
**`docs/SLO_and_Monitoring.md`** — метрики/пороги, что означает WARN/ERROR, где смотреть.  
**`docs/VPS_HANDBOOK_ru.md`** — SSH-туннель, systemd сервисы, `release.sh`/`rollback.sh`, health-пробы.

---

## 8) Tests (create/update)

**`tests/test_smoke.py`**
- 200 на `/api/health`, `/openapi.json`, все GET UI эндпоинты, `/metrics`, `/metrics/latency`, `/live-readiness`.

**`tests/test_status_api.py`**
- Проверка формы `overview/components/slo`; количество компонентов ≥20; группы и стейты валидны.

**`tests/test_config_api.py`**
- Позитивная и негативная валидация YAML; apply выдаёт `rollback_token`; rollback восстанавливает.

**`tests/test_guardrails_p0.py`**
- На каждый гард — включение ⇒ изменение статуса/overview/HOLD.

**`tests/test_live_readiness.py`**
- `/live-readiness` ⇒ READY (paper).

**`tests/test_merge_safety.py`**
```python
import os, re
def test_no_conflict_markers():
    bad=[]
    for root,_,files in os.walk("."):
        for f in files:
            if f.endswith((".py",".md",".yaml",".yml",".json",".txt")):
                p=os.path.join(root,f)
                with open(p,"r",encoding="utf-8",errors="ignore") as fh:
                    t=fh.read()
                if re.search(r"<<<<<<<|=======|>>>>>>>", t): bad.append(p)
                if "TODO" in t or "FIXME" in t: bad.append(p)
    assert not bad, f"Unresolved markers/TODOs in: {bad}"
```

---

## 9) CI (GitHub Actions)

**`.github/workflows/ci.yml`**
```yaml
name: CI
on:
  push:
    branches: ["main","epic/**","feature/**"]
  pull_request:
    branches: ["main"]
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: make venv
      - run: . .venv/bin/activate && pip install -r requirements.txt
      - run: . .venv/bin/activate && pytest -q
```

---

## 10) Deploy scripts

**`deploy/release.sh`**
```bash
#!/usr/bin/env bash
set -euo pipefail
APP=crypto-bot
BASE=/opt/$APP
TS=$(date +%Y%m%d_%H%M%S)
NEW=$BASE/releases/$TS
mkdir -p "$NEW"
rsync -a --delete ./ "$NEW/"
python3 -m venv "$NEW/.venv"
. "$NEW/.venv/bin/activate"
pip install -r "$NEW/requirements.txt"
# smoke
( uvicorn app.server_ws:app --host 127.0.0.1 --port 8000 & echo $! > "$NEW/uv.pid"; )
sleep 2
curl -fsS http://127.0.0.1:8000/api/health >/dev/null
curl -fsS http://127.0.0.1:8000/live-readiness >/dev/null
kill "$(cat "$NEW/uv.pid")"
ln -sfn "$NEW" "$BASE/current"
systemctl restart crypto-bot
echo "[OK] Release switched to $NEW"
```

**`deploy/rollback.sh`**
```bash
#!/usr/bin/env bash
set -euo pipefail
APP=crypto-bot
BASE=/opt/$APP
PREV=$(ls -1dt $BASE/releases/* | sed -n '2p')
[ -n "$PREV" ] || { echo "No previous release"; exit 1; }
ln -sfn "$PREV" "$BASE/current"
systemctl restart crypto-bot
echo "[OK] Rolled back to $PREV"
```

---

## 11) PR Body Template

**Title:** Indie‑Pro Upgrade v2 — P0/P1/SLO/Status/VPS

**Body (sections):**
- Summary — что добавлено и почему
- What changed — ключевые файлы/модули
- How to run locally — команды (make venv; make run-paper; pytest)
- Acceptance evidence — логи/скриншоты, тест‑репорт
- Assumptions (no‑questions mode) — список допущений
- Rollback plan — как откатиться
- Risks & mitigations — что может пойти не так и как это ловится

---

## 12) Merge Conflict Auto‑Resolution
- Перед пушем: `git fetch origin && git rebase origin/main`
- Конфликты — правим точечно, сохраняем оба ожидаемых поведения; повторяем тесты.
- После фикса — `git push -f` на рабочую ветку (если был rebase).
- Запуск тестов и статусов до зелёного.

---

## 13) Definition of Done (must be true)
- ✅ Все эндпоинты отвечают 200 и соответствуют схемам.
- ✅ System Status ≥20 компонент; SLO-панель читает пороги из YAML; WS-стрим тикает.
- ✅ P0‑гарды переключаются и видны в статусе; HOLD отражается в `control-state`.
- ✅ Все тесты зелёные; coverage ≥60%; CI блокирует merge до зелёного.
- ✅ `deploy/release.sh`/`rollback.sh` присутствуют; сервис‑юниты валидны.
- ✅ PR mergeable (нет конфликтов); есть CHANGELOG и REPORT.

---

**End of FULL PLUS ToR**
