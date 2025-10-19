# PropBot (bootstrap)

Этот репозиторий содержит стартовый набор файлов и ToR для Codex:
- `CODEX_TOR_propbot_v6.3.2_FULL_PLUS.md` — базовая операционная готовность (paper/testnet).
- `CODEX_TOR_propbot_v6.3.2_ARBITRAGE_FUTURES_ADDENDUM.md` — деривативный арбитраж Binance UM ↔ OKX Perps.
- `prop_bot_super_spec_v6.3.2.md` — полная спецификация.

## Быстрый старт
1) Создай ветку: `epic/indie-pro-upgrade-v2`.
2) Передай Codex ссылку на репозиторий и попроси выполнить **FULL_PLUS** + **ARBITRAGE_FUTURES_ADDENDUM** (режим no-questions).
3) Проверь PR, CI и **Merge**.

## Переменные окружения
Смотри `.env.example`. Реальные ключи не коммить.

## Возобновление работы (лимиты Codex)
Файл `RESUME_STATE.json` хранит чекпоинт. Codex обязан завершать шаги в зелёном состоянии и продолжать позже.
