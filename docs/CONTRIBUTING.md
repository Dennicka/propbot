# Contributing

- Установите pre-commit один раз: `pip install pre-commit && pre-commit install`
- Перед коммитом запускайте форматирование: `make fmt`
- Локально повторите проверки CI: `ruff check app tests && black --check . && mypy app && pytest -q`
