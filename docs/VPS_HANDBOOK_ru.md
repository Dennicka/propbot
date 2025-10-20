# VPS Handbook

## Доступ
- SSH: `ssh propbot@vps` (через jump-host, VPN согласно политике безопасности).
- Хранение секретов: `.env` (не в репозитории), содержит API-ключи Binance/OKX и SAFE_MODE.

## Systemd
- Сервис: `/etc/systemd/system/crypto-bot.service` (одиночный экземпляр) и шаблон `crypto-bot@.service` для канареек.
- Основные команды:
  - `sudo systemctl status crypto-bot`
  - `sudo systemctl restart crypto-bot`
  - `sudo journalctl -u crypto-bot -f`

## Деплой
1. Скопировать артефакт на VPS.
2. Запустить `deploy/release.sh` из корня проекта.
3. Скрипт создаёт релиз в `/opt/crypto-bot/releases/<timestamp>`, поднимает временный uvicorn smoke, проверяет `/api/health` и `/live-readiness`, переключает `current`, перезапускает systemd.
4. Для отката — `deploy/rollback.sh` (возвращает симлинк на предыдущую версию и рестартует службу).

## Health-checkи
- `GET http://127.0.0.1:8000/api/health`
- `GET http://127.0.0.1:8000/live-readiness`
- Проброс наружу через reverse-proxy (nginx) с basic auth.

## Логи
- Приложение пишет в stdout/stderr → `journalctl`.
- Дополнительно рекомендуется подключить Loki/Promtail или Filebeat.

## Post-deploy check
- `/api/ui/status/overview` = `OK`.
- `/api/arb/preview` выдаёт положительный preflight.
- `/metrics` экспонирует бизнес-метрики.
