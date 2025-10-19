# PROP_BOT_SUPER_SPEC v6.3.2 — FINAL (“Prop Lock‑in v1 — Consolidated”)

**Дата:** 2025‑10‑15  
**Замещает:** v6.3 / v6.3.1 (унификация)  
**Режим:** IMPLEMENTATION MODE — не описывать, а внедрять.  
**Язык:** ru‑RU (UI/тексты), en‑US fallback.

---

## §0. Принципы и глоссарий
- **Exactly‑once** — завершение сделок без дублей при рестартах (journal/outbox + идемпотентность).
- **Exception‑budget** — запас деградации до HOLD/rollback.
- **Two‑Man Rule** — опасные действия требуют двойного подтверждения (web/Telegram), полный аудит‑трейл.
- **Conformance‑suite** — проверка совместимости per‑venue (лимиты/форматы/поведение API).

---

## §1. Совместимость и запуск
- Python **3.12**, venv, macOS → VPS (Debian/Ubuntu).
- Профиль по умолчанию: `paper` (не ломать существующие эндпоинты).
- ENV локально:
```bash
APP_ENV=local
DEFAULT_PROFILE=paper
API_HOST=127.0.0.1
API_PORT=8000
PYTHONPATH="$PROJECT${PYTHONPATH:+:$PYTHONPATH}"
```

---

## §2. API / эндпоинты (минимум, совместимые)
- `/api/health`, `/live-readiness`, `/openapi.json`
- `/api/ui/state`, `/api/ui/{execution,pnl,exposure}`
- `/api/ui/recon/*`, `/api/ui/stream`, `/api/ui/approvals`, `/api/ui/limits`, `/api/ui/universe`
- `/api/ui/config/{validate,apply,rollback}`
- `/api/ui/status/{overview,components,slo}` и WS `/api/ui/status/stream`
- `/api/opportunities`
- `/metrics`, `/metrics/latency`

**Контракт `System Status` (jsonc, минимум):**
```jsonc
{
  "ts": "ISO8601 UTC",
  "overall": "OK|WARN|ERROR|HOLD",
  "scores": { "P0": 0.0, "P1": 0.0, "P2": 0.0, "P3": 0.0 },
  "slo": {
    "ws_gap_ms_p95": 0,
    "order_cycle_ms_p95": 0,
    "reject_rate": 0,
    "cancel_fail_rate": 0,
    "recon_mismatch": 0,
    "max_day_drawdown_bps": 0,
    "budget_remaining": 0
  },
  "components": [{
    "id": "string", "title": "string", "group": "P0|P1|P2|P3",
    "status": "OK|WARN|ERROR|HOLD", "summary": "string",
    "metrics": { "any": "numbers" },
    "links": [{ "title": "string", "href": "string" }]
  }],
  "alerts": [{ "severity": "info|warn|error|critical", "title": "string", "msg": "string", "since": "ISO8601", "component_id": "string" }]
}
```

---

## §3. Метрики, SLO и алерты
| Метрика | Цель: Local | Цель: VPS | Алерт/Действие |
|---|---:|---:|---|
| ws_gap_ms_p95 | ≤ 400 мс | ≤ 600 мс | HOLD при 3× превышении **5 мин** |
| order_cycle_ms_p95 | ≤ 200 мс | ≤ 300 мс | HOLD + auto‑remediate |
| maker_fill_rate | ≥ baseline +5% | ≥ baseline +5% | Investigate/Tune |
| taker_impact_bps | ↓ к baseline | ↓ к baseline | Gate в canary |
| reject_rate | ≤ 0.5% | ≤ 0.5% | RL tuning/Backoff |
| cancel_fail_rate | ≤ 0.3% | ≤ 0.3% | Venue quarantine |
| recon_mismatch | 0 | 0 | HOLD до исправления |
| max_day_drawdown_bps | ≤ лимита | ≤ лимита | HOLD, resume по Two‑Man Rule |

Пороги и статусы централизованы в `configs/status_thresholds.yaml` (§10.3).

---

## §4. Release‑политика (local → paper → testnet → shadow → micro‑live → ramp‑up)
1) **Local‑paper** → RG‑1..7, TCA baseline.  
2) **Testnet** → conformance‑suite + smoke.  
3) **Shadow‑live** → 0‑риск, mirror‑поток, TCA/атрибуция.  
4) **Micro‑live** → малые лимиты + exception‑budget; auto‑rollback по SLO.  
5) **Ramp‑up** → VIP‑планёр/ребаланс; A/B челленджеры малыми долями риска.

---

## §5. UI/UX (RU)
- Подсказки ≤100 мс, onboarding‑тур, `docs/USER_GUIDE_RU.md`.
- Потоки: **validate → apply → rollback**; опасные действия под **Two‑Man Rule**.

---

## §6. Acceptance Tests (минимум «зелёного» запуска)
- **AT‑P0**: падение лидера/сеть → split‑brain=0, RPO≈0, подъём в HOLD, recon=OK.  
- **AT‑TCA**: отчёт до/после, ≥3 метрики улучшены/не хуже.  
- **AT‑SLO**: выдержаны цели §3.

---

