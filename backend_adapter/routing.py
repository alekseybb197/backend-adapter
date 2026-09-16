"""Input-endpoint routing: which POST inputs the adapter accepts and what
happens to each request (convert / passthrough / reject). v0.9.0.

Три входных POST-эндпоинта адаптера — /v1/messages, /v1/chat/completions,
/v1/responses — управляются тремя env-переменными ``ADAPTER_*_TARGET``
(префикс переменной = входной эндпоинт; см. config.ADAPTER_*_TARGET).
Значение переменной задаёт прямое преобразование входа в указанный формат
(messages|completions|responses), дословную передачу на бэкенд без
преобразования (passthrough) или запрет обработки входа (none).

Модуль импортирует ``backend_adapter.config`` (корень DAG) и
``session_settings`` (лист DAG, пер-сессионные переопределения, v0.9.5);
его импортирует только ``server.py``. Чистая логика без сети и HTTP —
маршрут выбирается только по значению TARGET-переменной, синхронных
запросов к бэкенду во время выбора НЕТ.
"""

from __future__ import annotations

from typing import Literal

from . import config, session_settings

# Канонические форматы = входные эндпоинты (completions|messages|responses).
# Значение TARGET-переменной поверх них — те же три имени (прямое
# преобразование) плюс passthrough/none.
Format = Literal["messages", "completions", "responses"]
TargetValue = Literal["messages", "completions", "responses", "passthrough", "none"]

# Входные пути адаптера — для трёх форматов, участвующих в роутинге
# (распознавание входа в server.py, см. input_path_to_format).
INPUT_PATHS: dict[Format, str] = {
    "messages": "/v1/messages",
    "completions": "/v1/chat/completions",
    "responses": "/v1/responses",
}

# Реестр реализованных пар «вход → выход» (прямое преобразование форматов).
# True = преобразование существует; False = пара объявлена НЕреализованной
# для этой итерации — директива на неё даёт ошибку агенту («преобразование
# не реализовано», 400). Дословная передача без преобразования — отдельное
# значение TARGET ``passthrough`` (не через этот реестр).
#
# Реализованные преобразования:
#   messages → completions — полный конвертер (запрос
#       convert_messages_anthropic_to_openai, ответ convert_openai_to_anthropic,
#       стрим stream_openai_to_anthropic — convert.py/streaming.py);
#   messages → messages — сортировка полей: все role=system переносятся в
#       начало диалога (normalize_messages_system_first, convert.py), т.к.
#       часть бэкендов (vLLM-шаблон чата) падает 400 'System message must be
#       at the beginning';
#   responses → responses (v0.9.6) — «внутренний конвертор» ответов, в
#       отличие от TARGET=passthrough тело НЕ уходит бэкенду дословно:
#       (1) force_store_false (convert.py) принудительно выставляет
#       store=false — сторонний бэкенд за адаптером не может резолвить
#       previous_response_id чужого response, поэтому серверный стейт
#       Responses API здесь не используется в принципе; (2)
#       detect_model_switch_command (convert.py) перехватывает служебное
#       сообщение "/model <имя>" ДО похода к бэкенду и переключает модель
#       сессии через session_settings.set_model_override — обходит известную
#       ненадёжность нативного /model у Codex CLI на кастомных
#       model_provider (см. документацию проекта). В отличие от
#       messages→messages здесь нет сортировки полей — сама структура
#       input не меняется, кроме этих двух точечных вмешательств;
#   responses → completions (v0.9.7) — полный кросс-форматный конвертер,
#       аналог messages→completions по роли (запрос
#       convert_responses_input_to_openai_messages, ответ
#       convert_openai_completions_to_responses, стрим
#       stream_openai_completions_to_responses — convert.py/streaming.py).
#       Как и messages→messages, применяет normalize_messages_system_first
#       («сортировка полей» — system первым) к собранным messages. Как и
#       responses→responses, поддерживает "/model <имя>"
#       (detect_model_switch_command) — служебная команда перехватывается
#       ДО конвертации, реальный бэкенд не вызывается.
# Прочие пары (completions→messages, messages→responses, …, а также
# self-пара completions→completions) не реализованы: для дословной передачи
# таких входов используйте TARGET=passthrough.
IMPLEMENTED_CONVERSIONS: dict[tuple[Format, Format], bool] = {
    ("messages", "completions"): True,
    ("messages", "messages"): True,
    ("completions", "messages"): False,
    ("messages", "responses"): False,
    ("responses", "messages"): False,
    ("completions", "responses"): False,
    ("responses", "completions"): True,
    ("completions", "completions"): False,
    ("responses", "responses"): True,
}

# Zero-config дефолты TARGET-переменных (если env не задана): описывают
# ТЕКУЩИЕ возможности конвертера — принимается только /v1/messages и
# конвертируется в chat completions; остальные два входа выключены (404).
_TARGET_DEFAULTS: dict[Format, TargetValue] = {
    "messages": "completions",
    "completions": "none",
    "responses": "none",
}

