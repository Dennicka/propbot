# PROP_BOT_SUPER_SPEC v6.3.2 — “Prop Lock-in v1 — Consolidated”

**Date:** 2025-10-15  
**Replaces:** v6.3 / v6.3.1 (унифицировано)  
**Mode:** IMPLEMENTATION MODE — не описывать, а внедрять.  
**Language:** ru-RU (UI/тексты), en-US fallback.

---

## 0) Принципы и Глоссарий
- **Exactly-once**: закрытие сделок без дублей при рестартах (journal/outbox + идемпотентность).  
- **Exception-budget**: запас деградации до HOLD/rollback.  
- **Two-Man Rule**: любые опасные действия (resume из HOLD, повышение лимитов, ключи, withdraw-параметры) — двойное подтверждение (веб/Telegram), полный аудит-трейл.  
- **Conformance-suite**: проверка совместимости per-venue (лимиты/форматы/поведение API).  

---

## 1) Совместимость и запуск
- Python **3.12**, venv, macOS → VPS (Debian/Ubuntu).  
- Профиль по умолчанию: `paper`. Ключевые эндпоинты не ломать.  
- ENV локально:
```bash
APP_ENV=local
DEFAULT_PROFILE=paper
API_HOST=127.0.0.1
API_PORT=8000
PYTHONPATH="$PROJECT${PYTHONPATH:+:$PYTHONPATH}"
```

---

## 2) API/Эндпоинты (минимум, не ломая текущие)
- `/api/health`, `/live-readiness`, `/api/ui/state`, `/api/ui/{execution,pnl,exposure}`
- `/api/ui/recon/*`, `/api/ui/stream`, `/api/ui/config/{validate,apply,rollback}`  
- `/api/opportunities`, `/metrics`, `/metrics/latency`

---

## 3) Метрики, Пороги и Алерты (ядро)
| Метрика | Цель локально | Цель VPS | Алерт/Действие |
|---|---:|---:|---|
| ws_gap_ms_p95 | ≤ 400 мс | ≤ 600 мс | HOLD при 3× превышении **5 мин** |
| order_cycle_ms_p95 | ≤ 200 мс | ≤ 300 мс | HOLD + auto-remediate |
| maker_fill_rate | ≥ baseline +5% | ≥ baseline +5% | Investigate/Tune |
| taker_impact_bps | ↓ к baseline | ↓ к baseline | Gate в canary |
| reject_rate | ≤ 0.5% | ≤ 0.5% | Rate-limit tuning/Backoff |
| cancel_fail_rate | ≤ 0.3% | ≤ 0.3% | Venue quarantine |
| recon_mismatch | 0 | 0 | HOLD до исправления |
| max_day_drawdown | ≤ лимита | ≤ лимита | HOLD, **resume по Two-Man Rule** |

---

## 4) Release-политика и порядок раскатки
1) **Local-paper** → RG-1..7, TCA baseline.  
2) **Testnet** → conformance-suite + smoke.  
3) **Shadow-live** → 0 риск, mirror-поток, TCA/атрибуция.  
4) **Micro-live** → малыe лимиты + exception-budget; auto-rollback по SLO.  
5) **Ramp-up** → VIP-планёр/ребаланс; A/B челленджеры малыми долями риска.

---

## 5) UI/UX (RU)
- Info-точки «i», подсказки ≤100 мс, тур, `USER_GUIDE_RU.md`.  
- Управление из UI: **validate → apply → rollback**, безопасные дефолты, **Two-Man Rule** на опасные действия.

---

## 6) Acceptance Tests (минимум «зелёного» запуска)
- **AT-P0**: падение лидера/сеть → split-brain=0, RPO≈0, повторный подъём в HOLD, recon=OK.  
- **AT-TCA**: отчёт до/после, ≥3 метрики улучшены/не хуже.  
- **AT-SLO**: выдержаны цели из §3.

---

## 8) VPS Handoff / Prod Readiness

### 8.2 SSH-туннель (без открытых портов)
DoD: UI работает через туннель; раздел в `docs/runbooks/vps.md`.

### 8.3 HTTPS через Nginx (опционально)
DoD: фича-флагами включается/выключается; runbook.

### 8.4 Архитектура релизов (zero-downtime, быстрый rollback)
- Releases в `/opt/crypto-bot/releases/<ts>/`; активный симлинк `/opt/crypto-bot/current`; `systemd` из `current`.  
- **Canary**: второе приложение на `:9000` для smoke/AT/TCA; **Rollback**: мгновенная смена симлинка + `systemctl restart`.  
**Deliverables:** `release.sh`, `rollback.sh`, юниты `crypto-bot.service` и `crypto-bot@.service`.  
**DoD:** smoke `/api/health` и `/live-readiness` для canary зелёные **≥ 15 минут** перед переключением; переключение не рвёт соединения (≤1 рестарт цикла).

### 8.5 Config из UI
API: `/api/ui/config/{validate,apply,rollback}` с soft-reload и токеном отката.  
DoD: downtime ≤ 1–2 сек; валидатор схем; rollback всегда доступен.

