# Ops / Health / Alerts Overview

## Endpoints

- `GET /api/health`
  - Общий health/readiness с проверками журналов, лидера и конфигурации. В строгих профилях учитывает интеграцию watchdog и флаги среды `HEALTH_PROFILE_STRICT`, `HEALTH_FAIL_ON_WARN`.
- `GET /api/ui/status`
  - Быстрый ops-снимок: агрегирует состояние router, риск-модуля, readiness-реестра, market watchdog и свежие алерты.
- `GET /api/ui/config`
  - Возвращает актуальный рантайм-конфиг (профиль, router-контроль, risk limits) для UI и автоматизированных проверок.
- `GET /api/ui/alerts`
  - Список последних ops-алертов (risk limits, PnL caps, watchdog, тех. события). Поддерживает фильтрацию по типу/серьёзности.
- `GET /api/ui/execution`
  - Минимальный срез исполнений: список активных/недавних заявок, помогает smoke-тестам и панели исполнения.

## Profiles и safe_mode/canary

- Поддерживаемые профили: `paper`, `testnet`, `live`.
- `live` защищён guard'ами: auto-hold, критические SLO, строгие health-пороги.
- Флаги `CANARY` / `SAFE_MODE` управляют повышенными ограничениями (dry-run, блокировка исполнения) и отражаются в `/api/ui/status`.

## Watchdog

- Мониторит ключевые компоненты: router activity, market data, reconciliation/ledger.
- Снимок добавляется в `/api/health` и `/api/ui/status`.
- В строгом профиле (`HEALTH_PROFILE_STRICT`) и при `HEALTH_FAIL_ON_WARN=1` предупреждения watchdog могут перевести сервис в `not ready`.

## Alerts

- Источники алертов:
  - Risk limits (`RISK_LIMIT_BREACHED`).
  - PnL caps (`PNL_CAP_BREACHED`).
  - Прочие ops-события (watchdog, live readiness, safety режимы).
- Алерты отправляются в журналы и внешние каналы (например, Telegram, если подключено) и доступны через `/api/ui/alerts`.
