# Derivatives Setup Guide

## Binance USD-M Futures (UM)
1. Создать API-ключ (read/write, futures) на Binance, включить testnet при необходимости.
2. Настроить режим хеджирования: `POST /api/deriv/setup` c телом:
```json
{
  "venues": {
    "binance_um": {"position_mode": "hedge", "margin_type": "isolated", "leverage": 5}
  }
}
```
3. Проверить `/api/deriv/status` — поле `connected=true`, `position_mode=hedge`.
4. Ограничения: минимальный шаг `0.001`, `min_notional` = $5 (см. `get_filters`).

## OKX Perpetual Swaps
1. Сгенерировать API-key/secret/passphrase, задать режим isolated.
2. В тестовой среде использовать `symbols`: `BTC-USDT-SWAP`, `ETH-USDT-SWAP`.
3. Настроить tdMode (`isolated`) и leverage через `POST /api/deriv/setup`.
4. Проверить `/api/deriv/positions` — поля `margin_type`, `leverage`.

## Funding / Edge Policy
- `include_next_window=true` → арбитраж учитывает funding rate следующего окна.
- `avoid_window_minutes` — бот не открывает новые позиции за N минут до расчёта funding.
- `min_edge_bps` и `max_leg_slippage_bps` задают требования к дисбалансу.

## Переход в live
1. Заполнить `.env` c live API-ключами.
2. `SAFE_MODE=false`, `ALLOW_LIVE_ORDERS=1` (только после approvals).
3. `POST /api/arb/preview` → убедиться, что preflight ок.
4. Собрать два approvals → `POST /api/arb/execute` для минимального размера.
5. `POST /api/hedge/flatten` — завершение торгового дня.
