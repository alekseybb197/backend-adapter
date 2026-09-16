#!/usr/bin/env python3
"""
webui_errors.py — эндпойнт "/errors" общего веб-сервера WEBUI:
превью .err-файла сессии (список секций) и сырой вид ОДНОЙ секции.

ЗАЧЕМ, ЕСЛИ .err УЖЕ РАЗДАЁТСЯ: GET /logs/<имя> отдаёт файл ЦЕЛИКОМ как
text/plain. Файл — поток самоделимитированных блоков (инцидент бэкенда,
ошибка уровня адаптера, WARN-событие — см. session_log), и строка [REQUEST]
несёт ПОЛНОЕ тело запроса: при сплошном просмотре такие строки враппятся на
десятки экранов, и найти нужный инцидент глазами тяжело. Страница
/errors/<имя> даёт два вида одного файла:

  - таблица секций (HTML): по строке на блок, длинные строки обрезаются
    многоточием (CSS text-overflow: ellipsis), а не переносятся — файл
    пролистывается, инцидент находится;
  - ?section=N — ТОЛЬКО эта секция, дословно (text/plain): ссылка «№»
    открывает её в НОВОМ окне браузера, рядом с превью.

/logs при этом не трогается: там по-прежнему «весь файл целиком» (ссылка с
превью-страницы), и поведение раздачи файла остаётся ровно тем же. Имя
валидируется тем же строгим шаблоном webui_sessions._ERR_NAME_RE — в
допустимом имени нет ни слэшей, ни «..», поэтому обход каталога исключён
структурно (отдельная realpath-проверка не нужна, как и в /logs).

Разбор файла — ЧИСТАЯ функция parse_err_sections над текстом (тестируется
без HTTP); эндпойнт лишь читает файл, зовёт её и рендерит результат.

Модуль — чистый эндпойнт: CLI/сервер/обработчик живут в ядре webserver.py.
"""

import html
import logging
import os
import re
from urllib.parse import parse_qs, quote, urlparse

from . import webserver
from .webui_sessions import _ERR_NAME_RE

logger = logging.getLogger("webui_errors")

# Делимитеры блоков — РОВНО те строки, что пишет session_log
# (write_error_file / write_session_error / write_warn_file). Сравнение идёт
# по strip()-строке: файл формируется самим адаптером, но лишние пробелы по
# краям (правка вручную, чужой редактор) не должны ломать разбор.
_ERR_OPEN = {
    "ERROR": "==================== ERROR ====================",
    "WARNING": "==================== WARNING ====================",
}
_ERR_CLOSE = {
    "ERROR": "==================== END ERROR ====================",
    "WARNING": "==================== END WARNING ====================",
}
_KIND_BY_OPEN = {line: kind for kind, line in _ERR_OPEN.items()}
_KIND_BY_CLOSE = {line: kind for kind, line in _ERR_CLOSE.items()}

# Шапка секции: "[<ts>] [<req_id>] <поля>". У write_error_file/write_warn_file
# поля session_id/final_status/model/backend_url, у write_session_error нет
# backend_url, у WARNING нет final_status — отсутствующие поля становятся "".
_HEADER_TS_RE = re.compile(r"^\[([^\]]*)\]\s*\[([^\]]*)\]")
_FIELD_RE = re.compile(r"(\w+)=(\S*)")
_FIELDS = ("session_id", "final_status", "model", "backend_url")

# Строка тела: "[<ts>] [<req_id>] [<TAG>] <текст>". Тело запроса/ошибки может
# быть МНОГОСТРОЧНЫМ (_err_body_text не срезает переводы строк), поэтому
# строки без тега — продолжение предыдущей, а не самостоятельные записи.
_BODY_RE = re.compile(r"^\[([^\]]*)\]\s*\[([^\]]*)\]\s*\[([^\]]+)\]\s?(.*)$")

# Потолок чтения превью: .err-файл принципиально не обрезается при записи
# (полные тела запросов), поэтому у долгоживущей сессии он растёт неограниченно.
_MAX_PREVIEW_BYTES = 4 * 1024 * 1024