# Имя env-переменной для каждого входа (префикс переменной == вход).
_ENV_NAMES: dict[Format, str] = {
    "messages": "ADAPTER_MESSAGES_TARGET",
    "completions": "ADAPTER_COMPLETIONS_TARGET",
    "responses": "ADAPTER_RESPONSES_TARGET",
}

# Тексты ошибок маршрутизации (уходят клиенту как {"error": …}). В
# ERROR_DISABLED подставляется ПОЛНОЕ имя переменной из _ENV_NAMES
# (ADAPTER_<INP>_TARGET) — префикс ADAPTER_ в шаблоне не дублируется.
ERROR_DISABLED = "endpoint is disabled ({env}=none)"
ERROR_UNIMPLEMENTED = "conversion '{inp}' -> '{out}' is not implemented by this adapter"


def target_env_name(inp: Format) -> str:
    """Имя TARGET-переменной входного эндпойнта (messages →
    'ADAPTER_MESSAGES_TARGET').

    Публичный доступ к ``_ENV_NAMES`` для WEBUI (webui_sessions): селект
    строки таблицы Sessions адресует пер-сессионное переопределение ИМЕННО
    той переменной, которая управляет входом строки."""
    return _ENV_NAMES[inp]


def input_path_to_format(path: str) -> Format | None:
    """Формат входного эндпоинта по пути запроса ('/v1/messages' → 'messages').

    None — путь не является входным POST-эндпоинтом адаптера (не
    /v1/messages, /v1/chat/completions, /v1/responses)."""
    for fmt, ep_path in INPUT_PATHS.items():
        if path == ep_path or path.startswith(ep_path + "?"):
            return fmt
    return None


def target_for_input(inp: Format, session_id: str = "") -> TargetValue:
    """Живое значение TARGET-переменной для входного эндпоинта.

    Читает ``config.ADAPTER_<INP>_TARGET`` (атрибут модуля config, не
    снимок импорта): значение выставляется из env на импорте и входит в
    runtime-пул — ``/config`` переприсваивает тот же атрибут, поэтому
    чтение через ``getattr`` видит смену на лету и переживает reload
    конфига в тестах.

    v0.9.5: при непустом ``session_id`` значение берётся ПЕР-СЕССИОННО
    (``session_settings.effective`` поверх общей настройки). Переопределения
    нет — действует общая настройка приложения (живое наследование), т.е.
    поведение прежних версий без изменений."""
    name = _ENV_NAMES[inp]
    if session_id:
        value = session_settings.effective(session_id, name)
    else:
        value = getattr(config, name, _TARGET_DEFAULTS[inp])
    assert value in config.TARGET_ALLOWED_VALUES, value
    return value  # type: ignore[return-value]


def decide(inp: Format, session_id: str = "") -> tuple[str, Format | None, str, int]:
    """Решение о маршруте одного запроса на входе ``inp``.

    ``session_id`` (v0.9.5) — пер-сессионный TARGET: при непустом значении
    действующая цель берётся с учётом переопределений сессии (см.
    ``target_for_input``). Пустая строка — общие настройки приложения.

    Возвращает ``(action, output_fmt, error_msg, http_status)``:
    - ``("passthrough", inp, "", 200)`` — TARGET=passthrough: тело запроса
      уходит бэкенду КАК ЕСТЬ на эндпойнт входного формата;
    - ``("convert", out, "", 200)`` — TARGET = конкретный формат: прямое
      преобразование inp → out (реализованная пара реестра
      IMPLEMENTED_CONVERSIONS);
    - ``("reject", None, текст, 400)`` — запрос валиден, но маршрута нет:
      пара не реализована (400);
    - ``("disabled", None, текст, 404)`` — вход выключен (TARGET=none).

    Решение принимается ТОЛЬКО по TARGET-переменной — поддержка эндпойнта
    бэкендом не проверяется (v0.9.9: активные пробы эндпойнтов удалены).
    Неверный выбор эндпойнта фиксируется по факту ошибки бэкенда, а не
    предугадывается.

    v0.9.5: непустой ``session_id`` включает пер-сессионный TARGET (см.
    ``target_for_input``) — сессия может уйти на другой маршрут, чем общая
    настройка приложения, не меняя её для остальных."""
    target = target_for_input(inp, session_id)
    if target == "none":
        return ("disabled", None, ERROR_DISABLED.format(env=_ENV_NAMES[inp]), 404)

    # --- passthrough: дословная передача на эндпойнт входного формата ---
    # Тело и SSE уходят на бэкенд как пришли (вход == выход).
    if target == "passthrough":
        return ("passthrough", inp, "", 200)

    # --- конкретный формат: прямое преобразование inp → out ---
    # Реализованные пары — реестр IMPLEMENTED_CONVERSIONS (messages→completions,
    # messages→messages, responses→responses, responses→completions); для
    # остальных (в т.ч. self-пары completions→completions) преобразования нет
    # (400) — дословная передача таких входов достигается значением
    # TARGET=passthrough.
    assert target in ("messages", "completions", "responses"), target
    out = target
    if IMPLEMENTED_CONVERSIONS.get((inp, out)):
        return ("convert", out, "", 200)
    return ("reject", None, ERROR_UNIMPLEMENTED.format(inp=inp, out=out), 400)
