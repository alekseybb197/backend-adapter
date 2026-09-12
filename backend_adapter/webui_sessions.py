#!/usr/bin/env python3
"""
webui_sessions.py — эндпойнт "/sessions" общего веб-сервера WEBUI:
таблица сессий агентов и пер-сессионное управление логированием и роутингом.

До v0.9.5 таблица Sessions жила секцией статус-страницы "/" и была чистым
НАБЛЮДЕНИЕМ. Теперь она — отдельная страница (ссылка 🗂 со статус-страницы,
обратно — «Статус 📊») и единица УПРАВЛЕНИЯ: каждая строка позволяет, не
трогая общие настройки приложения, включить/выключить для своей сессии
файловые логи и parts-дампы и переназначить TARGET-маршрутизацию входного
эндпойнта (см. session_settings).

Колонки строки (13):

    Сессия | Агент | Модель | Бэкенд | Входной эндпойнт | Маршрут |
    Последнее обращение | Вызовов | Ошибок | Log | Parts | TARGET | Actions

  - «Входной эндпойнт» — константа строки: путь запроса выбирает АГЕНТ
    (messages|completions|responses), адаптер его не меняет. Это же значение
    выбирает, какая TARGET-переменная адресуется селектом строки;
  - «Маршрут» — КУДА адаптер решил отправить (passthrough/convert/reject/
    disabled): производная от TARGET на момент обращения;
  - «Log»/«Parts» — селекты ADAPTER_DEBUG/ADAPTER_DEBUG_PARTS (inherit/on/
    off): объём файловой записи для сессии;
  - «TARGET» — селект TARGET-переменной входного эндпойнта строки
    (inherit + домен config.TARGET_ALLOWED_VALUES);
  - «Ошибок» — счётчик; при наличии .err-файла сессии число становится
    ссылкой на него (/logs/<имя>, открывается в НОВОМ окне браузера);
  - «Actions» — ⏪ (обнулить счётчики ЭТОЙ строки-кортежа) и 🗑 (удалить
    строку).

Все селекты и кнопки — обычные HTML-формы (PRG через 303 на GET /sessions),
без JS: изменение селекта отправляет форму сразу (onchange="this.form.submit()").
Метка варианта «inherit» несёт действующее значение общей настройки
приложения — видно, к чему вернётся сессия (задача 4: дефолт сессии —
общие настройки).

Счётчики Вызовов/Ошибок обновляются без перезагрузки: безусловный JS
sessions_poll каждые 5 с опрашивает /api/sessions/snapshot и правит ячейки
по data-атрибутам (data-calls — textContent, data-errors — innerHTML:
errors_html приходит с сервера ГОТОВЫМ HTML — числом или ссылкой, как
cost_html у таблицы моделей). Строки сопоставляются по data-key
(JSON-строка кортежа): одна сессия может занимать НЕСКОЛЬКО строк.

Раздача .err-файлов: GET /logs/<имя> отдаёт файл из корня WEBUI
(WebContext.root_dir = ADAPTER_DEBUG_LOGPATH) как text/plain. Имя
ограничено строгим шаблоном session-<дата>-<время>-<safe8>.err — ни слэшей,
ни «..» в нём быть не может, поэтому обход каталога исключён структурно.

Модуль — чистый эндпойнт: CLI/сервер/обработчик живут в ядре webserver.py.
"""

import html
import json
import logging
import os
import re
from urllib.parse import parse_qs, quote, urlparse

from . import (
    config,
    routing,
    session_log,
    session_registry,
    session_settings,
    webserver,
)

logger = logging.getLogger("webui_sessions")

# Имена .err-файлов, которые разрешено отдавать через /logs: ровно тот
# формат, что строит session_log._make_session_file ("session-<YYYYMMDD>-
# <HHMMSS>-<safe8>.err"). Строгая проверка ЗАМЕНЯЕТ защиту от обхода пути:
# в допустимом имени нет ни "/", ни "..", поэтому выйти за корень нельзя.
_ERR_NAME_RE = re.compile(r"^session-[0-9]{8}-[0-9]{6}-[A-Za-z0-9._-]{1,8}\.err$")

