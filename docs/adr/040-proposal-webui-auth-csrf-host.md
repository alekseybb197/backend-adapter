# ADR-040: Защита WEBUI и прокси — `Host`, `Origin`, basic auth, ключ прокси, маскирование

| Поле | Значение |
|---|---|
| Статус | Предложено (2026-10-11); basic auth уже запланирован автором (вопрос 11) |
| Тип решения | proposal |
| Связано | ADR-020, ADR-023, ADR-024 |

## Подтверждённые проблемы (v1.0.0)

| # | Проблема | Как проверено | Последствие |
|---|---|---|---|
| 1 | **CSRF**: изменяющие POST WEBUI без проверки `Origin`/`Referer` | form-POST с `Origin: https://evil.example` на `/config` переключил `ADAPTER_SENSITIVE_LOGGING_ENABLE` в «вкл.» | любая открытая в браузере страница отключает маскирование секретов, меняет TARGET; изменение сохраняется в `state.yaml` |
| 2 | **DNS rebinding**: запрос с чужим `Host` обслуживается | `Host: attacker.example` → 200 | страница злоумышленника читает `/session`, `.err` с полными запросами агента |
| 3 | **Нет аутентификации** на WEBUI и на порту прокси | — | при привязке не к loopback: открытый релей к бэкенду с токеном адаптера |
| 4 | **`x-api-key` в консоли открытым текстом** | реальный путь `redact_headers` → `_dr`: `x-api-key: sk-ant-api03-…` выведен целиком, `Bearer` замаскирован | [CC] передаёт `ANTHROPIC_API_KEY` в `x-api-key`; консоль безусловна, под systemd — journald |
| 5 | `redact()` не маскирует `*_PASSWORD` и `Authorization: Basic` | вызов `redact()` | с basic auth пароль окажется в логах |

Basic auth **не закрывает** 1 и 2: браузер сам подставляет сохранённые
учётные данные в межсайтовый form-POST, а при rebinding страница уже
находится «внутри» источника. Поэтому защита — слоями, и первые два слоя
обязательны независимо от аутентификации.

## Решение

Все проверки WEBUI — в единственной точке входа `webserver.Handler._dispatch`,
до сопоставления эндпойнта.

### Слой 1 — проверка `Host` (всегда)

Допустимые значения: `127.0.0.1:<port>`, `localhost:<port>`, `[::1]:<port>`
плюс список `ADAPTER_WEBUI_ALLOWED_HOSTS` (через запятую) — обязателен, если
`ADAPTER_WEBUI_HOST` не loopback. Иначе — `421 Misdirected Request`.
Закрывает DNS rebinding.

### Слой 2 — проверка источника для изменяющих методов (всегда)

Для `POST` (и любых не-`GET`/`HEAD`):

1. `Sec-Fetch-Site: cross-site` → 403;
2. есть `Origin` → он обязан совпадать с `http(s)://<Host>`, иначе 403;
3. нет `Origin`, есть `Referer` → его origin обязан совпадать, иначе 403;
4. нет ни того, ни другого → пропустить: так ходят `curl` и скрипты, а
   браузеры при межсайтовом POST `Origin` отправляют всегда.

### Слой 3 — basic auth (по конфигурации; обязателен вне loopback)

- `ADAPTER_WEBUI_USER`, `ADAPTER_WEBUI_PASSWORD`. Если `ADAPTER_WEBUI_HOST`
  не loopback, а учётные данные не заданы — `[FATAL]` на старте: открытый в
  сеть WEBUI без аутентификации запустить нельзя (fail-safe).
- Сравнение — `hmac.compare_digest` по байтам всей строки `user:password`
  (постоянное время, без раннего выхода на имени).
- Ответ без учётных данных: `401` + `WWW-Authenticate: Basic
  realm="backend-adapter", charset="UTF-8"`.
- Исключения из аутентификации (не из слоёв 1–2): `/healthz`, `/health`,
  `/live`, `/ready` — для проб оркестратора; список настраивается.
- Basic auth по HTTP — пароль в открытом виде в каждом запросе. Вне loopback
  — только за TLS-терминирующим прокси; это должно быть написано в
  `docs/webui.md` рядом с переменными.

### Слой 4 — ключ порта прокси (по конфигурации; обязателен вне loopback)

