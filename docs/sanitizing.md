# Sanitization — контроль утечек токенов и паролей в логах адаптера

> Анализ встроенной системы маскирования чувствительных данных. Описаны паттерны, точки применения, зоны риска и способы поиска в логах.

---

## 1. Источники: откуда берутся образцы для маскирования

Два регулярных выражения (`backend_adapter/redact.py`, `_SECRET_PATTERNS`) — это **все**, что умеет адаптер для обнаружения секретов. Ни больше, ни меньше.

### Паттерн 1: Bearer-токены

```regex
(Bearer\s+)([A-Za-z0-9\-_\.=/+]{6,})
```

- **Ищет:** слово `Bearer` (без учёта регистра), пробельные символы, затем 6 и более допустимых символов.
- **Маскирует:** вторую захваченную группу — значение токена.
- **Результат:** `Bearer sk-abc...***REDACTED***...xyzQ`
- **Где встречается:** заголовок `Authorization: Bearer <key>`; раскрытое значение env-переменной токена, на которую ссылается поле `key` YAML-конфига (`sample.adapter.yaml`).

### Паттерн 2: Именованные переменные секретов

```regex
((?:[A-Z0-9_]*(?:_PAT|_KEY|_TOKEN|_SECRET|API_KEY)[A-Z0-9_]*)\s*[:=]\s*["\']?)([A-Za-z0-9\-_\.\/+=]{8,})
```

- **Ищет:** имена переменных, содержащие `_PAT`, `_KEY`, `_TOKEN`, `_SECRET` или `API_KEY` (**только верхний регистр** + подчёркивания), далее `:` или `=`, затем 8+ символов значения.
- **Маскирует:** только значение после разделителя, имя переменной сохраняется.
- **Результат:** `ADAPTER_HOME_KEY="sk-abc123***REDACTED***xyzQw9"`
- **Где встречается:** строки логирования, содержащие пары вида `key=value` или `key: value` с именами переменных окружения.

**Важно:** шаблон требует `[A-Z0-9_]` — строчные буквы не входят. Следовательно `api_key`, `access_token`, `secret_key` в JSON-теле **не совпадут** с этим паттерном.

### Алгоритм маскирования

| Длина маскируемой строки | Результат |
|---|---|
| ≤ 8 | `***REDACTED***` (полностью заменяется) |
| > 8 | первые 4 символа + `***REDACTED***` + последние 4 символа |

```python
def _mask(value: str) -> str:
    if len(value) <= 8:
        return "***REDACTED***"
    return f"{value[:4]}***REDACTED***{value[-4:]}"
```

---

## 2. Точки применения

### 2.1 Заголовки HTTP-запроса клиента (`server.py:150–152`)

```python
for k, v in redact_headers(self.headers).items():
    _dr(req_id, f"  {k}: {v}")
```

Функция `redact_headers()` (redact.py:36) проходит по **каждому** заголовку:

- Если имя заголовка (нижний регистр) = `authorization` → пропускает через `redact()`.
- Остальные заголовки → без изменений.

**Что маскируется:** заголовок `Authorization: Bearer <client_token>`.

**Как выглядит в логе:**

```
[2026-08-30T14:32:01] [abc123def456]   Authorization: Bearer sk-abc1***REDACTED***xQw9
[2026-08-30T14:32:01] [abc123def456]   X-Claude-Code-Session-Id: sess-12345
```

**Как найти в логах:**

```bash
grep "Authorization:" session-debug.log
```

---

### 2.2 Входной запрос Anthropic (`server.py:161`)

```python
_dr(req_id, f"[BODY] {(_body if ADAPTER_DEBUG_BODY_FULL else _body[:ADAPTER_DEBUG_TRIM])}")
```

**Важный нюанс:** эта строка **НЕ** проходит через `redact()`, потому что `_body` — это Python-строка, встроенная прямо в f-string. Вызов `_dr()` оборачивает итоговую строку в `redact()` — **НО** `_body` уже полностью сформирован до вызова `_dr()`.

В зависимости от флагов:

| Флаг | Что попадает в лог | Маскируется? |
|---|---|---|
| `ADAPTER_DEBUG_BODY_FULL=0` (default) | первые 3000 символов (`ADAPTER_DEBUG_TRIM`) | **Да** — через `_dr()` → `redact()` |
| `ADAPTER_DEBUG_BODY_FULL=1` | полный текст тела | **НЕТ** — полный raw-текст записывается без обработки |