# ==================== ЧИСТАЯ ЛОГИКА ====================


def _finalize(sec: dict) -> dict:
    """Собрать секцию из накопленных строк в готовую запись.

    ``sec`` — внутреннее состояние машины состояний (kind/header/body/raw).
    Наружу отдаётся словарь с полями: ``n`` (номер, проставляется позже),
    ``kind``, ``closed``, ``kind_mismatch``, ``header``, ``ts``, ``req_id``,
    ``fields``, ``body_lines``, ``summary``, ``raw``."""
    header = sec["header"]
    ts = req_id = ""
    m = _HEADER_TS_RE.match(header)
    if m:
        ts, req_id = m.group(1), m.group(2)
    found = dict(_FIELD_RE.findall(header))

    body_lines: list[list[str]] = []
    for line in sec["body"]:
        bm = _BODY_RE.match(line)
        if bm:
            body_lines.append([bm.group(3), bm.group(4)])
        elif body_lines:
            # Продолжение многострочного тела: приклеиваем к предыдущей записи,
            # чтобы raw-вид и summary не теряли содержимое.
            body_lines[-1][1] += "\n" + line

    # «Строка» превью — ПОСЛЕДНЯЯ запись тела, а не первая: первая у инцидента
    # почти всегда [REQUEST] с полным телом запроса (у всех секций одинаковый
    # шум), а диагноз несёт именно последняя — [BACKEND_ERROR]/[ADAPTER_ERROR]/
    # [WARN]. Для превью берём её первую физическую строку (остальное — в raw).
    summary = ""
    if body_lines:
        tag, text = body_lines[-1]
        summary = f"[{tag}] {text.splitlines()[0] if text else ''}".rstrip()

    return {
        "n": 0,
        "kind": sec["kind"],
        "closed": sec["closed"],
        "kind_mismatch": sec["kind_mismatch"],
        "header": header,
        "ts": ts,
        "req_id": req_id,
        "fields": {name: found.get(name, "") for name in _FIELDS},
        "body_lines": [(tag, text) for tag, text in body_lines],
        "summary": summary,
        "raw": "\n".join(sec["raw"]) + "\n",
    }


def parse_err_sections(text: str) -> list[dict]:
    """Разобрать текст .err-файла на секции (блоки между делимитерами).

    Машина состояний: открывающий делимитер начинает секцию, ЛЮБОЙ
    закрывающий её закрывает (несовпадение вида — ``kind_mismatch=True``),
    конец файла без закрывающего — ``closed=False``. Текст до первого
    открывающего — секция ``PREAMBLE``, и только если в ней есть хоть одна
    непустая строка (иначе это просто пустой файл/хвост).

    Номера ``n`` — 1-based в порядке файла: они же — номера строк превью и
    значения ``?section=N``. Секция-преамбула участвует в нумерации наравне
    с остальными (она первая, если есть)."""
    sections: list[dict] = []
    current: dict | None = None
    preamble: list[str] = []

    def _close(sec: dict, closing: str | None) -> None:
        if closing is not None:
            sec["raw"].append(closing)
            sec["closed"] = True
            if _KIND_BY_CLOSE[closing.strip()] != sec["kind"]:
                sec["kind_mismatch"] = True
        sections.append(_finalize(sec))

    for line in text.splitlines():
        stripped = line.strip()
        if stripped in _KIND_BY_OPEN:
            if current is not None:
                # Незакрытая секция: следующий открывающий делимитер начинает
                # новую (обрыв записи — файл дописывается построчно).
                _close(current, None)
            current = {
                "kind": _KIND_BY_OPEN[stripped],
                "closed": False,
                "kind_mismatch": False,
                "header": "",
                "body": [],
                "raw": [line],
            }
            continue
        if stripped in _KIND_BY_CLOSE:
            if current is not None:
                _close(current, line)
                current = None
            else:
                # Закрывающий делимитер без открывающего — мусор, но не повод
                # терять строку: уходит в преамбулу (и в превью, и в raw).
                preamble.append(line)
            continue
        if current is None:
            preamble.append(line)
            continue
        current["raw"].append(line)
        if not current["header"]:
            # Шапка — первая непустая строка секции; пустые до неё только
            # копятся в raw (в тело не попадают).
            if stripped:
                current["header"] = line
        else:
            current["body"].append(line)
    if current is not None:
        _close(current, None)

    if any(line.strip() for line in preamble):
        sections.insert(
            0,
            _finalize(
                {
                    "kind": "PREAMBLE",
                    "closed": True,  # закрывать нечего — бейджа у преамбулы нет
                    "kind_mismatch": False,
                    "header": next((ln for ln in preamble if ln.strip()), ""),
                    "body": [],
                    "raw": preamble,
                }
            ),
        )
    for i, sec in enumerate(sections):
        sec["n"] = i + 1
    return sections