## §7. VPS Handoff / Prod Readiness
### §7.1 Архитектура релизов
Releases: `/opt/crypto-bot/releases/<ts>/` → симлинк `/opt/crypto-bot/current` → `systemd`.  
**Canary:** порт `:9000` для smoke/AT/TCA; **Rollback:** мгновенная смена симлинка + `systemctl restart`.  
**DoD:** `/api/health` и `/live-readiness` для canary зелёные ≥ 15 мин; не более 1 рестарт цикла.

### §7.2 Конфиги из UI
API: `/api/ui/config/{validate,apply,rollback}` (soft‑reload, токен отката).  
DoD: downtime ≤ 1–2 сек; валидатор схем; гарантированный rollback.

### §7.3 Тюнинг сети VPS (sysctl)
DoD: профиль установлен; цели SLO задокументированы.

### §7.4 Бэкапы/миграции БД
DoD: snapshot перед миграциями; Alembic идемпотентен; auto‑rollback релиза при провале.

### §7.5 SSH‑туннель и HTTPS
Туннель без открытых портов; Nginx/HTTPS — фича‑флаг, runbook.

---

## §8. System Status (API/UI/Acceptance)
### §8.1 API
Роутер `**/api/ui/status**` — `GET /overview`, `GET /components`, `GET /slo`, WS `/stream`.

### §8.2 Реестр проверок (backend)
Обязательное покрытие компонентами:  
- **P0:** Journal/Outbox (gap=0), Guarded Startup, Leader/Fencing (split‑brain=0), Conformance (per‑venue), Recon (mismatch=0), Keys/Security (audit on), Compliance/WORM.  
- **P1:** Live Readiness, Recon API, Config {validate|apply|rollback}, Stream, TCA Gate, Regime Engine, Policy Matrix, Sizing/De‑risk, Fee Planner.  
- **P2:** QPE, Batch Amend/Склейка, Lock‑free Queues, Zero‑copy L2, Multi‑Region MD.  
- **P3:** A/B Factory, Research Kit, Reports/Replay.

### §8.3 Пороги/цели
`configs/status_thresholds.yaml` — статусы OK/WARN по P0..P3 и локали; поддержан override.

### §8.4 UI
Шапка: Overall, профиль, кнопки HOLD/RESUME (**Two‑Man Rule**). Вкладки P0..P3, SLO & Alerts, таймлайн.  
Live‑обновления через WS/Server‑Events.

### §8.5 Acceptance
- Контракт соблюдён; ≥20 компонентов; фильтры вкладок корректны.  
- Симуляция ERROR подсвечивает карточку, создаёт алерт и (если включено) переводит в HOLD.  
- Боковина SLO показывает актуальные значения и остаток exception‑budget; при ухудшении ≥5 мин — алерт.

---

## §9. P0 Hardening (обязательно)
- **Cancel‑on‑Disconnect / Session fencing per‑venue** — при обрыве открытые ордера отменены, split‑brain=0.  
- **Rate‑Limit Governor** — приоритет cancel/replace > place; backoff; штраф маршрутам.  
- **Clock‑skew guard** — **|skew| > 200 мс → HOLD** + алерт.  
- **Snapshot+Diff continuity checks** — при gap/рассинхроне → HOLD, ресабскрайб/реинициализация.  
- **Hard Risk Kill‑caps** — при превышении → HOLD/flatten; **resume по Two‑Man Rule**.  
- **Runaway‑loop breakers** — `≤300 place/мин`, `≤600 cancel/мин`; анти‑flip post‑only; quarantine маршрута/символа.  
- **Exchange maintenance calendar** — auto‑HOLD/route‑off; ранние оповещения.  
- **Break‑glass & key‑escrow** — доступ по **Two‑Man Rule**; оффлайн‑ключи.  
- **At‑rest шифрование БД/журналов** — LUKS/encfs; runbook.  
- **Надёжность сервиса** — systemd Watchdog, Restart=on‑failure, StartLimit, logrotate, квоты.

**Acceptance:** AT‑P0‑FENCE, RLG, SKEW(>200 мс), SNAPDIFF, KILLCAP, RUNAWAY, MAINT, ENC, SVC — зелёные.

---

## §10. P1 Enhancements (под TCA/SLO/A‑B)
Drop‑Copy, Maker Quoting Core, Bulk‑API и пр. — без регрессий к §9.

---

## §11. Guardrails (анти‑ловушки)
Единая таблица порогов/действий (stale book, ws_gap, crossed/locked, anchors, funding/basis, 429, DD); конфиг‑пример; Acceptance.

---

## §12. Deliverables (код/документы)
- FastAPI‑сервис; БД SQLite (WAL) + Alembic; Prometheus‑метрики.  
- Конфиги: `configs/config.paper.yaml` (дефолт), `status_thresholds.yaml`.  
- Скрипты: `release.sh`, `rollback.sh`, Makefile.  
- Документация RU: `README_ru.md`, `RUNBOOK_ru.md`, `OPENAPI.md`, `SLO_and_Monitoring.md`.  
- Smoke‑тесты и Acceptance‑хелперы.

---

## §13. Definition of Done
- Самосогласованная спецификация, без дублей и противоречий.  
- Код собирается; тесты и smoke зелёные; OpenAPI доступен.  
- Запуск в `paper`‑профиле локально корректен.  
- PR готов к немедленному merge.
