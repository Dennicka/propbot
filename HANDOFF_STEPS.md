# Handoff — как загрузить это в пустой репозиторий

1) Скачай архив `propbot_repo_bootstrap.zip` и распакуй локально.
2) Открой GitHub Desktop → File → Clone repository… → выбери `2025` новый пустой репозиторий `Dennicka/propbot`.
3) Скопируй СОДЕРЖИМОЕ распакованной папки в локальную папку репозитория (не сам zip).
4) Commit → Push to origin (main).
5) Открой `https://github.com/Dennicka/propbot` и убедись, что файлы на месте.
6) Включи Actions (если спросит) и проверь, что CI стартанул.
7) Открой чат Codex и дай команду из README (или из ТЗ FULL_PLUS/ARBITRAGE).

Подсказки:
- `.env.example` лежит в корне: скопируй в `.env` и заполни ключи позже.
- `RESUME_STATE.json` — для возобновления работы Codex по чекпоинтам.
- Все ToR лежат в корне: `CODEX_TOR_*`.
