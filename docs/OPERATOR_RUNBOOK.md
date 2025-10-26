# Operator Runbook (Prod/Testnet)

Рабочая памятка для операторов PropBot без доступа к коду. Все шаги предполагают,
что у вас есть сеть до инстанса и API-токен (если включена авторизация).

> ⚠️ **LIVE-торговля:** связка `PROFILE=live` и `DRY_RUN_ONLY=false` означает реальные заявки на бирже. Всегда запускайте сервис в HOLD (`mode=HOLD`) и с `SAFE_MODE=true`, проверяйте лимиты и пары (`loop_pair`/`loop_venues`), баланс и ключи, и только после ручной проверки переводите бота в `RUN` и снимаете `SAFE_MODE`.

## 1. Ежедневная проверка здоровья

1. Откройте документацию Swagger по адресу `https://<host>/docs`.
   - Убедитесь, что страница отвечает и список ручек прогружается.
2. Запросите агрегированное состояние:
   - `curl -s -H "Authorization: Bearer $API_TOKEN" https://<host>/api/ui/status/overview | jq`.
   - Поле `overall` принимает значения `OK`, `WARN`, `ERROR` или `HOLD`.
3. Посмотрите раскладку по компонентам:
   - `curl -s -H "Authorization: Bearer $API_TOKEN" https://<host>/api/ui/status/components | jq '.components[] | {id, status, summary}'`.
   - Проверяйте статус P0-гардов (`recon`, `rate_limit`, `runaway_breaker`, …) и метрики.
4. Интерпретация статусов:
   - **OK** — компонент в норме, вмешательство не требуется.
   - **WARN** — деградация, но торговый цикл всё ещё разрешён. Наблюдайте и при необходимости включайте HOLD вручную.
   - **ERROR** — критическая ошибка, компонент не выполняет SLO. Требуются действия оператора.
   - **HOLD** — торговый цикл остановлен (авто-HOLD или операторская пауза).
5. SLO-алерты:
   - Список активных нарушений отображается в `overview.alerts[*]`.
   - Любой P0-алерт (например, `recon mismatch`, `runaway_breaker`) автоматически ставит авто-HOLD: бот включает SAFE_MODE и прекращает выставлять заявки.
6. Если `overall=HOLD`, уточните причину через `components` или журнал инцидентов `/api/ui/status/components` → `incidents`.

## 2. Управление риском и паузой

1. Режимы:
   - **Обычная торговля** — `SAFE_MODE=false`, `overall` не в HOLD, торговый цикл активен.
   - **SAFE_MODE=true** — ордера не отправляются, но цикл и мониторинг продолжают работу (используйте для dry-run).
   - **HOLD** — система ставит петлю на паузу; SAFE_MODE включается автоматически, пока HOLD не снят.
2. Авто-HOLD:
   - Бот сам переводит себя в HOLD при P0-ошибке, критическом SLO-алерте или провале preflight-а.
   - В UI и Телеграме приходит уведомление о причине и таймштампе.
3. Ручная пауза и продолжение через Телеграм:
   - `/pause` — включает SAFE_MODE и HOLD.
   - `/resume` — снимает HOLD и (если пройдены approvals/Two-Man Rule) отключает SAFE_MODE.
   - `/status` — текущий обзор состояния.
   - Команды работают только из авторизованного чата `TELEGRAM_CHAT_ID`.
4. Ручная пауза через CLI `propbotctl`:
   - `python3 cli/propbotctl.py --base-url https://<host> status` — быстрый обзор без открытия Swagger.
   - `python3 cli/propbotctl.py --base-url https://<host> components` — таблица статусов компонентов.
   - `python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" pause` — постановка HOLD (payload `{"mode": "HOLD"}`).
   - `python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" resume` — выход из HOLD (payload `{"mode": "RUN"}`).
   - Bearer-токен передавайте через `--token` или переменную окружения `API_TOKEN`. Никогда не коммитьте токен в git.
5. Принудительная пауза через REST (если CLI недоступен):
   - `curl -X PATCH https://<host>/api/ui/control \
     -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"mode": "HOLD"}'`.
   - Выход из HOLD: `curl -X PATCH ... -d '{"mode": "RUN"}'` (при активном `TWO_MAN_RULE` следуйте процедуре двойного подтверждения).
6. После устранения причины HOLD убедитесь, что критические компоненты вернулись в `OK`, и снимите HOLD через Telegram или CLI.

## 3. Ротация секретов

1. Сформируйте JSON с новыми ключами (пример для Binance testnet):
   ```bash
   cat <<JSON > /tmp/keys.json
   {
     "BINANCE_UM_API_KEY_TESTNET": "<новый ключ>",
     "BINANCE_UM_API_SECRET_TESTNET": "<новый секрет>"
   }
   JSON
   ```
2. Отправьте обновление через REST или CLI:
   - REST:
     ```bash
     curl -X POST https://<host>/api/ui/secret \
       -H "Authorization: Bearer $API_TOKEN" \
       -H "Content-Type: application/json" \
       --data @/tmp/keys.json
     ```
   - CLI: `python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" rotate-key --value 'новый-секрет'` (значение можно передать из `stdin`, если не хотите хранить его в shell истории).
3. Убедитесь, что ответ содержит статус `ok` и новые значения не появляются в логах (секреты всегда редактируются).
4. Всегда удаляйте временные файлы (`rm /tmp/keys.json`).
5. **Важно:** секреты НЕЛЬЗЯ коммитить или хранить в git/облаке. Используйте менеджер секретов или защищённые vault-решения.