При `ADAPTER_DEBUG_BODY_FULL=0` маскация работает через `_dr()` → `redact()`, потому что внутри тела JSON есть пары типа `key: value` и потенциально `Bearer ...` — оба паттерна `redact()` подхватывают эти строки.

**Как найти:**

```bash
grep "\[BODY\]" session-debug.log
```

**Риск:** при `ADAPTER_DEBUG_BODY_FULL=1` тело клиента записывается **полностью** — включая все сообщения, инструменты, system prompt. Если кто-то в промпте случайно передал токен — он окажется в логе в открытом виде.

---

### 2.3 Тело запроса на бэкенд OpenAI (`server.py:324`)

```python
_dr(req_id, f"[OPENAI_BODY] {(json.dumps(openai_body, ensure_ascii=False) if ADAPTER_DEBUG_OPENAI_BODY_FULL else json.dumps(openai_body, ensure_ascii=False)[:ADAPTER_DEBUG_TRIM])}")
```

Аналогично пункту 2.2:

| Флаг | Что попадает в лог | Маскируется? |
|---|---|---|
| `ADAPTER_DEBUG_OPENAI_BODY_FULL=0` (default) | первые 3000 символов | **Да** — через `_dr()` → `redact()` |
| `ADAPTER_DEBUG_OPENAI_BODY_FULL=1` | полный JSON | **НЕТ** — raw-текст |

**Риск:** тело запроса бэкенду содержит `Authorization: Bearer <key>` как отдельное поле (не заголовок), но `openai_body` — это JSON, который не содержит заголовок Authorization. Токен бэкенда передаётся только как HTTP-заголовок и в лог **не пишется** в этом месте. Токен клиента тоже здесь отсутствует — это тело OpenAI-формата, которое генерируется из данных Anthropic-запроса.

---

### 2.4 Ошибки бэкенда (`server.py:379`, `491`)

```python
_trace(session_id, req_id, "backend_result", ..., error=redact(err[:500]))
```

Ответы об ошибках от бэкенда (JSON-ошибки) — пропускаются через `redact()` перед записью в trace-файл.

**Как найти:**

```bash
grep '"event": "backend_result"' trace.jsonl | grep '"error"'
```

---

### 2.5 Логирование через `_d()` и `_dr()` (`logger.py:20`)

```python
def _d(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    line = f"[{ts}] {redact(msg)}"   # ← каждый вызов _d() → redact()
```

**Каждое** сообщение, прошедшее через `_d()` или `_dr()`, проходит через `redact()`. Это включает:

- Метаданные запроса (model, max_tokens, tool_count и т.д.)
- Результаты resolve backend
- Статусы стриминга
- Таймауты и ошибки
- Имена инструментов и скиллов

**Что маскируется:** если в любом из этих сообщений случайно попадёт строка вида `ADAPTER_HOME_KEY=sk-xyz` или `Bearer abc123...` — она будет замаскирована.

---

### 2.6 Trace-записи (`tracer.py:87`)

```python
def _trace(session_id, req_id, event, **fields):
    ...
    line = json.dumps(record, ensure_ascii=False, default=str)
    line = redact.redact(line)     # ← каждая trace-строка → redact()
```

**Каждая** JSONL-строка trace проходит через `redact()`. Это покрывает все trace-события:

| Event | Поля, потенциально содержащие секреты | Маскируется? |
|---|---|---|
| `request_start` | path, tool_names | Нет (нет секретов) |
| `model_map` | agent_model, backend_model | Нет |
| `tool_result` | content (результат выполнения tool) | **Да** — если content содержит `Bearer ...` или `KEY=value` |
| `response_content` | text, input (аргументы tool call), reasoning | **Да** |
| `skill_signal` | tool_name, evidence | **Да** |
| `backend_result` | error | **Да** (явный вызов `redact()` в вызывающем коде) |

**Как найти:**

```bash
grep '"event": "tool_result"' trace.jsonl
grep '"event": "response_content"' trace.jsonl
```

---

### 2.7 Response от бэкенда — НЕ проходит через redact!

В нестриминговой ветке (`server.py:479`):

```python
_dr(req_id, f"[RESPONSE] {json.dumps(anthropic_resp, ensure_ascii=False)[:800]}")
```

Эта строка проходит через `_dr()` → `redact()`. **НО** ограничена 800 символами и содержит только сгенерированный ответ модели (текст, tool calls) — секретов там быть не должно, если модель не вернула токены в ответе.

