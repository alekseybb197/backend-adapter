# ADR-034: Настраиваемый коридор `max_tokens` с учётом формата тела

| Поле | Значение |
|---|---|
| Статус | **Принято** автором (2026-10-10, вопрос 18: «Конечно да!»); реализация — к выполнению |
| Тип решения | deliberate (записано) |
| Заменяет | константы `16 / 8192 / 16384` в `convert.sanitize_max_tokens` (ADR-010) |

## Контекст

`sanitize_max_tokens` защищает от ошибки конкретного бэкенда (малый
`max_tokens` затирал ответ) и действует во всех трактах, включая
`passthrough` (ADR-010). Коридор зашит константами.

**Обнаружено при подготовке ADR** (проверено вызовом функции на v1.0.0):
санитайзер знает только поле `max_tokens`:

| Тело на входе | Что уходит бэкенду |
|---|---|
| Responses: `{"max_output_tokens": 32000}` | `{"max_output_tokens": 32000, "max_tokens": 8192}` |
| Completions: `{"max_completion_tokens": 32000}` | `{"max_completion_tokens": 32000, "max_tokens": 8192}` |
| без лимита | `{"max_tokens": 8192}` |
| `{"max_tokens": 32000}` | `{"max_tokens": 16384}` |

Тело с двумя лимитами — противоречивое; OpenAI-совместимые API отклоняют
`max_tokens` вместе с `max_completion_tokens`, а для Responses API поле
`max_tokens` неизвестно. Лимит, заданный клиентом в «правильном» поле,
коридором не проверяется вовсе.

## Решение

**Переменные** (int, строгая валидация `env_validate._ENV_SPECS`):

| Переменная | Дефолт | Смысл |
|---|---|---|
| `ADAPTER_MAX_TOKENS_MIN` | `16` | меньше — служебный запрос, заменяется на DEFAULT |
| `ADAPTER_MAX_TOKENS_DEFAULT` | `8192` | значение вместо слишком малого или отсутствующего |
| `ADAPTER_MAX_TOKENS_MAX` | `16384` | потолок; `0` — без потолка |

Стартовая проверка согласованности: `MIN ≤ DEFAULT` и (`MAX = 0` или
`DEFAULT ≤ MAX`), иначе `[FATAL]` (неверный коридор — ошибка конфигурации,
а не «устаревшее имя», ADR-029).

**Поле лимита определяется форматом исходящего тела** (`out_fmt` известен в
`server.py` в каждой ветке):

| Формат | Поле |
|---|---|
| `responses` | `max_output_tokens` |
| `completions` | `max_completion_tokens`, если присутствует, иначе `max_tokens` |
| `messages` | `max_tokens` |

Правится **только одно** поле; второе никогда не добавляется. Отсутствующее
поле заполняется DEFAULT (сохраняется текущее поведение, ради которого
санитайзер создан).

```python
_LIMIT_FIELD = {"responses": "max_output_tokens", "messages": "max_tokens"}


def _limit_field(body: dict, fmt: str) -> str:
    if fmt == "completions":
        return "max_completion_tokens" if "max_completion_tokens" in body else "max_tokens"
    return _LIMIT_FIELD[fmt]


def sanitize_max_tokens(body: dict, fmt: str, req_id: str = "") -> dict:
    lo = config.ADAPTER_MAX_TOKENS_MIN
    default = config.ADAPTER_MAX_TOKENS_DEFAULT
    hi = config.ADAPTER_MAX_TOKENS_MAX
    field = _limit_field(body, fmt)
    value = body.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < lo:
        body[field] = default
    elif hi and value > hi:
        body[field] = hi
    if body.get(field) != value and req_id:
        _d(f"[{req_id}] [CLAMP] {field} {value} -> {body[field]}")
    return body
```

`bool` исключён явно: `True` — подкласс `int` и иначе прошёл бы как `1`.

## Следствия

- Новые переменные попадают в `templates.render_env`,
  `docs/samples/sample.adapter.env` (сверка байт-в-байт в
  `tests/test_templates.py`), `docs/environment.md`, `--check`.
- Решение о включении в runtime-пул `/config` — отдельное (сейчас не
  предлагается: коридор — свойство бэкенда, а не отладочная настройка).
- Адаптивный подъём (`bump_reasoning_budget`) по-прежнему может превысить
  MAX — это намеренно (ADR-010).
- Тесты: таблица «формат × поле × значение» (включая `bool`, `None`,
  строку, оба поля completions), стартовая проверка коридора, `MAX=0`.
