# RUNBOOK_ru.md

## Safe Mode / HOLD / RESUME
- HOLD активируется автоматикой (SLO/alerts) или вручную (Two‑Man Rule).
- RESUME требует двойного подтверждения (web + Telegram/2nd operator).

## Kill-switch
- При breach kill-caps → HOLD и flatten; возобновление по процедуре Two‑Man Rule.

## DR/HA
- Leader election + fencing tokens (эмуляция в paper).
- Cancel-on-disconnect; watchdog systemd.

## Релизы
- `release.sh` / `rollback.sh` — canary на :9000, зелёные проверки ≥15 минут, затем переключение симлинка.
