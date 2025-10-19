# Arbitrage Engine Overview

## Strategy
- Cross-venue delta-neutral arbitrage между Binance USD-M perpetuals и OKX perpetual swaps.
- SAFE_MODE по умолчанию: все вызовы `/api/arb/execute` выполняют dry-run.
- Префлайт проверяет: доступность бирж, режимы позиции/маржи/плеча, risk caps, funding window, фильтры и минимальный edge.

## Основные компоненты
- `app/services/arbitrage.py` — расчёт edge, state-machine (IDLE → PREFLIGHT → LEG_A → LEG_B → HEDGED → DONE, rescue).
- `app/services/derivatives.py` — runtime адаптеров и бумажных позиций.
- `app/exchanges/binance_um.py` / `okx_perp.py` — in-memory клиенты (расширяемые до реальных REST/WS).

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
- В SAFE_MODE исполнение не открывает позиций, но журналирует план.

## Rescue / Incidents
- При отказе второй ноги → `LEG_A_FILLED_LEG_B_FAIL`, вызывается reduceOnly-хедж, увеличивается счётчик `rescues`, в `RuntimeState.incidents` фиксируется событие.
- Гард `runaway_breaker` получает статус WARN до ручного подтверждения.

## Метрики
- `/api/ui/status/components` отображает компоненты `arb_engine`, `deriv_*` и `Incident Journal`.
- `metrics.counters` содержит `dry_runs`, `executions`, `rescues` (доступно через `/api/ui/status/components`).