# ==================== РЕНДЕР ПРЕВЬЮ ====================

_PAGE_CSS = """  body { font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; color: #222; }
  table { border-collapse: collapse; margin-top: 12px; }
  td, th { border: 1px solid #ddd; padding: 6px 10px; text-align: left; vertical-align: top; }
  th { background: #f5f5f5; }
  code { font-size: 13px; }
  td.line { max-width: 760px; }
  td.line span { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  tr.sec { cursor: pointer; }
  tr.sec:hover { background: #fafafa; }
"""

_BADGE_STYLE = "color:#c0392b;font-size:12px"


def _cell(value: str, *, code: bool = False, title: str = "") -> str:
    """HTML ячейки: пустое значение — серая «—», иначе текст (можно <code>)."""
    if not value:
        return '<td><span style="color:#aaa">—</span></td>'
    escaped = html.escape(value)
    body = f"<code>{escaped}</code>" if code else escaped
    attr = f' title="{html.escape(title, quote=True)}"' if title else ""
    return f"<td{attr}>{body}</td>"


def _section_row_html(name: str, sec: dict) -> str:
    """HTML строки превью для одной секции.

    Ссылка «№» — настоящий <a> (работает без JS) и открывается в НОВОМ окне;
    вся остальная строка кликабельна через инлайновый onclick (инлайновый JS
    в WEBUI — уже норма, ср. sessions_poll_script). stopPropagation на ссылке
    обязателен: иначе клик по «№» сначала увёл бы ТЕКУЩЕЕ окно по data-href."""
    n = sec["n"]
    href = f"/errors/{quote(name, safe='')}?section={n}"
    badge = ""
    if sec["kind_mismatch"]:
        badge = f' <span style="{_BADGE_STYLE}">(END не совпал с видом)</span>'
    elif not sec["closed"]:
        badge = f' <span style="{_BADGE_STYLE}">(не закрыта)</span>'
    session = sec["fields"]["session_id"]
    return (
        f'<tr class="sec" data-href="{html.escape(href, quote=True)}" '
        f"onclick=\"location.href=this.getAttribute('data-href')\">"
        f'<td><a href="{html.escape(href, quote=True)}" target="_blank" rel="noopener" '
        f'onclick="event.stopPropagation()" '
        f'title="Открыть секцию №{n} в сыром виде (новое окно)">{n}</a></td>'
        f"<td>{html.escape(sec['kind'])}{badge}</td>"
        f"{_cell(sec['ts'], code=True)}"
        f"{_cell(sec['req_id'], code=True)}"
        f"{_cell(session[:8], code=True, title=session)}"
        f"{_cell(sec['fields']['final_status'])}"
        f"{_cell(sec['fields']['model'])}"
        f"{_cell(sec['fields']['backend_url'])}"
        f'<td class="line"><span title="{html.escape(sec["summary"], quote=True)}">'
        f"{html.escape(sec['summary'])}</span></td>"
        "</tr>"
    )


