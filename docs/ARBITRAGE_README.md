# Arbitrage Engine Overview

## Strategy
- Cross-venue delta-neutral arbitrage между Binance USD-M perpetuals и OKX perpetual swaps.
- SAFE_MODE по умолчанию: все вызовы `/api/arb/execute` выполняют dry-run.
- Префлайт проверяет: доступность бирж, режимы позиции/маржи/плеча, risk caps, funding window, фильтры и минимальный edge.

## Основные компоненты
- `app/services/arbitrage.py` — расчёт edge, state-machine (IDLE → PREFLIGHT → LEG_A → LEG_B → HEDGED → DONE, rescue).
- `app/services/derivatives.py` — runtime адаптеров и бумажных позиций.
- `app/exchanges/binance_um.py` — реальный REST-клиент Binance testnet с SAFE_MODE fallback.
- `app/exchanges/okx_perp.py` — реальный REST-клиент OKX demo (подпись, tdMode, posSide, reduceOnly).

## API
| Endpoint | Описание |
| --- | --- |
| `GET /api/arb/edge` | список пар, net edge, tradable size |
| `POST /api/arb/preview` | dry-run и preflight отчёт |
| `POST /api/arb/execute` | state-machine исполнения (SAFE_MODE → dry-run) |
| `POST /api/hedge/flatten` | reduceOnly закрытие ног |
| `GET /api/deriv/status` | статусы адаптеров |
| `POST /api/deriv/setup` | установка режимов |
| `GET /api/deriv/positions` | бумажные позиции |

## Эджи и риски
- Edge = `(short_bid - long_ask)/mid * 10_000 - fees - slippage`.
- `configs/config.<profile>.yaml` задаёт `min_edge_bps`, `max_leg_slippage_bps`, notional caps и delta limits.
- По умолчанию используется `IOC` режим (чтобы не оставлять незахеджированные хвосты); при `post_only_maker=true` ордера выставляются `LIMIT`/Post Only на уровнях `bid/ask`.
- В SAFE_MODE исполнение не открывает позиций, но журналирует план.

## Rescue / Incidents
- При отказе второй ноги → `LEG_A_FILLED_LEG_B_FAIL`, вызывается reduceOnly-хедж, увеличивается счётчик `rescues`, в `RuntimeState.incidents` фиксируется событие.
- Гард `runaway_breaker` получает статус WARN до ручного подтверждения.

## Метрики
- `/api/ui/status/components` отображает компоненты `arb_engine`, `deriv_*` и `Incident Journal`.
- `metrics.counters` содержит `dry_runs`, `executions`, `rescues` (доступно через `/api/ui/status/components`).

## Быстрый чеклист testnet

1. Подготовьте `.env` (см. `docs/DERIV_SETUP_GUIDE.md`) и запустите сервис с `EXCHANGE_PROFILE=testnet`.
2. Убедитесь, что `/api/deriv/status` отображает `connected=true` по обоим venue.
3. Выполните `POST /api/arb/preview` — префлайт и расчёт edge после комиссий.
4. Для реального исполнения снимите SAFE_MODE (после двух approvals) и вызовите `POST /api/arb/execute`.
5. В завершение дня вызовите `POST /api/hedge/flatten` для закрытия позиций на обеих биржах.