**В потоковой ветке (streaming) response вообще НЕ логируется** — SSE-чанки идут напрямую в `wfile` клиента через `_sse_write()` без какой-либо обработки. Это означает, что ответ модели в потоковом режиме **нигде не записывается в лог**.

---

## 3. Поток данных и карта маскирования

Ниже показан полный путь данных и точки санитизации:

```
Claude Code → Адаптер
  │
  │  1. HTTP-заголовки (Authorization: Bearer <client_token>)
  │  └─ redact_headers() → redact()        ✅ МАССИРУЕТСЯ
  │
  │  2. Тело Anthropic-запроса (messages, system, tools)
  │  └─ _dr() → redact() [по умолчанию, первые 3000 символов]
  │     или raw при ADAPTER_DEBUG_BODY_FULL=1  ⚠️ РИСК
  │
  ▼
Адаптер (конвертация)
  │
  │  3. Тело OpenAI-запроса (сгенерированное)
  │  └─ _dr() → redact() [по умолчанию, первые 3000 символов]
  │     или raw при ADAPTER_DEBUG_OPENAI_BODY_FULL=1  ⚠️ РИСК
  │
  │  4. Authorization: Bearer <backend_key>
  │  └─ ТОЛЬКО как HTTP-заголовок, в лог НЕ пишется  ✅ СЕКРЕТНО
  │
  ▼
Backend
  │
  │  5. Ответ ошибки бэкенда (JSON)
  │  └─ redact() перед записью в trace  ✅ МАССИРУЕТСЯ
  │
  ▼
Клиент
  │
  │  6. Стриминговый ответ (SSE)
  │  └─ напрямую в wfile, в лог НЕ идёт  ℹ️ Не логируется
  │
  │  7. Нестриминговый ответ
  │  └─ _dr() → redact(), первые 800 символов  ✅ МАССИРУЕТСЯ
```

---

## 4. Что НЕ маскируется

Ни один из двух паттернов **не покрывает**:

1. **Ключи JSON в нижнем регистре** — паттерн 2 требует `[A-Z0-9_]`:
   ```json
   {"api_key": "sk-live-abc123"}        → ❌ не совпадёт (нижний регистр)
   {"access_token": "abc123"}           → ❌ не совпадёт (нет _TOKEN, нижний регистр)
   {"secret_key": "abc123"}             → ❌ не совпадёт (нижний регистр, нет _SECRET)
   ```
   Паттерн сработает **только** если ключ в верхнем регистре: `"API_KEY": "sk-..."` или `"ADAPTER_HOME_KEY=sk-..."`.

2. **Plain-text токены без префикса Bearer** — токен, не следующий за словом `Bearer`, не попадёт под паттерн 1.

3. **Стриминговый ответ** — SSE-чанки не логируются вообще.

4. **Тело при полных флагах** (`ADAPTER_DEBUG_BODY_FULL=1`, `ADAPTER_DEBUG_OPENAI_BODY_FULL=1`) — raw-текст записывается без `redact()`.

5. **Полное отключение санитайзера** — при `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` маскация отключается на всех точках одновременно (см. §4.1).

### 4.1 Полный рубильник: `ADAPTER_SENSITIVE_LOGGING_ENABLE`

| Переменная | Default | Эффект |
|---|---|---|
| `ADAPTER_SENSITIVE_LOGGING_ENABLE` | `0` (санитайзер активен) | **Полное отключение** `redact()` на всех 7+ точках применения |

Значения, включающие санитайзер: `0`, `false`, `no`, пусто (любое другое → отключён).

Когда флаг установлен в `1` (или `true`, `yes`):

- `_d()` / `_dr()` записывают строки **без** вызова `redact()` — полные токены, заголовки, ключи выводятся в открытом виде
- `_trace()` записывает JSONL **без** вызова `redact.redact()` — секренты появляются в trace-файле без маскировки
- Прямые вызовы `redact()` в `server.py` (заголовки, ошибки) заменяются на ветки без маскировки

**Область действия:** **только логи** — `session-debug.log`, `trace.jsonl`, stdout. Санитайзер никогда не модифицирует сетевой трафик — запросы и ответы от/к бэкенду передаются в полном, немаскированном виде независимо от этого флага.

**Когда использовать:** отладка, когда нужно увидеть полные токены/ключи в логах (например, проверить формат токена, длину, спецсимволы). **Никогда не включайте на продакшене** — все секренты окажутся в логах в открытом виде.

