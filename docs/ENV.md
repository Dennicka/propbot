| VAR | Default | Description | Level |
| --- | ------- | ----------- | ----- |
| SAFE_MODE | 0 | Автоматический стоп подачи ордеров при срабатывании guard. | safety |
| EXEC_PROFILE | paper | Активный профиль исполнения: paper/testnet/live. | core |
| FF_PRETRADE_STRICT | 0 | Жёсткие проверки pre-trade условий. | feature-flag |
| FF_RISK_LIMITS | 0 | Включает риск-лимиты маршрутизатора. | feature-flag |
| IDEMPOTENCY_WINDOW_SEC | 3 | Тайм-окно уникальности ключей идемпотентности. | reliability |
| IDEMPOTENCY_MAX_KEYS | 100000 | Максимальное количество ключей в памяти. | reliability |
| FF_IDEMPOTENCY_OUTBOX | 0 | Использовать outbox для публикации событий идемпотентности. | feature-flag |
| ORDER_TRACKER_TTL | 3600 | Время хранения трекера ордеров (в секундах). | reliability |
| ORDER_TRACKER_MAX | 20000 | Максимальное число активных записей трекера. | reliability |
| FF_ROUTER_COOLDOWN | 0 | Включает cooldown-логику в маршрутизаторе. | feature-flag |
| ROUTER_COOLDOWN_SEC_DEFAULT | 5 | Базовый интервал cooldown по умолчанию. | throttling |
| ROUTER_COOLDOWN_REASON_MAP | JSON | Карта причин -> индивидуальные cooldown значения. | throttling |
| FF_ORDER_TIMEOUTS | 0 | Активирует расширенные таймауты подтверждений. | feature-flag |
| SUBMIT_ACK_TIMEOUT_SEC | 3 | Сколько ждать подтверждение отправки. | reliability |
| FILL_TIMEOUT_SEC | 30 | Максимальное ожидание заполнения. | reliability |
| METRICS_PATH | "data/metrics/metrics.prom" | Путь к Prometheus textfile экспортеру. | observability |
| METRICS_BUCKETS_MS | CSV | Настройка бакетов для latency-гистограмм. | observability |
| FF_READINESS_AGG_GUARD | 0 | Агрегирующий guard готовности. | feature-flag |
| READINESS_TTL_SEC | 30 | TTL сигналов готовности. | observability |
| READINESS_REQUIRED | "market,recon,adapters" | Список обязательных readiness-сигналов. | observability |
