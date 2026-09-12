"""Input-endpoint routing: which POST inputs the adapter accepts and what
happens to each request (convert / passthrough / reject). v0.9.0.

Три входных POST-эндпоинта адаптера — /v1/messages, /v1/chat/completions,
/v1/responses — управляются тремя env-переменными ``ADAPTER_*_TARGET``
(префикс переменной = входной эндпоинт; см. config.ADAPTER_*_TARGET).
Значение переменной задаёт прямое преобразование входа в указанный формат
(messages|completions|responses), дословную передачу на бэкенд без
преобразования (passthrough) или запрет обработки входа (none).

Модуль — лист DAG: на верхнем уровне импортирует только ``backend_adapter.
config`` (корень DAG); его импортирует только ``server.py``. Чистая логика
без сети и HTTP — решение о маршруте принимается по кэшу проб
(``config.endpoint_support``), синхронных запросов к бэкенду во время
выбора НЕТ.
"""

from __future__ import annotations

from typing import Literal

from . import config

# Канонические форматы = входные эндпоинты = короткие имена ENDPOINT_PROBES
# (completions|messages|responses). Значение TARGET-переменной поверх них —
# те же три имени (прямое преобразование) плюс passthrough/none.
Format = Literal["messages", "completions", "responses"]
TargetValue = Literal["messages", "completions", "responses", "passthrough", "none"]

# Входные пути адаптера. Зеркало ENDPOINT_PROBES (config.py) — для трёх
# форматов, участвующих в роутинге; соответствие путей форматам
# зафиксировано unit-тестом (не синхронизируется автоматически).
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
#       at the beginning'.
# Прочие пары (completions→messages, messages→responses, …, а также
# self-пары completions→completions и responses→responses) не реализованы:
# для дословной передачи таких входов используйте TARGET=passthrough.
IMPLEMENTED_CONVERSIONS: dict[tuple[Format, Format], bool] = {
    ("messages", "completions"): True,
    ("messages", "messages"): True,
    ("completions", "messages"): False,
    ("messages", "responses"): False,
    ("responses", "messages"): False,
    ("completions", "responses"): False,
    ("responses", "completions"): False,
    ("completions", "completions"): False,
    ("responses", "responses"): False,
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
ERROR_UNSUPPORTED = "backend '{backend}' does not support {fmt} (probe: endpoint not found)"


def input_path_to_format(path: str) -> Format | None:
    """Формат входного эндпоинта по пути запроса ('/v1/messages' → 'messages').

    None — путь не является входным POST-эндпоинтом адаптера (не
    /v1/messages, /v1/chat/completions, /v1/responses)."""
    for fmt, ep_path in INPUT_PATHS.items():
        if path == ep_path or path.startswith(ep_path + "?"):
            return fmt
    return None


def target_for_input(inp: Format) -> TargetValue:
    """Живое значение TARGET-переменной для входного эндпоинта.

    Читает ``config.ADAPTER_<INP>_TARGET`` (атрибут модуля config, не
    снимок импорта): значение выставляется из env на импорте и входит в
    runtime-пул — ``/config`` переприсваивает тот же атрибут, поэтому
    чтение через ``getattr`` видит смену на лету и переживает reload
    конфига в тестах."""
    value = getattr(config, _ENV_NAMES[inp], _TARGET_DEFAULTS[inp])
    assert value in config.TARGET_ALLOWED_VALUES, value
    return value  # type: ignore[return-value]


def decide(inp: Format, backend_name: str) -> tuple[str, Format | None, str, int]:
    """Решение о маршруте одного запроса на входе ``inp`` к бэкенду.

    Возвращает ``(action, output_fmt, error_msg, http_status)``:
    - ``("passthrough", inp, "", 200)`` — TARGET=passthrough: тело запроса
      уходит бэкенду КАК ЕСТЬ на эндпойнт входного формата (поддержка
      подтверждена кэшем проб либо, при отсутствии данных, не опровергнута);
    - ``("convert", out, "", 200)`` — TARGET = конкретный формат: прямое
      преобразование inp → out (реализованная пара реестра
      IMPLEMENTED_CONVERSIONS), бэкенд поддерживает ``out``;
    - ``("reject", None, текст, 400|502)`` — запрос валиден, но маршрута
      нет: пара не реализована (400), либо бэкенд подтверждённо не
      поддерживает целевой формат (502);
    - ``("disabled", None, текст, 404)`` — вход выключен (TARGET=none).

    Решения — ТОЛЬКО по кэшу проб (``config.endpoint_support``): фоновая
    probe_endpoints + пер-модельные пробы usage-таблицы. Сети здесь НЕТ.

    Семантика None (эндпоинт не пробовался / пробы выключены): «нет данных»
    НЕ блокирует — маршрут выбирается оптимистично (passthrough / convert),
    как вёл бы себя адаптер без роутинга. Отказ (502) — только при
    подтверждённом found=False (проба была, не-HTTP-200).
    """
    target = target_for_input(inp)
    if target == "none":
        return ("disabled", None, ERROR_DISABLED.format(env=_ENV_NAMES[inp]), 404)

    # --- passthrough: дословная передача на эндпойнт входного формата ---
    # Тело и SSE уходят на бэкенд как пришли (вход == выход); проверяем
    # поддержку именно входного формата.
    if target == "passthrough":
        if config.endpoint_support(backend_name, inp) is not False:
            return ("passthrough", inp, "", 200)
        return ("reject", None, ERROR_UNSUPPORTED.format(backend=backend_name, fmt=inp), 502)

    # --- конкретный формат: прямое преобразование inp → out ---
    # Реализованные пары — реестр IMPLEMENTED_CONVERSIONS (messages→completions
    # и messages→messages); для self-пар completions→completions и
    # responses→responses преобразования нет (400) — дословная передача таких
    # входов достигается значением TARGET=passthrough.
    assert target in ("messages", "completions", "responses"), target
    out = target
    if IMPLEMENTED_CONVERSIONS.get((inp, out)):
        if config.endpoint_support(backend_name, out) is not False:
            return ("convert", out, "", 200)
        return ("reject", None, ERROR_UNSUPPORTED.format(backend=backend_name, fmt=out), 502)
    return ("reject", None, ERROR_UNIMPLEMENTED.format(inp=inp, out=out), 400)