### 8.6 Профиль сети VPS (sysctl)
DoD: профайл установлен; runbook «Network tuning» с целями SLO.

### 8.7 Бэкапы/миграции БД
DoD: snapshot перед миграциями; Alembic идемпотентен; auto-rollback релиза при провале.

---

## 10) System Status (API/UI/контракты/Acceptance)

### 10.1 API (обязательно)
Роутер `**/api/ui/status**`:
- `GET /overview`, `GET /components`, `GET /slo`, WS `/stream/status`.

**Контракт (jsonc, минимум):**
```jsonc
{
  "ts": "ISO8601 UTC",
  "overall": "OK|WARN|ERROR|HOLD",
  "scores": { "P0": 0.0, "P1": 0.0, "P2": 0.0, "P3": 0.0 }, // значения в диапазоне 0.0..1.0
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

### 10.2 Реестр проверок (backend)
Обязательные компоненты:  
- **P0:** Journal/Outbox (gap=0), Guarded Startup, Leader/Fencing (split-brain=0), Conformance (per-venue), Recon (mismatch=0), Keys/Security (audit on), Compliance/WORM.  
- **P1:** Live Readiness, Recon API, Config {validate|apply|rollback}, Stream, TCA Gate, Regime Engine, Policy Matrix, Sizing/De-risk, Fee Planner.  
- **P2:** QPE, Batch Amend/Skleyka, Lock-free Queues, Zero-copy L2, Multi-Region MD.  
- **P3:** A/B Factory, Research Kit, Reports/Replay.

### 10.3 Пороги/цели
`status_thresholds.yaml` с порогами OK/WARN P0..P3 и SLO local/VPS; поддержать override.

### 10.4 UI — System Status
Шапка: Overall, профиль, кнопки HOLD/RESUME (**Two-Man Rule**). Вкладки P0..P3, SLO & Alerts, таймлайн.

### 10.6 Deliverables
Backend модуль, роутер, UI-страница, автотесты.

### 10.7 Acceptance
- Контракт соблюдён; ≥ 20 компонентов; фильтры вкладок корректны.  
- Симуляция ERROR подсвечивает карточку, создаёт алерт и (если включено) переводит в HOLD.  
- Боковина SLO показывает актуальные значения и остаток exception-budget; **при ухудшении ≥ 5 минут — алерт**.  
- Live-обновления через WS.

---

## 11) Codex Handoff (1-pager для PR)
- Ветка: `epic/indie-pro-upgrade-v2`. Коммиты батчами ≤ ~300 LOC, каждый с тестами/метриками/TCA.  
- Порядок: **P0 → P1 → SLO/OBS → Status Dashboard → VPS Handoff**.  
- Гейты: ни один PR не мержится без «зелёных» Acceptance.  
- Артефакты к PR: скриншоты System Status, логи автотестов, `/live-readiness`, графики SLO, TCA «до/после».

---

## 12) P0 Hardening Addenda (обязательно)

### 12.2 Добавить явным текстом (с DoD)
- **Cancel-on-Disconnect / Session fencing per-venue** — эмуляция обрыва → открытые ордера отменены, split-brain=0.  
- **Rate-Limit Governor** — приоритет cancel/replace > place; backoff; штраф маршрутам.  
- **Clock-skew guard** — **|skew| > 200 мс → HOLD** + алерт.  
- **Snapshot+Diff continuity checks** — при gap/рассинхроне → HOLD, ресабскрайб/реинициализация.  
- **Hard Risk Kill-caps** — при превышении → HOLD/flatten, **resume по Two-Man Rule**.  
- **Runaway-loop breakers** — `≤ 300 place/мин`, `≤ 600 cancel/мин`; анти-flip post-only; quarantine маршрута/символа.  
- **Exchange maintenance calendar** — auto-HOLD/route-off, ранние оповещения.  
- **Break-glass & key-escrow** — авардоступ по **Two-Man Rule**; оффлайн-ключи.  
- **At-rest шифрование БД/журналов** — LUKS/encfs; runbook.  
- **Service надёжность на узле** — systemd Watchdog, Restart=on-failure, StartLimit, logrotate, квоты.

### 12.3 Интеграция в Acceptance
- **AT-P0-FENCE, RLG, SKEW(>200 мс), SNAPDIFF, KILLCAP, RUNAWAY, MAINT, ENC, SVC** — все зелёные.

---

## 13) P1 Enhancements (под TCA/SLO/A-B)
Drop-Copy, Maker Quoting Core, Bulk-API и пр. (перечень как в v6.3, без регрессий).

---

## 23) Guardrails (анти-ловушки) — расширено
Таблица порогов/действий (stale book, ws_gap, crossed/locked, anchors, funding/basis, 429, DD и т. п.); конфиг-пример; Acceptance.

---

## 24–26) Расширения UI/Events/Acceptance
UI-руссификация/what-if sandbox; API расширения (`/api/ui/universe`, `/api/ui/limits`, `/api/ui/approvals`, SSE события); расширенный Acceptance (A–E).