# Колонок в таблице сессий (для colspan пустого плейсхолдера).
_COLUMNS = 13


# ==================== ЧИСТАЯ ЛОГИКА ====================


def _bool_options(name: str) -> tuple[tuple[str, str], ...]:
    """Варианты селекта bool-настройки: inherit/on/off.

    Метка «inherit» несёт ДЕЙСТВУЮЩЕЕ значение общей настройки приложения —
    видно, к чему вернётся сессия, сняв переопределение."""
    inherited = "on" if getattr(config, name, False) else "off"
    return (("inherit", f"inherit ({inherited})"), ("1", "on"), ("0", "off"))


def _target_options(name: str) -> tuple[tuple[str, str], ...]:
    """Варианты селекта TARGET: inherit + домен config.TARGET_ALLOWED_VALUES.

    «inherit» — тоже полноценное хранимое значение (см. session_settings):
    метка несёт общую настройку приложения."""
    inherited = getattr(config, name, "none")
    options = [("inherit", f"inherit ({inherited})")]
    options.extend((value, value) for value in config.TARGET_ALLOWED_VALUES)
    return tuple(options)


def _stored_bool(session_id: str, name: str) -> str:
    """Хранимое переопределение bool-настройки как значение селекта
    ("inherit" — записи нет; "1"/"0" — on/off)."""
    value = session_settings.override(session_id, name)
    if value is None:
        return "inherit"
    return "1" if value else "0"


def _select_form_html(session_id: str, name: str, options, selected: str) -> str:
    """HTML ячейки настройки: форма + выпадающий список, отправляющийся сразу.

    POST уходит на /api/sessions/settings с query ``session``+``name``
    (имя настройки — ровно env-переменная пула, напр. ADAPTER_DEBUG), а
    значение — поле ``value``. ``onchange="this.form.submit()"`` отправляет
    форму без отдельной кнопки (одно поле — одно действие)."""
    opts = "".join(
        f'<option value="{html.escape(value)}"'
        f"{' selected' if value == selected else ''}>"
        f"{html.escape(label)}</option>"
        for value, label in options
    )
    qs = f"session={quote(str(session_id), safe='')}&name={quote(str(name), safe='')}"
    return (
        f'<form method="post" action="/api/sessions/settings?{qs}" style="margin:0">'
        f'<select name="value" onchange="this.form.submit()" '
        f'style="font:inherit;max-width:170px">{opts}</select>'
        "</form>"
    )


def _input_cell_html(row: dict) -> str:
    """HTML ячейки «Входной эндпойнт»: формат входа + путь в title.

    Формат задаёт АГЕНТ выбором пути запроса (/v1/messages и т.д.) — для
    строки он константен. Пустое/неизвестное значение (битый сид, старые
    строки до v0.9.5) → серая «—»."""
    inp = str(row.get("input", ""))
    # Перебором, а не INPUT_PATHS.get(inp): ключ словаря — Literal-тип Format,
    # а inp — произвольная строка из строки реестра, поэтому mypy strict не
    # даст проиндексировать словарь строкой без ignore. Таблица из 3 записей —
    # линейный поиск дешевле, чем cast/ignore.
    path = next((p for fmt, p in routing.INPUT_PATHS.items() if fmt == inp), None)
    if path is None:
        return '<span style="color:#aaa">—</span>'
    return f'<code title="{html.escape(path)}">{html.escape(inp)}</code>'


