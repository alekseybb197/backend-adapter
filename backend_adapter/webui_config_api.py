#!/usr/bin/env python3
"""
webui_config_api.py — эндпойнт "/config" общего веб-сервера WEBUI.

Контракт: runtime-переключение debug-записи и поведения НОВЫХ запросов без
перезапуска адаптера. Пул переменных (RUNTIME_CONFIG_POOL в config.py) —
объём записи на диск (логи/трейсы/*.parts дампы), маскировка секретов
(санитайзер), рубильники стриминга, строгая валидация моделей и TARGET-
маршрутизация входных эндпоинтов (выбор целевого формата входа). Сеть/бэкенды/
модели/порты/точка хранения не входят — их смена на лету требует пересоздания
слушателей/переинициализации и сорвала бы активные соединения.

Эндпойнт:
  GET /config → HTML-форма с текущими значениями пула (12 полей: 6 bool
                checkbox + 3 int input + 3 select для TARGET-переменных
                маршрутизации входов)
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
    # Разбиваем поля по типам для правильного рендера: bool-чекбоксы,
    # int-инпуты и enum-поля (строки с фиксированным доменом значений —
    # TARGET-переменные маршрутизации входов, рендерятся выпадающим списком).
    bool_fields = [
        "ADAPTER_DEBUG",
        "ADAPTER_DEBUG_PARTS",
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
    # enum-поля с допустимыми значениями: {имя: (домен, «порядок в форме» не
    # нужен)}. Домен — config.TARGET_ALLOWED_VALUES (для трёх входов общий:
    # значение сообщает и имя входа — label-строкой).
    enum_fields = {
        "ADAPTER_MESSAGES_TARGET": config.TARGET_ALLOWED_VALUES,
        "ADAPTER_COMPLETIONS_TARGET": config.TARGET_ALLOWED_VALUES,
        "ADAPTER_RESPONSES_TARGET": config.TARGET_ALLOWED_VALUES,
    }

    # Описания полей для подсказок
    field_descriptions = {
        "ADAPTER_DEBUG": "Файловая запись логов (полные, без обрезки); консоль — всегда, с обрезкой TRIM",
        "ADAPTER_DEBUG_PARTS": "Per-session дампы частей протокола — .json+.yaml по каждому тегу",
        "ADAPTER_SENSITIVE_LOGGING_ENABLE": "Отключить санитайзер логов (секреты в открытом виде!)",
        "ADAPTER_STREAMING_ENABLE": "Рубильник стриминга: 0 — всегда stream=False (аварийный)",
        "ADAPTER_STREAM_INCLUDE_USAGE": "Передавать usage-токены в стриме (stream_options)",
        "ADAPTER_STRICT_MODELS": "Строгая валидация моделей по списку бэкенда",
        "ADAPTER_DEBUG_TRIM": "Порог обрезки консольного вывода (символы, 0=без обрезки; файл — всегда полный)",
        "ADAPTER_TRACE_REASONING_MAX_CHARS": "Макс. символов reasoning в трейсе (0=без ограничений)",
        "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS": "Макс. символов tool-полей в трейсе (0=без ограничений)",
        # TARGET-маршрутизация входов (см. docs/routing.md): значение задаёт,
        # что делать с запросом на входе. Цель = сам вход → passthrough E→E;
        # auto — по кэшу проб, без сети; none — вход закрыт (404).
        "ADAPTER_MESSAGES_TARGET": "Куда направлять /v1/messages (Anthropic): формат-цель (messages → passthrough E→E), auto, none — вход закрыт (404)",
        "ADAPTER_COMPLETIONS_TARGET": "Куда направлять /v1/chat/completions ([OI]): completions → passthrough E→E, auto, none — вход закрыт (404)",
        "ADAPTER_RESPONSES_TARGET": "Куда направлять /v1/responses: responses → passthrough E→E, auto, none — вход закрыт (404)",
    }

    rows = []
    for name in bool_fields:
        value = current_values.get(name, False)
        desc = field_descriptions.get(name, "")
        checked = "checked" if value else ""
        hidden_value = "1" if value else "0"
        # Для каждого bool-поля — ПАРА полей: checkbox (шлёт value=1, только
        # когда отмечен) + hidden-поле текущего состояния. Hidden-поле шлёт
        # 1/0 ВСЕГДА под именем "_<NAME>" (префикс "_" — чтобы разбор POST
        # ниже внёс в data именно "<NAME>": сначала явное значение checkbox-а,
        # затем всегда-присутствующее hidden-состояние). Идиома нужна потому,
        # что снятая галка просто отсутствует в теле POST — без hidden-
        # «соседа» выключить bool через форму было бы невозможно.
        rows.append(f"""
      <tr>
        <td><label for="{name}">{html.escape(name)}</label></td>
        <td><input type="checkbox" id="{name}" name="{name}" value="1" {checked}>
            <input type="hidden" name="_{name}" value="{hidden_value}">
        </td>
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

    for name, allowed in enum_fields.items():
        value = current_values.get(name, "none")
        desc = field_descriptions.get(name, "")
        # Выпадающий список допустимых значений; текущее — selected. select
        # шлёт одно значение (строку) — в отличие от bool-пары checkbox+hidden,
        # отдельный «сосед» не нужен: каждое значение выбирается явно.
        options = "".join(
            f'<option value="{opt}"{" selected" if opt == value else ""}>{opt}</option>'
            for opt in allowed
        )
        rows.append(f"""
      <tr>
        <td><label for="{name}">{html.escape(name)}</label></td>
        <td><select id="{name}" name="{name}" style="min-width: 160px">{options}</select></td>
        <td style="color:#666; font-size: 13px">{html.escape(desc)}</td>
        <td style="color:#999; font-size: 12px">текущее: {html.escape(str(value))}</td>
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
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
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
<h2>Backend-Adapter — runtime config</h2>
<p><a href="/">Статус 📊</a> &nbsp;·&nbsp; <a href="/session">Обзор сессий 📋</a></p>
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
            # Каждое bool-поле формы шлёт ПАРУ значений: явный checkbox
            # (value=1, только когда отмечен) и всегда-присутствующее
            # hidden-поле "_<NAME>" с текущим состоянием (1/0). Разбор ниже:
            # значения каждого ключа складываются в список, из которого мы
            # берём ПОСЛЕДНЕЕ — hidden-состояние; ключ "_NAME" затем пишется
            # в data как "NAME". Если галка отмечена — checkbox внёс "NAME"→True
            # (состояние «включено»), если снята — остаётся только hidden
            # "_NAME"→0 → False. int-поля всегда присутствуют (type="number").
            parsed = parse_qs(body, keep_blank_values=True)
            for key, values in parsed.items():
                if not values:
                    continue
                # Последнее значение: для bool — это hidden-«сосед» (при
                # отмеченной галке checkbox "NAME"=1 идёт ПЕРВЫМ, затем
                # hidden "_NAME"=1 — оба дают True, не конфликтуют).
                val = values[-1]
                # hidden-ключ "_NAME" → имя "NAME"
                data_key = key[1:] if key.startswith("_") else key
                # Преобразуем типы: "1"/"true"/"on" → True, "0"/"false" → False
                if val.lower() in ("1", "true", "on", "yes"):
                    data[data_key] = True
                elif val.lower() in ("0", "false", "off", "no"):
                    data[data_key] = False
                else:
                    # Пробуем int
                    try:
                        data[data_key] = int(val)
                    except ValueError:
                        data[data_key] = val

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
                # enum-поле (TARGET): value — строка из допустимого набора.
                if (
                    isinstance(expected_type, tuple)
                    and expected_type[0] == "enum"
                    and isinstance(value, str)
                    and value in expected_type[1]
                ):
                    type_ok = True
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