`ADAPTER_ENDPOINT_KEY`: если задан, запрос к `/v1/*` обязан нести его в
`x-api-key` или `Authorization: Bearer` (сравнение `hmac.compare_digest`),
иначе 401 в формате ошибки входного протокола. Клиенты уже умеют это
передавать: [CC] — `ANTHROPIC_API_KEY`, Codex — `env_key`, QwenCode —
`apiKey`. При `ADAPTER_ENDPOINT_HOST` не loopback без ключа — `[FATAL]`.
Ключ проверяется адаптером и **не пересылается** бэкенду (у бэкенда свой
токен из `adapter.yaml`).

### Слой 5 — маскирование (исправление, независимо от остального)

Проверенный прототип (`redact.py`):

```python
_SECRET_PATTERNS = [
    (re.compile(r"(Bearer\s+)([A-Za-z0-9\-_\.=/+]{6,})", re.I),
     lambda m: m.group(1) + _mask(m.group(2))),
    # Basic — base64 «user:password»: даже 4 символа раскрывают начало имени
    (re.compile(r"(Basic\s+)([A-Za-z0-9+/=]{4,})", re.I),
     lambda m: m.group(1) + "***REDACTED***"),
    (re.compile(r'((?:[A-Z0-9_\-]*(?:_PAT|_KEY|_TOKEN|_SECRET|_PASSWORD|_PASSWD|API[_\-]KEY)'
                r'[A-Z0-9_\-]*)\s*[:=]\s*["\']?)([^\s"\']{8,})', re.I),
     lambda m: m.group(1) + _mask(m.group(2))),
]

_SENSITIVE_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "x-api-key", "api-key", "cookie", "set-cookie"}
)


def redact_headers(headers) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADERS:
            scheme, _, rest = v.partition(" ")
            if rest and scheme.lower() == "basic":
                out[k] = "Basic ***REDACTED***"
            elif rest and scheme.isalpha():
                out[k] = f"{scheme} {_mask(rest)}"
            else:
                out[k] = _mask(v)
        else:
            out[k] = redact(v)
    return out
```

Проверено: `x-api-key: sk-ant-api03-…` → `sk-a***REDACTED***9xyz`;
`Authorization: Basic YWRt…` → `Basic ***REDACTED***`;
`ADAPTER_WEBUI_PASSWORD=hunter2secret` → `hunt***REDACTED***cret`;
`Cookie` маскируется; `Content-Type`, id сессии,
`ADAPTER_MAX_TOKENS_MAX=16384` не затрагиваются.

### Каркас проверки в `_dispatch`

```python
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_SAFE_METHODS = ("GET", "HEAD")


def _guard(self, method: str, path: str) -> bool:
    """True — пропустить запрос; False — ответ уже отправлен."""
    host = (self.headers.get("Host") or "").strip().lower()
    if host not in self.context.allowed_hosts:              # слой 1
        self._write(421, "text/plain; charset=utf-8", b"Misdirected Request")
        return False
    if method not in _SAFE_METHODS:                          # слой 2
        if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
            self._write(403, "text/plain; charset=utf-8", b"Cross-site request refused")
            return False
        source = self.headers.get("Origin") or _origin_of(self.headers.get("Referer"))
        if source and source.lower() not in (f"http://{host}", f"https://{host}"):
            self._write(403, "text/plain; charset=utf-8", b"Origin mismatch")
            return False
    if self.context.auth and not _is_auth_exempt(path):      # слой 3
        if not _check_basic(self.headers.get("Authorization"), self.context.auth):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="backend-adapter", charset="UTF-8"')
            self.send_header("Content-Length", "0")
            self.end_headers()
            return False
    return True
```

`allowed_hosts` строится в `serve()` из порта, loopback-имён и
`ADAPTER_WEBUI_ALLOWED_HOSTS`.

## Порядок внедрения

1. Слой 5 (маскирование) — сразу, это исправление утечки.
2. Слои 1–2 — сразу, не требуют настройки и не ломают ни браузер на
   `127.0.0.1`, ни `curl`.
3. Слои 3–4 — вместе с запланированным basic auth.

## Тесты

Для каждого слоя — отрицательные и положительные случаи через реальный
HTTP-сервер (как `tests/test_webserver.py`): чужой `Host` → 421;
`Origin` чужой → 403, свой → 200, отсутствует (curl) → 200;
`Sec-Fetch-Site: cross-site` → 403; неверный пароль, неверное имя,
отсутствие заголовка → 401; health без учётных данных → 200; `[FATAL]` при
не-loopback без учётных данных.