def _errors_cell_html(row: dict) -> str:
    """HTML ячейки «Ошибок»: число; при наличии .err-файла — ссылка на него.

    Файл ошибок сессии (session-<ts>-<safe8>.err) открывается в НОВОМ окне
    браузера (target="_blank", v0.9.5 — задача 7). Файла ещё нет (сессия не
    писала логов) — просто число без ссылки. Тот же HTML отдаётся и в снимке
    (/api/sessions/snapshot → errors_html), поэтому поллинг счётчиков ставит
    ссылку тем же рендером, что и страница."""
    errors = row.get("errors", 0)
    name = session_log.error_file_name(str(row.get("session", "")))
    if not name:
        return str(errors)
    return (
        f'<a href="/logs/{quote(name, safe="")}" target="_blank" rel="noopener" '
        f'title="Открыть файл ошибок сессии ({html.escape(name)})">{errors}</a>'
    )


def _actions_cell_html(row: dict) -> str:
    """HTML ячейки Actions: ⏪ (обнулить счётчики строки) + 🗑 (удалить строку).

    Обе кнопки — PRG-формы (POST → 303 на GET /sessions). ⏪ обнуляет
    счётчики ТОЛЬКО этой строки-кортежа (сессия может занимать несколько
    строк — соседние не трогаются); 🗑 удаляет строку целиком. ``key`` —
    компактная JSON-строка кортежа (session_registry.key_json)."""
    q = quote(str(row.get("key", "")), safe="")
    btn = "background:none;border:none;padding:0;font:inherit;cursor:pointer;margin-right:6px"
    return (
        "<td>"
        f'<form method="post" action="/api/sessions/reset?key={html.escape(q)}" '
        'style="display:inline">'
        f'<button type="submit" aria-label="Сбросить счётчики строки сессии" '
        f'title="Сбросить счётчики строки сессии" style="color:#555;{btn}">⏪</button>'
        "</form>"
        f'<form method="post" action="/api/sessions/delete?key={html.escape(q)}" '
        'style="display:inline">'
        f'<button type="submit" aria-label="Удалить строку сессии" '
        f'title="Удалить строку сессии" style="color:#c0392b;{btn}">🗑</button>'
        "</form>"
        "</td>"
    )


def _sessions_rows_html(rows: list[dict]) -> str:
    """HTML строк таблицы Sessions (по строке на КОРТЕЖ session+agent+model+
    backend+route).

    ``rows`` — session_registry.sessions_snapshot() (уже отсортирован: новые
    сверху). Одна сессия может занимать НЕСКОЛЬКО строк (сменила модель/
    обработчик) — поэтому ``session`` не уникален: строка несёт data-key с
    JSON-строкой всего кортежа, и JS sessions_poll сопоставляет строки по
    нему (порядок меняется при всплытии строки вверх). data-session (полный
    session_id) остаётся для наглядности/отладки. Сессия — UUID-подобный id:
    показываем первые 8 символов в <code>, полный id — в title. Все значения —
    html.escape; счётчики несут data-атрибуты (data-calls/data-errors) —
    поллинг правит ячейки по ним, а не по позиции колонки."""
    body = []
    for row in rows:
        session = str(row.get("session", ""))
        key = str(row.get("key", ""))
        body.append(
            f'<tr data-session="{html.escape(session)}" data-key="{html.escape(key)}">'
            f'<td><code title="{html.escape(session)}">{html.escape(session[:8])}</code></td>'
            f"<td>{html.escape(str(row.get('agent', '')))}</td>"
            f"<td>{html.escape(str(row.get('model', '')))}</td>"
            f"<td>{html.escape(str(row.get('backend', '')))}</td>"
            f"<td>{_input_cell_html(row)}</td>"
            f"<td>{html.escape(str(row.get('route', '')))}</td>"
            f"<td>{html.escape(str(row.get('last_seen', '')))}</td>"
            f'<td data-calls="{row.get("calls", 0)}">{row.get("calls", 0)}</td>'
            f'<td data-errors="{row.get("errors", 0)}">{_errors_cell_html(row)}</td>'
            f"<td>{_select_form_html(session, 'ADAPTER_DEBUG', _bool_options('ADAPTER_DEBUG'), _stored_bool(session, 'ADAPTER_DEBUG'))}</td>"
            f"<td>{_select_form_html(session, 'ADAPTER_DEBUG_PARTS', _bool_options('ADAPTER_DEBUG_PARTS'), _stored_bool(session, 'ADAPTER_DEBUG_PARTS'))}</td>"
            f"<td>{_target_cell_html(row)}</td>"
            f"{_actions_cell_html(row)}"
            "</tr>"
        )
    if not body:
        body.append(
            f'<tr><td colspan="{_COLUMNS}" style="color:#888">пока нет данных — '
            "таблица заполняется при обращениях агентов</td></tr>"
        )
    return "".join(body)


