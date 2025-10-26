# Operator Runbook (Prod/Testnet)

Рабочая памятка для операторов PropBot без доступа к коду. Все шаги предполагают,
что у вас есть сеть до инстанса и API-токен (если включена авторизация).

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
4. Ручная пауза через CLI (по мере появления поддержки):
   - Планируется команда `python api_cli.py hold`/`resume`, использующая REST API. После релиза CLI проверьте `python api_cli.py --help`.
5. Принудительная пауза через REST:
   - `curl -X POST https://<host>/api/ui/hold -H "Authorization: Bearer $API_TOKEN"`.
   - Для выхода из HOLD используйте соответствующий POST `/api/ui/resume` (требует двух подтверждений, если включён `TWO_MAN_RULE`).
6. После устранения причины HOLD убедитесь, что критические компоненты вернулись в `OK`, и выполните `/resume`.

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
2. Отправьте обновление:
   ```bash
   curl -X POST https://<host>/api/ui/secret \
     -H "Authorization: Bearer $API_TOKEN" \
     -H "Content-Type: application/json" \
     --data @/tmp/keys.json
   ```
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
     -d '{"order_notional_usdt": 100, "min_spread_bps": 1.2, "dry_run_only": true, "pair": "BTCUSDT", "venues": ["binance_um"]}'
   ```
   - Параметры `dry_run_only`, `order_notional_usdt`, `min_spread_bps`, `poll_interval_sec`, список пар/бирж обновляются без рестарта.
   - После PATCH выполните `GET /api/ui/control-state` и убедитесь, что изменения применены.
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
   python api_cli.py --base-url https://<host> --api-token $API_TOKEN events --format csv --out logs/propbot-events.csv
   ```
3. Сохраняйте выгрузку в защищённом каталоге и делитесь её только с инженерами расследования.

## 6. Аккуратное выключение и рестарт

1. Перед остановкой убедитесь, что бот в HOLD (`/pause` или `PATCH /api/ui/control` → `dry_run_only=true`).
2. Проверьте, что открытых позиций нет: `GET /api/ui/state` → блок `risk.positions` должен быть пустой.
3. Сохраните журнал событий, если нужно (см. раздел 5).
4. Остановите контейнер:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env down
   ```
5. При рестарте обновите образ/конфиг и поднимите сервис:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml --env-file .env up -d
   ```
6. После старта выполните проверки из раздела 1 (Swagger, `/status/overview`, `/status/components`). Убедитесь, что `overall=OK` и HOLD снят вручную, если требовалось.

## 7. Продакшн-данные и файловая система

- В `deploy/docker-compose.prod.yml` каталог `../data` монтируется внутрь контейнера как `/app/data`.
- Это постоянное хранилище для базы (`ledger.db`), снапшотов состояния и экспортов.
- Проверьте, что папка `./data` существует на хосте и имеет права на запись для пользователя/группы, под которыми запускается Docker (`chown`/`chmod` при необходимости).
- Не удаляйте содержимое `./data` без бэкапа — там находятся рабочие журналы и состояние бота.