## 4. Лимиты риска и параметры

1. Просмотр текущих лимитов: `curl -s https://<host>/api/ui/state -H "Authorization: Bearer $API_TOKEN" | jq '.risk + {flags: .flags, dry_run: .dry_run}'`.
2. Проверка управляющего состояния: `curl -s https://<host>/api/ui/control-state -H "Authorization: Bearer $API_TOKEN" | jq`.
3. Изменение параметров на лету (доступно в `paper`/`testnet` при `SAFE_MODE=true`):
   ```bash
   curl -X PATCH https://<host>/api/ui/control \
     -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
    -d '{"order_notional_usdt": 100, "min_spread_bps": 1.2, "dry_run_only": true, "loop_pair": "BTCUSDT", "loop_venues": ["binance-um"]}'
   ```
   - Параметры `dry_run_only`, `order_notional_usdt`, `min_spread_bps`, `poll_interval_sec`, список пар/бирж обновляются без рестарта.
   - После PATCH выполните `GET /api/ui/control-state` и убедитесь, что изменения применены.
   _⚠️ Поля `pair` и `venues` больше не принимаются — сервер их игнорирует, и бот продолжит работать со старыми значениями `loop_pair`/`loop_venues`._
4. Параметры, требующие перезапуска:
   - `PROFILE`, `SAFE_MODE` на уровне `.env`, ключи API (если бот должен прочитать их при старте), `TWO_MAN_RULE` при изменении значения, окружение `MODE`.
   - Измените `.env`, затем выполните аккуратный рестарт (см. раздел 6).
5. Если `risk_blocked=true`, изучите `risk_reasons` в ответе `/api/ui/state` и устраните нарушения (например, превышение `MAX_POSITION_USDT`).

## 5. Экспорт журнала событий

1. Быстрый экспорт через curl:
   ```bash
   curl -s https://<host>/api/ui/events/export \
     -H "Authorization: Bearer $API_TOKEN" \
     -G --data-urlencode "format=csv" --data-urlencode "limit=500" \
     -o propbot-events.csv
   ```
2. Через CLI (рекомендуется при больших объёмах):
   ```bash
   python3 cli/propbotctl.py --base-url https://<host> --token "$API_TOKEN" export-log --out logs/propbot-events.json
   ```
3. Сохраняйте выгрузку в защищённом каталоге и делитесь её только с инженерами расследования.

## 6. Аккуратное выключение и рестарт

1. Перед остановкой убедитесь, что бот в HOLD (`/pause` в Telegram, `propbotctl pause` или `PATCH /api/ui/control` → `{"mode":"HOLD","dry_run_only":true}`).
2. Проверьте, что открытых позиций нет: `GET /api/ui/state` → блок `risk.positions` должен быть пустой.
3. Сохраните журнал событий, если нужно (см. раздел 5).
4. Зафиксируйте текущее состояние через CLI: `python3 cli/propbotctl.py --base-url https://<host> status` — убедитесь, что `overall.status=HOLD` и нет неожиданных алертов.
5. Остановите контейнер:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env down
   ```
6. При рестарте обновите образ/конфиг и поднимите сервис:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d
   ```
7. После старта выполните проверки из раздела 1. Для быстрого сигнала используйте CLI: `python3 cli/propbotctl.py --base-url https://<host> status` и `python3 cli/propbotctl.py --base-url https://<host> components`. Затем подтвердите через Swagger, что `overall=OK` и HOLD снят вручную, если требовалось.

## 7. Прод-деплой через Docker Compose

1. На чистом Linux-сервере установите Docker и Docker Compose plugin.
2. Склонируйте репозиторий и перейдите в каталог `deploy/`.
3. Создайте рядом каталог для данных и задайте права контейнеру:
   ```bash
   sudo mkdir -p ../data
   sudo chown 1000:1000 ../data
   sudo chmod 770 ../data
   ```
   Каталог будет примонтирован как `/app/data` и хранит `runtime_state.json`, `ledger.db`, экспортированные логи и снапшоты.
4. Скопируйте `deploy/env.example.prod` в `.env` и заполните значения (API токены, ключи, профиль, Telegram, лимиты).
5. Для первого запуска оставьте `SAFE_MODE=true`, `DRY_RUN_ONLY=true` (или `SAFE_MODE=true` + HOLD для тестнета/лайва) — убедитесь, что `mode=HOLD` через `propbotctl status`.
6. Запустите сервис:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d
   ```
7. Проверьте `/docs`, `propbotctl status --base-url https://<host>` и убедитесь, что сервис активен, но остаётся в HOLD.
8. Когда проверки завершены, снимите HOLD через `propbotctl resume --base-url https://<host> --token "$API_TOKEN"` или Telegram (Two-Man Rule должен быть выполнен, если включён).

## 8. Продакшн-данные и файловая система

- В `deploy/docker-compose.prod.yml` каталог `../data` монтируется внутрь контейнера как `/app/data`.
- Это постоянное хранилище для базы (`ledger.db`), снимков состояния (`runtime_state.json`), экспортов и временных файлов оркестратора.
- Проверьте, что папка `./data` существует на хосте и имеет права на запись для пользователя/группы, под которыми запускается Docker (`chown`/`chmod` при необходимости).
- Не удаляйте содержимое `./data` без бэкапа — там находятся рабочие журналы и состояние бота.