def _target_cell_html(row: dict) -> str:
    """HTML ячейки TARGET: селект TARGET-переменной ВХОДНОГО эндпойнта строки.

    Селект адресует пер-сессионное переопределение именно той переменной,
    которая управляет входом строки (ADAPTER_MESSAGES_TARGET и т.д.) —
    переопределение живёт на сессии, поэтому влияет на все строки той же
    сессии с тем же входом. Неизвестный вход (нет переменной) → серая «—»."""
    inp = str(row.get("input", ""))
    if inp not in routing.INPUT_PATHS:
        return '<span style="color:#aaa">—</span>'
    name = routing.target_env_name(inp)
    stored = session_settings.override(str(row.get("session", "")), name)
    selected = "inherit" if stored is None else str(stored)
    return _select_form_html(str(row.get("session", "")), name, _target_options(name), selected)


def _render_sessions_page(context) -> bytes:
    """HTML страницы "/sessions": таблица сессий + поллинг счётчиков.

    Ссылки страницы (назад на статус, в /config, на .err-файл) ведут в ТЕКУЩЕМ
    окне — исключение только .err-файл (открывается в новом окне, задача 7).
    Обратные ссылки статус-страницы наоборот открываются в новом окне
    (задача 2)."""
    rows = session_registry.sessions_snapshot()
    html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>backend-adapter — сессии</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; color: #222; }}
  table {{ border-collapse: collapse; margin-top: 12px; }}
  td, th {{ border: 1px solid #ddd; padding: 6px 10px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; }}
  code {{ font-size: 13px; }}
</style>
{sessions_poll_script}
</head>
<body>
<h2>Backend-Adapter Version {html.escape(context.version)} — сессии</h2>
<p><a href="/">Статус 📊</a> &nbsp;·&nbsp; <a href="/session">Обзор сессий 📋</a> &nbsp;·&nbsp; <a href="/config">Runtime config 🔧</a></p>
<p style="color:#666;font-size:13px;max-width:1100px">
  Строка — кортеж <code>session+agent+model+backend+route</code>: смена модели
  или обработчика даёт новую строку. Колонки <b>Log</b>/<b>Parts</b> и
  <b>TARGET</b> переопределяют настройки <b>только для этой сессии</b>; вариант
  «inherit» означает «взять общую настройку приложения» (её значение показано в
  скобках). Изменение применяется сразу и действует на последующие запросы.
</p>
<table>
  <tr><th>Сессия</th><th>Агент</th><th>Модель</th><th>Бэкенд</th><th>Входной эндпойнт</th><th>Маршрут</th><th>Последнее обращение</th><th>Вызовов</th><th>Ошибок</th><th>Log</th><th>Parts</th><th>TARGET</th><th>Actions</th></tr>
  {_sessions_rows_html(rows)}
</table>
</body>
</html>
"""
    return html_page.encode("utf-8")


# Live-обновление таблицы: JS sessions_poll каждые 5 с опрашивает
# /api/sessions/snapshot (session_registry.sessions_snapshot() — копии строк
# из памяти, сети нет) и обновляет счётчики. Строки сопоставляются ПО
# data-key (JSON-строка кортежа), а НЕ по data-session: одна сессия может
# занимать несколько строк, session не уникален. Ячейки ищутся по
# data-атрибутам (data-calls/data-errors), а не по позиции колонки —
# добавление/перестановка колонок поллинг не ломает. Вызовов — textContent,
# Ошибок — innerHTML (errors_html: число ИЛИ готовая ссылка на .err-файл).
# Состав/число строк не совпало (новый кортеж / эвикция по глубине) —
# location.reload() перерисует таблицу. Оверхед — один маленький JSON раз в
# 5 с на вкладку; скрытую вкладку браузер троттлит.
sessions_poll_script = """
<script>
  function sessions_poll() {
    fetch("/api/sessions/snapshot")
      .then(function (r) { return r.json(); })
      .then(function (rows) {
        var trs = document.querySelectorAll("tr[data-key]");
        if (trs.length !== rows.length) { location.reload(); return; }
        var byKey = {};
        for (var i = 0; i < rows.length; i++) { byKey[rows[i]["key"]] = rows[i]; }
        for (var j = 0; j < trs.length; j++) {
          var row = byKey[trs[j].getAttribute("data-key")];
          if (!row) { location.reload(); return; }
          var calls = trs[j].querySelector("[data-calls]");
          if (calls && String(calls.textContent) !== String(row["calls"])) {
            calls.textContent = row["calls"];
          }
          var errs = trs[j].querySelector("[data-errors]");
          if (errs && errs.innerHTML !== row["errors_html"]) {
            errs.innerHTML = row["errors_html"];
          }
        }
        setTimeout(sessions_poll, 5000);
      })
      .catch(function () { setTimeout(sessions_poll, 5000); });
  }
  window.addEventListener("load", function () { setTimeout(sessions_poll, 5000); });
</script>
"""


# ==================== ПРИМЕНЕНИЕ ПЕР-СЕССИОННОЙ НАСТРОЙКИ ====================


def _apply_setting(session_id: str, name: str, raw) -> bool:
    """Применить одну пер-сессионную настройку; True — принята.

    ``raw`` — либо уже типизированное значение (JSON-клиент: bool/строка),
    либо строка формы ("1"/"0"/"inherit"/значение домена). «inherit» у
    bool-настройки СНИМАЕТ переопределение (у неё нет состояния inherit —
    домен bool), у enum (TARGET) записывается явно как значение домена —
    см. session_settings. Возвращает False, если имя вне пула или значение не
    прошло валидацию (в таблицу переопределений ничего не попало)."""
    if not session_id or name not in config.SESSION_CONFIG_POOL:
        return False
    expected = config._SESSION_CONFIG_TYPES[name]
    if isinstance(raw, str):
        low = raw.strip().lower()
        if low == "inherit":
            if expected is bool:
                session_settings.set_config(session_id, clear=(name,))
                return True
            raw = "inherit"
        elif expected is bool:
            if low in ("1", "true", "on", "yes"):
                raw = True
            elif low in ("0", "false", "off", "no"):
                raw = False
            else:
                return False
    if not config.accepts_value(expected, raw):
        return False
    session_settings.set_config(session_id, {name: raw})
    return True


# ==================== ЭНДПОЙНТЫ ====================


@webserver.register
class SessionsPageEndpoint(webserver.Endpoint):
    """Эндпойнт "/sessions": страница таблицы сессий (v0.9.5, задача 1).

    Таблица переехала сюда со статус-страницы "/" (там осталась ссылка 🗂).
    GET → HTML-таблица + поллинг счётчиков; обратные ссылки («Статус 📊»,
    «Runtime config 🔧») ведут в текущем окне."""

    prefix = "/sessions"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        if remainder:
            handler.send_error(404, "Not found")
            return
        handler._write(200, "text/html; charset=utf-8", _render_sessions_page(self.context))


@webserver.register
class SessionsSnapshotEndpoint(webserver.Endpoint):
    """Эндпойнт "/api/sessions/snapshot": снимок таблицы Sessions (JSON).

    Лёгкий ответ для JS sessions_poll на странице /sessions:
    session_registry.sessions_snapshot() — список строк реестра (по строке на
    кортеж session+agent+model+backend+route: session/agent/input/model/
    backend/route/last_seen/calls/errors + служебный "key" — JSON-строка
    кортежа), отсортированный по времени последнего обращения (новые сверху).
    Каждая строка дополнена полем ``errors_html`` — готовым HTML ячейки
    «Ошибок» (число или ссылка на .err-файл; см. _errors_cell_html), как
    ``cost_html`` у /api/model-usage/snapshot. GET ничего не мутирует и не
    ходит в сеть к бэкендам — безопасно опрашивать каждые 5 с."""

    prefix = "/api/sessions/snapshot"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        rows = session_registry.sessions_snapshot()
        for row in rows:
            row["errors_html"] = _errors_cell_html(row)
        body = json.dumps(rows).encode("utf-8")
        handler._write(200, "application/json; charset=utf-8", body)


def _parse_session_key(raw: str) -> tuple[str, str, str, str, str] | None:
    """Разобрать query-параметр ``key`` в кортеж-ключ строки Sessions.

    ``key`` — компактная JSON-строка кортежа (session_registry.key_json,
    ровно она стоит в атрибуте ``data-key`` и в поле ``"key"`` снимка).
    Возвращает кортеж из 5 строк либо None, если это не JSON-список из 5
    строк (битый key, подделка, неверная арность) — эндпойнт отвечает 400."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, list) and len(parsed) == 5 and all(isinstance(x, str) for x in parsed):
        return (parsed[0], parsed[1], parsed[2], parsed[3], parsed[4])
    return None


@webserver.register
class SessionResetEndpoint(webserver.Endpoint):
    """POST /api/sessions/reset?key=<json> — обнуление счётчиков СТРОКИ.

    Кнопка ⏪ в строке таблицы Sessions (form method=post) работает по
    PRG-паттерну: обнуление счётчиков строки-кортежа (session_registry.
    reset_counters: только ``calls``/``errors``) + 303 See Other на GET
    /sessions — страница показывается GET-навигацией, обновление не повторяет
    POST. Обнуляется ИМЕННО СТРОКА: сессия может занимать несколько строк
    (сменила модель/обработчик), и счётчики соседних строк не трогаются.
    JSON-клиент (Content-Type: application/json) получает 200 {"ok": true,
    "key": ...} при успехе, 404 {"error": ...} — строки нет (второй клик,
    кортеж вытеснен по лимиту), 400 {"error": ...} — нет параметра key или
    key не является JSON-списком из 5 строк. GET на префикс — 404 дефолтом."""

    prefix = "/api/sessions/reset"

    def __init__(self, context):
        self.context = context

    def POST(self, handler, remainder: str):
        parsed = urlparse(handler.path)
        raw_key = parse_qs(parsed.query).get("key", [""])[0]
        ct = handler.headers.get("Content-Type", "")
        if "application/json" not in ct:
            # HTML-форма кнопки (application/x-www-form-urlencoded): PRG.
            key = _parse_session_key(raw_key)
            if key is not None:
                session_registry.reset_counters(key)  # нет строки — no-op
            handler._redirect("/sessions")
            return
        if not raw_key:
            body = b'{"error": "missing \'key\' query parameter"}'
            handler._write(400, "application/json; charset=utf-8", body)
            return
        key = _parse_session_key(raw_key)
        if key is None:
            body = b'{"error": "\'key\' must be a JSON array of 5 strings"}'
            handler._write(400, "application/json; charset=utf-8", body)
            return
        if not session_registry.reset_counters(key):
            body = json.dumps({"error": "session row not found"}).encode()
            handler._write(404, "application/json; charset=utf-8", body)
            return
        body = json.dumps({"ok": True, "key": raw_key}).encode("utf-8")
        handler._write(200, "application/json; charset=utf-8", body)


@webserver.register
class SessionDeleteEndpoint(webserver.Endpoint):
    """POST /api/sessions/delete?key=<json> — удаление строки таблицы Sessions.

    Кнопка 🗑 в конце строки таблицы Sessions (form method=post) работает по
    PRG-паттерну: удаление строки из реестра (session_registry.delete_key) +
    303 See Other на GET /sessions — страница показывается GET-навигацией,
    обновление не повторяет POST (как у кнопок Models in use). ``key`` —
    компактная JSON-строка кортежа (session+agent+model+backend+route): одна
    сессия может занимать несколько строк, поэтому удаляем именно
    строку-кортеж, а не все строки сессии. JSON-клиент (Content-Type:
    application/json) получает 200 {"ok": true, "key": ...} при успехе, 404
    {"error": ...} — строки нет (повторное удаление; строка уже вытеснена по
    лимиту), 400 {"error": ...} — нет параметра key или key не является
    JSON-списком из 5 строк (единый формат ошибки, как в server.py). GET на
    префикс — 404 дефолтом Endpoint."""

    prefix = "/api/sessions/delete"

    def __init__(self, context):
        self.context = context

    def POST(self, handler, remainder: str):
        parsed = urlparse(handler.path)
        raw_key = parse_qs(parsed.query).get("key", [""])[0]
        ct = handler.headers.get("Content-Type", "")
        if "application/json" not in ct:
            # HTML-форма кнопки (application/x-www-form-urlencoded): PRG.
            key = _parse_session_key(raw_key)
            if key is not None:
                session_registry.delete_key(key)  # нет строки — no-op
            handler._redirect("/sessions")
            return
        if not raw_key:
            body = b'{"error": "missing \'key\' query parameter"}'
            handler._write(400, "application/json; charset=utf-8", body)
            return
        key = _parse_session_key(raw_key)
        if key is None:
            body = b'{"error": "\'key\' must be a JSON array of 5 strings"}'
            handler._write(400, "application/json; charset=utf-8", body)
            return
        if not session_registry.delete_key(key):
            body = json.dumps({"error": "session row not found"}).encode()
            handler._write(404, "application/json; charset=utf-8", body)
            return
        body = json.dumps({"ok": True, "key": raw_key}).encode("utf-8")
        handler._write(200, "application/json; charset=utf-8", body)


@webserver.register
class SessionSettingsEndpoint(webserver.Endpoint):
    """POST /api/sessions/settings — пер-сессионные настройки (v0.9.5).

    Адресует переопределения сессии (session_settings): логирование
    (ADAPTER_DEBUG, ADAPTER_DEBUG_PARTS) и TARGET-роутинг входов
    (ADAPTER_{MESSAGES,COMPLETIONS,RESPONSES}_TARGET). Значение "inherit"
    у bool-настройки снимает переопределение, у TARGET — записывается явно.

    Два клиента:
      - HTML-форма селекта строки (form method=post): query ``session``+
        ``name`` (имя настройки = env-переменная пула), тело — поле ``value``
        (строка: "1"/"0"/"inherit"/значение домена). Ответ — PRG 303 на GET
        /sessions; битые имя/значение молча игнорируются (в норме их не
        бывает — селект собран сервером из домена).
      - JSON-клиент (Content-Type: application/json): либо {"session", "name",
        "value"} — одна настройка, либо {"session", "values": {...},
        "clear": [...]} — массово (как session_settings.set_config). 200
        {"ok": true, "session": ..., "overrides": {...}} — КОПИЯ
        переопределений сессии после применения; 400 {"error": ...} — нет
        session / невалидное имя или значение / битый JSON. GET — 404."""

    prefix = "/api/sessions/settings"

    def __init__(self, context):
        self.context = context

    def POST(self, handler, remainder: str):
        ct = handler.headers.get("Content-Type", "")
        if "application/json" not in ct:
            parsed = urlparse(handler.path)
            query = parse_qs(parsed.query)
            session_id = query.get("session", [""])[0].strip()
            name = query.get("name", [""])[0].strip()
            length = int(handler.headers.get("Content-Length", 0))
            body = handler.rfile.read(length).decode("utf-8") if length else ""
            value = parse_qs(body).get("value", [""])[0]
            if session_id and name:
                _apply_setting(session_id, name, value)
            handler._redirect("/sessions")
            return

        length = int(handler.headers.get("Content-Length", 0))
        raw = handler.rfile.read(length) if length else b""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            handler._write(400, "application/json; charset=utf-8", b'{"error": "invalid JSON"}')
            return
        if not isinstance(data, dict):
            handler._write(
                400, "application/json; charset=utf-8", b'{"error": "JSON body must be an object"}'
            )
            return
        session_id = str(data.get("session") or "").strip()
        if not session_id:
            handler._write(
                400,
                "application/json; charset=utf-8",
                b'{"error": "missing \'session\' field"}',
            )
            return

        if "values" in data or "clear" in data:
            values = data.get("values") or {}
            clear = data.get("clear") or []
            if not isinstance(values, dict) or not isinstance(clear, (list, tuple)):
                handler._write(
                    400,
                    "application/json; charset=utf-8",
                    b"{\"error\": \"'values' must be an object, 'clear' a list\"}",
                )
                return
            result = session_settings.set_config(session_id, values, clear)
        else:
            name = str(data.get("name") or "").strip()
            if not name:
                handler._write(
                    400,
                    "application/json; charset=utf-8",
                    b'{"error": "missing \'name\' field"}',
                )
                return
            if not _apply_setting(session_id, name, data.get("value")):
                body = json.dumps({"error": f"invalid name/value: {name!r}"}).encode()
                handler._write(400, "application/json; charset=utf-8", body)
                return
            result = session_settings.session_overrides(session_id)

        body = json.dumps({"ok": True, "session": session_id, "overrides": result or {}}).encode(
            "utf-8"
        )
        handler._write(200, "application/json; charset=utf-8", body)


@webserver.register
class SessionLogFileEndpoint(webserver.Endpoint):
    """GET /logs/<имя> — раздача .err-файла сессии (v0.9.5, задача 7).

    Ссылка-счётчик «Ошибок» в таблице Sessions открывает файл инцидентов
    сессии в НОВОМ окне браузера (text/plain — браузер показывает его
    построчно). Файлы лежат в корне WEBUI (WebContext.root_dir =
    ADAPTER_DEBUG_LOGPATH). Имя проверяется строгим шаблоном _ERR_NAME_RE —
    в допустимом имени нет ни слэшей, ни «..», поэтому обход каталога
    невозможен структурно (отдельная realpath-проверка не нужна)."""

    prefix = "/logs"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        name = remainder
        if not name or not _ERR_NAME_RE.match(name):
            handler.send_error(404, "Not found")
            return
        path = os.path.join(handler.context.root_dir, name)
        if not os.path.isfile(path):
            handler.send_error(404, "File not found")
            return
        try:
            with open(path, "rb") as f:
                body = f.read()
        except OSError:
            handler.send_error(404, "File not found")
            return
        handler._write(200, handler._content_type_for(path), body)


__all__ = [
    "_ERR_NAME_RE",
    "_bool_options",
    "_target_options",
    "_stored_bool",
    "_select_form_html",
    "_input_cell_html",
    "_errors_cell_html",
    "_target_cell_html",
    "_actions_cell_html",
    "_sessions_rows_html",
    "_render_sessions_page",
    "sessions_poll_script",
    "_apply_setting",
    "SessionsPageEndpoint",
    "SessionsSnapshotEndpoint",
    "SessionResetEndpoint",
    "SessionDeleteEndpoint",
    "SessionSettingsEndpoint",
    "SessionLogFileEndpoint",
    "_parse_session_key",
]