**Вид логов при отключённом санитайзере:**

```
# До (санитайзер активен):
[2026-08-30T14:32:01] [abc123]   Authorization: Bearer sk-abc1***REDACTED***xQw9
[2026-08-30T14:32:01] [abc123]   ADAPTER_HOME_KEY: sk-abc1***REDACTED***xQw9

# После (ADAPTER_SENSITIVE_LOGGING_ENABLE=1):
[2026-08-30T14:32:01] [abc123]   Authorization: Bearer sk-abc123xyzQw9
[2026-08-30T14:32:01] [abc123]   ADAPTER_HOME_KEY: sk-abc123xyzQw9
```

---

## 5. Как искать записи санитизации в логах

### Debug-лог (человекочитаемый)

```bash
# Найти все строки, содержащие "***REDACTED***"
grep "REDACTED" session-debug.log

# Найти маскированный Authorization-заголовок
grep "Authorization: Bearer" session-debug.log

# Найти все записи BODY с маской
grep "\[BODY\].*REDACTED" session-debug.log

# Проверить, что санитайзер активен (должны быть REDACTED-строки)
grep -c "REDACTED" session-debug.log
# Сравнить с общим числом строк — если 0, санитайзер отключён или секретов нет
wc -l session-debug.log
```

**Если `ADAPTER_SENSITIVE_LOGGING_ENABLE=1`** — строки с `REDACTED` в логах **не появятся**, так как санитайзер отключён. Вместо этого полные токны будут видны напрямую:

```bash
# Найти полные Bearer-токены (только при отключённом санитайзере)
grep -i "Bearer [A-Za-z0-9]" session-debug.log

# Найти пары ключ=значение (только при отключённом санитайзере)
grep "ADAPTER.*KEY\s*[:=]" session-debug.log
```

### Trace-лог (JSONL)

```bash
# Найти все trace-события с маскированными данными
grep "REDACTED" trace.jsonl

# Найти ошибки бэкенда с маской
grep '"event": "backend_result"' trace.jsonl | grep "REDACTED"

# Найти результаты tool calls (могут содержать секреты в content)
grep '"event": "tool_result"' trace.jsonl | grep "REDACTED"
```

**При `ADAPTER_SENSITIVE_LOGGING_ENABLE=1`** — ищите полные токены напрямую:

```bash
# Найти Bearer-токены в trace
grep -o '"error":"Bearer [^"]*"' trace.jsonl

# Найти tool_result с потенциальными секретами в content
grep '"event": "tool_result"' trace.jsonl | grep -v "REDACTED"
```

### Подсчёт покрытия

```bash
# Сколько строк с секретами было замаскировано (приблизительно)
grep -c "REDACTED" session-debug.log

# Сравнить с общим числом строк
wc -l session-debug.log

# Если REDACTED не найдено — проверить, не отключён ли санитайзер
grep "REDACTED" session-debug.log || echo "No redactions — sanitizer may be disabled"
```

---

## 6. Сводная таблица точек санитизации

| Точка | Что маскируется | Как | Флаг-исключение |
|---|---|---|---|
| HTTP-заголовки | `Authorization: Bearer ...` | `redact_headers()` → `redact()` | `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` |
| Тело Anthropic-запроса | `Bearer ...`, `KEY=value`, `_TOKEN=...` | `_dr()` → `redact()` | `ADAPTER_DEBUG_BODY_FULL=1` **или** `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` |
| Тело OpenAI-запроса | `Bearer ...`, `KEY=value` | `_dr()` → `redact()` | `ADAPTER_DEBUG_OPENAI_BODY_FULL=1` **или** `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` |
| Ошибки бэкенда | `Bearer ...`, `KEY=value` | явный `redact()` | `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` |
| `_d()` / `_dr()` вызовы | любое совпадение с паттернами | `redact()` | `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` |
| Trace-записи | любое совпадение с паттернами | `redact()` | `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` |
| Стриминговый ответ (SSE) | — | **не логируется** | N/A |
| Нестриминговый ответ | `Bearer ...`, `KEY=value` | `_dr()` → `redact()` | `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` |

> **Общий рубильник:** `ADAPTER_SENSITIVE_LOGGING_ENABLE=1` отключает маскировку **одновременно на всех строках** — это не отдельное исключение, а глобальный переключатель, который заменяет все `redact()`-вызовы.
