#!/usr/bin/env python3
"""
webui_config_api.py — эндпойнт "/config" общего веб-сервера WEBUI.

Контракт: runtime-переключение debug-записи и поведения НОВЫХ запросов без
перезапуска адаптера. Пул переменных (RUNTIME_CONFIG_POOL в config.py) —
объём записи на диск (логи/трейсы/*.parts дампы), маскировка секретов
(санитайзер), рубильники стриминга и строгая валидация моделей. Сеть/бэкенды/
модели/порты/точка хранения не входят — их смена на лету требует пересоздания
слушателей/переинициализации и сорвала бы активные соединения.

Эндпойнт:
  GET /config → HTML-форма с текущими значениями пула (12 полей: 8 bool
                checkbox + 3 int input + 1 text для списка тегов без обрезки)
  POST /config → application/x-www-form-urlencoded или JSON, применяет валидные
                 значения через config.set_runtime_config(**...), сверяет ответ
                 с посланным, редирект на GET с flash-сообщением об успехе
"""

import html
import json
import logging
from urllib.parse import parse_qs

from . import config, webserver

logger = logging.getLogger("webui_config_api")


# ==================== ЧИСТАЯ ЛОГИКА ====================


def _render_config_page(current_values: dict, applied: dict | None = None) -> bytes:
    """HTML-форма runtime-конфига.

    current_values — dict из config.get_runtime_config(): {имя: значение}
    applied — что применилось при последнем POST (для flash-сообщения):
              {"ok": [...], "ignored": [...]}
    """
    # Разбиваем поля по типам для правильного рендера
    bool_fields = [
        "ADAPTER_DEBUG",
        "ADAPTER_DEBUG_TAGS_OUT",
        "ADAPTER_DEBUG_TOOLS",
        "ADAPTER_DEBUG_TOOLS_ERROR",
        "ADAPTER_SENSITIVE_LOGGING_ENABLE",
        "ADAPTER_STREAMING_ENABLE",
        "ADAPTER_STREAM_INCLUDE_USAGE",
        "ADAPTER_STRICT_MODELS",
    ]
    int_fields = [
        "ADAPTER_DEBUG_TRIM",
        "ADAPTER_TRACE_REASONING_MAX_CHARS",
        "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS",
    ]
    # Единственная строковая переменная пула — список тегов без обрезки
    # (строка env-формата "TAG1,TAG2"; рабочий список живёт в config._SET).
    str_field = "ADAPTER_DEBUG_TAGS_FULL"

    # Описания полей для подсказок
    field_descriptions = {
        "ADAPTER_DEBUG": "Мастер-выключатель debug-логов (0/1)",
        "ADAPTER_DEBUG_TAGS_OUT": "Per-session дампы протокола (.json+.yaml парой)",
        "ADAPTER_DEBUG_TOOLS": "Логировать все результаты инструментов ([TOOL_RESULT])",
        "ADAPTER_DEBUG_TOOLS_ERROR": "Логировать ошибки инструментов ([TOOL_RESULT_ERROR])",
        "ADAPTER_SENSITIVE_LOGGING_ENABLE": "Отключить санитайзер логов (секреты в открытом виде!)",
        "ADAPTER_STREAMING_ENABLE": "Рубильник стриминга: 0 — всегда stream=False (аварийный)",
        "ADAPTER_STREAM_INCLUDE_USAGE": "Передавать usage-токены в стриме (stream_options)",
        "ADAPTER_STRICT_MODELS": "Строгая валидация моделей по списку бэкенда",
        "ADAPTER_DEBUG_TRIM": "Порог обрезки логов (символы, 0=выкл.)",
        "ADAPTER_TRACE_REASONING_MAX_CHARS": "Макс. символов reasoning в трейсе (0=без ограничений)",
        "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS": "Макс. символов tool-полей в трейсе (0=без ограничений)",
        "ADAPTER_DEBUG_TAGS_FULL": "Теги без обрезки (через запятую; пусто — trim везде)",
    }

    rows = []
    for name in bool_fields:
        value = current_values.get(name, False)
        desc = field_descriptions.get(name, "")
        checked = "checked" if value else ""
        rows.append(f"""
      <tr>
        <td><label for="{name}">{html.escape(name)}</label></td>
        <td><input type="checkbox" id="{name}" name="{name}" value="1" {checked}></td>
        <td style="color:#666; font-size: 13px">{html.escape(desc)}</td>
        <td style="color:#999; font-size: 12px">текущее: {value}</td>
      </tr>""")

    for name in int_fields:
        value = current_values.get(name, 0)
        desc = field_descriptions.get(name, "")
        rows.append(f"""
      <tr>
        <td><label for="{name}">{html.escape(name)}</label></td>
        <td><input type="number" id="{name}" name="{name}" value="{value}" min="0" style="width: 120px"></td>
        <td style="color:#666; font-size: 13px">{html.escape(desc)}</td>
        <td style="color:#999; font-size: 12px">текущее: {value}</td>
      </tr>""")

    # Строковое поле: text input с текущим env-формат-значением пула.
    str_value = config._ADAPTER_DEBUG_TAGS_FULL_RAW
    str_desc = field_descriptions.get(str_field, "")
    rows.append(f"""
      <tr>
        <td><label for="{str_field}">{html.escape(str_field)}</label></td>
        <td><input type="text" id="{str_field}" name="{str_field}" value="{html.escape(str_value)}" style="width: 220px" placeholder="BODY,TOOL_RESULT,RESPONSE"></td>
        <td style="color:#666; font-size: 13px">{html.escape(str_desc)}</td>
        <td style="color:#999; font-size: 12px">текущее: {html.escape(str_value or "—")}</td>
      </tr>""")

    # Flash-сообщение о применённых изменениях
    flash_html = ""
    if applied:
        ok_list = applied.get("ok", [])
        ignored_list = applied.get("ignored", [])
        if ok_list:
            flash_html = (
                f'<p style="color:#1a7f37">Применено: {html.escape(", ".join(ok_list))}</p>'
            )
        if ignored_list:
            flash_html += f'<p style="color:#b8860b">Игнорировано (неверный тип/неизвестный ключ): {html.escape(", ".join(ignored_list))}</p>'

    html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>backend-adapter — runtime config</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; color: #222; }}
  table {{ border-collapse: collapse; margin-top: 12px; width: 100%; }}
  td, th {{ border: 1px solid #ddd; padding: 6px 12px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; }}
  label {{ font-weight: 500; }}
  input[type="number"] {{ padding: 4px; }}
</style>
</head>
<body>
<h2>[CC]-adapter — runtime config</h2>
<p><a href="/">← статус</a> &nbsp;·&nbsp; <a href="/session">просмотр сессий →</a></p>
{flash_html}
<form method="POST" action="/config">
<table>
  <tr><th>Переменная</th><th>Значение</th><th>Описание</th><th>Текущее</th></tr>
  {"".join(rows)}
</table>
<div style="margin-top: 16px">
  <button type="submit">Применить</button>
</div>
</form>
<p style="color:#888; margin-top: 16px; font-size: 13px">
  Изменения применяются немедленно и не требуют перезапуска адаптера.
  Неизвестные ключи и значения неверного типа игнорируются.
</p>
</body>
</html>
"""
    return html_page.encode("utf-8")


# ==================== ЭНДПОЙНТ ====================


@webserver.register
class ConfigEndpoint(webserver.Endpoint):
    """Эндпойнт "/config": runtime-переключение debug-записи и рубильников.

    GET → HTML-форма текущих значений RUNTIME_CONFIG_POOL
    POST → применение валидных значений, редирект на GET с сообщением
    """

    prefix = "/config"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        if remainder:
            handler.send_error(404, "Not found")
            return
        current = config.get_runtime_config()
        handler._write(200, "text/html; charset=utf-8", _render_config_page(current))

    def POST(self, handler, remainder: str):
        if remainder:
            handler.send_error(404, "Not found")
            return

        # Парсим тело запроса
        content_type = handler.headers.get("Content-Type", "")
        data = {}

        if "application/json" in content_type:
            # JSON
            length = int(handler.headers.get("Content-Length", 0))
            body = handler.rfile.read(length)
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                handler.send_error(400, "Invalid JSON")
                return
        else:
            # form-data (application/x-www-form-urlencoded)
            length = int(handler.headers.get("Content-Length", 0))
            body = handler.rfile.read(length).decode("utf-8")
            # keep_blank_values=True: пустое значение text-поля (например,
            # ADAPTER_DEBUG_TAGS_FULL= — сброс списка тегов) должно ДОЙТИ
            # как "", а не исчезнуть (иначе очистить поле формой нельзя).
            parsed = parse_qs(body, keep_blank_values=True)
            # parse_qs возвращает списки значений; берём первое
            for key, values in parsed.items():
                if values:
                    val = values[0]
                    # Преобразуем типы: "1"/"true"/"on" → True, "0"/"false" → False
                    if val.lower() in ("1", "true", "on", "yes"):
                        data[key] = True
                    elif val.lower() in ("0", "false", "off", "no"):
                        data[key] = False
                    else:
                        # Пробуем int
                        try:
                            data[key] = int(val)
                        except ValueError:
                            data[key] = val

        # Применяем через set_runtime_config
        result = config.set_runtime_config(**data)

        # Сверяем, что применилось
        applied_ok = []
        applied_ignored = []
        for key, value in data.items():
            if key in config.RUNTIME_CONFIG_POOL:
                expected_type = config._RUNTIME_CONFIG_TYPES.get(key)
                # bool — подкласс int: для int-поля проверяем строго.
                type_ok = (expected_type is bool and isinstance(value, bool)) or (
                    expected_type is int and isinstance(value, int) and not isinstance(value, bool)
                )
                if type_ok and result.get(key) == value:
                    applied_ok.append(key)
                else:
                    applied_ignored.append(key)
            else:
                applied_ignored.append(key)

        # Редирект на GET с flash-сообщением
        # (через HTTP 303 See Other + Location)
        applied_data = {"ok": applied_ok, "ignored": applied_ignored}
        html_content = _render_config_page(result, applied=applied_data)
        handler._write(200, "text/html; charset=utf-8", html_content)


__all__ = [
    "_render_config_page",
    "ConfigEndpoint",
]
