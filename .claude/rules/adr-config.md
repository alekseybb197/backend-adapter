---
paths:
  - "backend_adapter/{config,env_validate,env_check,cli_args,templates,state_store}.py"
  - "tests/test_{config,env_validate,env_check,cli_args,templates,state_store}.py"
  - "docs/environment.md"
  - "docs/install.md"
  - "docs/samples/**"
---

# Конфигурация, бэкенды, состояние — действующие решения (docs/adr/)

- 004 — всё через env `ADAPTER_*`; приоритет: `state.yaml` > явный env >
  `adapter.env` дома > дефолт (для runtime-пула); новые возможности — за флагом,
  дефолт безопасный.
- 005 — единственная внешняя зависимость PyYAML; `adapter.yaml` читается
  `yaml.BaseLoader` и в рантайме, и в `--check` (одинаково).
- 014 — бэкенды в YAML, `key` — ИМЯ env-переменной, не токен.
- 015 — fail-fast на старте без моделей; strict-проверка модели.
- 017 — глобалы `config`: коллекции мутируются на месте, читать через `config.X`.
- 025 — состояние в файлах, запись атомарная (tmp + `os.replace`), версия формата
  в файле; корень данных `~/.ba` = дом.
- 029 — типы env строго (`[FATAL]`), устаревшие имена — `[WARN]` и игнор.

Новая env-переменная: `env_validate._ENV_SPECS`, `templates.render_env`,
`docs/samples/sample.adapter.env` (сверка байт-в-байт в `tests/test_templates.py`),
`docs/environment.md`.

Предложены: 033 (атомарный индекс моделей), 034 (коридор `max_tokens`, Принято).