def _render_preview(name: str, sections: list[dict], truncated: bool, version: str) -> bytes:
    """HTML страницы /errors/<имя>: таблица секций .err-файла.

    Шапка и базовый CSS — те же, что у /sessions (единый облик страниц WEBUI);
    дополнительно — обрезка длинных строк превью по ellipsis."""
    quoted = quote(name, safe="")
    rows = "".join(_section_row_html(name, sec) for sec in sections)
    if not rows:
        rows = '<tr><td colspan="9" style="color:#888">файл ошибок пуст — секций нет</td></tr>'
    truncated_note = ""
    if truncated:
        truncated_note = (
            '<p style="color:#c0392b">Файл обрезан: показаны первые 4 МиБ. '
            f'Полная версия — <a href="/logs/{quoted}">весь файл (raw)</a>.</p>'
        )
    page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>backend-adapter — ошибки {html.escape(name)}</title>
<style>
{_PAGE_CSS}</style>
</head>
<body>
<h2>Backend-Adapter Version {html.escape(version)} — ошибки сессии</h2>
<p><a href="/sessions">← Сессии</a> &nbsp;·&nbsp; <a href="/">Статус 📊</a></p>
<p style="color:#666;font-size:13px;max-width:1100px">
  Файл <code>{html.escape(name)}</code> разобран на секции: инциденты
  бэкенда и ошибки адаптера (<b>ERROR</b>), диагностические предупреждения
  (<b>WARNING</b>). Длинные строки в колонке «Строка» обрезаны — целиком
  секция открывается ссылкой <b>№</b> в новом окне, а весь файл — ссылкой
  «весь файл (raw)» внизу.
</p>
{truncated_note}
<table>
  <tr><th>№</th><th>Тип</th><th>Время</th><th>req_id</th><th>Сессия</th><th>Статус</th><th>Модель</th><th>Бэкенд</th><th>Строка</th></tr>
  {rows}
</table>
<p><a href="/logs/{quoted}">весь файл (raw)</a> &nbsp;·&nbsp; <a href="/sessions">← Сессии</a></p>
</body>
</html>
"""
    return page.encode("utf-8")


# ==================== ЭНДПОЙНТ ====================


@webserver.register
class ErrorsEndpoint(webserver.Endpoint):
    """GET /errors/<имя>[?section=N] — превью .err-файла и сырая секция.

    Без ``section`` — HTML-таблица секций (превью с обрезанными строками);
    с ``section=N`` — ДОСЛОВНЫЙ текст одной секции как text/plain (её
    открывает ссылка «№» в новом окне). Имя проверяется строгим шаблоном
    _ERR_NAME_RE (общий с /logs): в допустимом имени нет ни слэшей, ни «..»,
    поэтому обход каталога невозможен структурно. Нет файла, имя не по
    шаблону, ``section`` не число / вне диапазона, ошибка чтения — 404."""

    prefix = "/errors"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        name = remainder
        if not name or not _ERR_NAME_RE.match(name):
            handler.send_error(404, "Not found")
            return
        path = os.path.join(handler.context.log_dir, name)
        if not os.path.isfile(path):
            handler.send_error(404, "File not found")
            return
        try:
            with open(path, "rb") as f:
                # Читаем на байт больше потолка — так видно, обрезан ли файл
                # (без отдельного stat и без гонки между ними).
                raw_bytes = f.read(_MAX_PREVIEW_BYTES + 1)
        except OSError:
            handler.send_error(404, "File not found")
            return
        truncated = len(raw_bytes) > _MAX_PREVIEW_BYTES
        text = raw_bytes[:_MAX_PREVIEW_BYTES].decode("utf-8", errors="replace")
        sections = parse_err_sections(text)

        raw_section = parse_qs(urlparse(handler.path).query).get("section", [""])[0]
        if raw_section:
            try:
                number = int(raw_section)
            except ValueError:
                handler.send_error(404, "Not found")
                return
            if not 1 <= number <= len(sections):
                handler.send_error(404, "Not found")
                return
            body = sections[number - 1]["raw"].encode("utf-8")
            handler._write(200, "text/plain; charset=utf-8", body)
            return
        handler._write(
            200,
            "text/html; charset=utf-8",
            _render_preview(name, sections, truncated, self.context.version),
        )


__all__ = [
    "_ERR_OPEN",
    "_ERR_CLOSE",
    "_MAX_PREVIEW_BYTES",
    "parse_err_sections",
    "_render_preview",
    "_section_row_html",
    "ErrorsEndpoint",
]
