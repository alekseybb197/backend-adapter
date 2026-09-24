#!/usr/bin/env python3
"""
webui_config_api.py — эндпойнт "/config" общего веб-сервера WEBUI.

Контракт: runtime-переключение debug-записи и поведения НОВЫХ запросов без
перезапуска адаптера. Пул переменных (RUNTIME_CONFIG_POOL в config.py) —
объём записи на диск (логи/трейсы/*.parts дампы), маскировка секретов
(санитайзер), рубильники стриминга, строгая валидация моделей, TARGET-
маршрутизация входных эндпоинтов (выбор целевого формата входа) и строка
маппинга моделей agent→backend. Сеть/бэкенды/порты/точка хранения не входят —
их смена на лету требует пересоздания слушателей/переинициализации и сорвала
бы активные соединения.

Особый случай — Log (v0.9.8): это не выключатель функционала, а ШАБЛОН
для новых сессий. Сессия получает его значение в момент образования (снимок
общего флага, см. session_settings.ensure_session) и дальше живёт своим;
переключение здесь уже работающих сессий не трогает — их Log
управляется на странице /sessions. (v0.9.10: второй флаг — Parts — снят;
части протокола ``*.parts`` собираются вместе с логами по тому же флагу.)

Эндпойнт:
  GET /config → HTML-форма с текущими значениями пула (12 полей: 5 bool
                checkbox + 3 int input + 1 text (маппинг моделей) + 3 select
                для TARGET-переменных маршрутизации входов)
  POST /config → application/x-www-form-urlencoded или JSON, применяет валидные
                 значения через config.set_runtime_config(**...); страница
                 сообщает ТОЛЬКО об ошибках (игнорированные ключи/неверный тип),
                 успешное применение ничего не печатает
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
    applied — ошибки последнего POST (для flash-сообщения): {"ignored": [...]}
              (успешное применение не сообщается — плашка «Применено» убрана)
    """
    # Разбиваем поля по типам для правильного рендера: bool-чекбоксы,
    # int-инпуты и enum-поля (строки с фиксированным доменом значений —
    # TARGET-переменные маршрутизации входов, рендерятся выпадающим списком).
    bool_fields = [
        "ADAPTER_DEBUG",
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
    # Строковые поля свободного формата (без фиксированного домена): сейчас
    # одно — строка маппинга моделей (формат env ``agent:backend,…``).
    str_fields = [
        "ADAPTER_MODELS_MAPPING",
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
        # v0.9.8: глобальный Log — не выключатель функционала, а шаблон
        # для НОВЫХ сессий: существующие держат значение, взятое при своём
        # образовании (страница /sessions). Формулировка это подчёркивает.
        # v0.9.10: флаг один — логи, трейсы и дампы частей (*.parts) вместе.
        "ADAPTER_DEBUG": "Файловая запись логов, трейсов и дампов частей (*.parts) для НОВЫХ сессий (полные, без обрезки); консоль — всегда, с обрезкой TRIM",
        "ADAPTER_SENSITIVE_LOGGING_ENABLE": "Отключить санитайзер логов (секреты в открытом виде!)",
        "ADAPTER_STREAMING_ENABLE": "Рубильник стриминга: 0 — всегда stream=False (аварийный)",
        "ADAPTER_STREAM_INCLUDE_USAGE": "Передавать usage-токены в стриме (stream_options)",
        "ADAPTER_STRICT_MODELS": "Строгая валидация моделей по списку бэкенда",
        "ADAPTER_DEBUG_TRIM": "Порог обрезки консольного вывода (символы, 0=без обрезки; файл — всегда полный)",
        "ADAPTER_TRACE_REASONING_MAX_CHARS": "Макс. символов reasoning в трейсе (0=без ограничений)",
        "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS": "Макс. символов tool-полей в трейсе (0=без ограничений)",
        # TARGET-маршрутизация входов (см. docs/routing.md): значение задаёт,
        # что делать с запросом на входе. Конкретный формат → прямое
        # преобразование входа в него; passthrough → дословная передача без
        # преобразования; none — вход закрыт (404).
        "ADAPTER_MESSAGES_TARGET": "Куда направлять /v1/messages (Anthropic): формат-цель (преобразование), passthrough — без преобразования, none — вход закрыт (404)",
        "ADAPTER_COMPLETIONS_TARGET": "Куда направлять /v1/chat/completions ([OI]): формат-цель (преобразование), passthrough — без преобразования, none — вход закрыт (404)",
        "ADAPTER_RESPONSES_TARGET": "Куда направлять /v1/responses: формат-цель (преобразование), passthrough — без преобразования, none — вход закрыт (404)",
        # Маппинг моделей agent→backend: тот же формат, что у env
        # ADAPTER_MODELS_MAPPING (docs/environment.md §4). Применяется на
        # лету — config._MAP перестраивается на месте, следующий запрос
        # резолвит модель по новому маппингу.
        "ADAPTER_MODELS_MAPPING": "Маппинг моделей agent→backend: agent:backend,agent2:backend2 (пусто — маппинг отключён)",
    }

    rows = []
    for name in bool_fields:
        value = current_values.get(name, False)
        desc = field_descriptions.get(name, "")
        checked = "checked" if value else ""
        # Hidden-«сосед» bool-поля несёт КОНСТАНТУ "0" (выключено), а не
        # текущее состояние: снятая галка просто отсутствует в теле POST, и
        # без «соседа» выключить bool через форму было бы невозможно. Разбор
        # берёт ПОСЛЕДНЕЕ значение ключа (values[-1]), поэтому hidden обязан
        # идти ПЕРВЫМ, а checkbox — вторым: отмеченная галка ("_NAME=0&NAME=1")
        # даёт 1, снятая (только "_NAME=0") — 0. Hidden с ТЕКУЩИМ состоянием
        # (как было до v0.9.6) перебивал галку и переключение через форму не
        # работало вовсе — только JSON-API.
        hidden_value = "0"
        rows.append(f"""
      <tr>
        <td><label for="{name}">{html.escape(name)}</label></td>
        <td><input type="hidden" name="_{name}" value="{hidden_value}">
            <input type="checkbox" id="{name}" name="{name}" value="1" {checked}>
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

    for name in str_fields:
        value = current_values.get(name, "")
        desc = field_descriptions.get(name, "")
        # Текстовое поле свободного формата: значение как есть (экранируем
        # только HTML). width — чтобы длинная строка маппинга была видна.
        rows.append(f"""
      <tr>
        <td><label for="{name}">{html.escape(name)}</label></td>
        <td><input type="text" id="{name}" name="{name}" value="{html.escape(str(value))}" style="width: 360px"></td>
        <td style="color:#666; font-size: 13px">{html.escape(desc)}</td>
        <td style="color:#999; font-size: 12px">текущее: {html.escape(str(value))}</td>
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

    # Flash-сообщение: ТОЛЬКО об ошибках. Успешное применение ничего не
    # печатает — обновлённые значения и так видны в колонке «текущее», а
    # зелёная плашка «Применено: …» была лишним шумом.
    flash_html = ""
    if applied:
        ignored_list = applied.get("ignored", [])
        if ignored_list:
            flash_html = (
                f'<p style="color:#b8860b">Игнорировано (неверный тип/неизвестный ключ): '
                f"{html.escape(', '.join(ignored_list))}</p>"
            )

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
<p><a href="/">Статус 📊</a> &nbsp;·&nbsp; <a href="/sessions">Сессии 🗂</a> &nbsp;·&nbsp; <a href="/session">Обзор сессий 📋</a></p>
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
<p style="color:#888; margin-top: 8px; font-size: 13px">
  <b>Log</b> здесь — шаблон для <b>новых</b> сессий: сессия получает это
  значение в момент образования и дальше живёт своим
  (управление — на странице <a href="/sessions">Сессии 🗂</a>). Переключение
  этого флага не включает и не выключает запись у уже работающих сессий.
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
    POST → применение валидных значений; страница сообщает только об ошибках
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
            # Каждое bool-поле формы шлёт ПАРУ значений: hidden-поле "_<NAME>"
            # с КОНСТАНТОЙ "0" (в HTML оно идёт ПЕРВЫМ) и checkbox
            # (value=1, шлётся ВТОРЫМ, только когда отмечен). Разбор ниже берёт
            # ПОСЛЕДНЕЕ значение ключа: отмеченная галка ("_NAME=0&NAME=1") →
            # True, снятая (только "_NAME=0") → False; ключ "_NAME" пишется в
            # data как "NAME". int-поля всегда присутствуют (type="number").
            parsed = parse_qs(body, keep_blank_values=True)
            for key, values in parsed.items():
                if not values:
                    continue
                # Последнее значение ключа: у bool-поля это checkbox, если он
                # отмечен, иначе hidden-константа "0".
                val = values[-1]
                # hidden-ключ "_NAME" → имя "NAME"
                data_key = key[1:] if key.startswith("_") else key
                # Разбор по ОЖИДАЕМОМУ типу ключа (config._RUNTIME_CONFIG_TYPES),
                # а не по значению: bool-эвристика для int-поля крадёт "0" →
                # False (set_runtime_config отклоняет bool для int — поле
                # уходило в «Игнорировано»). Тип смотрим по data_key (после
                # снятия "_"-префикса — hidden-«сосед» bool-поля).
                expected = config._RUNTIME_CONFIG_TYPES.get(data_key)
                if expected is int:
                    # int-поля (type="number"): значение числами 0/3000/... —
                    # строго int(), без bool-эвристики ("0" → 0, не False).
                    try:
                        data[data_key] = int(val)
                    except ValueError:
                        data[data_key] = val
                elif expected is bool:
                    # bool-поля: hidden "_NAME"=0 + checkbox value=1.
                    if val.lower() in ("1", "true", "on", "yes"):
                        data[data_key] = True
                    elif val.lower() in ("0", "false", "off", "no"):
                        data[data_key] = False
                    else:
                        data[data_key] = val
                elif expected is str:
                    # str-поля (ADAPTER_MODELS_MAPPING): значение как есть —
                    # без bool/int-эвристик ("0" остаётся строкой "0", не
                    # False/0). Пустая строка валидна (маппинг отключён).
                    data[data_key] = val
                else:
                    # enum-select (TARGET) и посторонние ключи: select шлёт
                    # строку из домена — как есть. Прежняя эвристика
                    # (bool-слова → int → строка) сохраняется ТОЛЬКО для
                    # ключей вне пула (их всё равно отклонит set_runtime_config).
                    if val.lower() in ("1", "true", "on", "yes"):
                        data[data_key] = True
                    elif val.lower() in ("0", "false", "off", "no"):
                        data[data_key] = False
                    else:
                        try:
                            data[data_key] = int(val)
                        except ValueError:
                            data[data_key] = val

        # Применяем через set_runtime_config
        result = config.set_runtime_config(**data)

        # Что НЕ применилось: неверный тип для известного ключа или ключ вне
        # пула. Успех отдельно не собираем — страница сообщает ТОЛЬКО об
        # ошибках (зелёный «Применено» убран как лишний шум).
        applied_ignored = []
        for key, value in data.items():
            if key in config.RUNTIME_CONFIG_POOL:
                expected_type = config._RUNTIME_CONFIG_TYPES.get(key)
                # bool — подкласс int: для int-поля проверяем строго.
                type_ok = (expected_type is bool and isinstance(value, bool)) or (
                    expected_type is int and isinstance(value, int) and not isinstance(value, bool)
                )
                # str-поле (ADAPTER_MODELS_MAPPING): любая строка валидна.
                if expected_type is str and isinstance(value, str):
                    type_ok = True
                # enum-поле (TARGET): value — строка из допустимого набора.
                if (
                    isinstance(expected_type, tuple)
                    and expected_type[0] == "enum"
                    and isinstance(value, str)
                    and value in expected_type[1]
                ):
                    type_ok = True
                if not (type_ok and result.get(key) == value):
                    applied_ignored.append(key)
            else:
                applied_ignored.append(key)

        # Ошибки (если есть) показываются на отрендеренной странице — без
        # редиректа: форма остаётся на POST-ответе с актуальными значениями.
        applied_data = {"ignored": applied_ignored}
        html_content = _render_config_page(result, applied=applied_data)
        handler._write(200, "text/html; charset=utf-8", html_content)


__all__ = [
    "_render_config_page",
    "ConfigEndpoint",
]
