"""Input-endpoint routing: which POST inputs the adapter accepts and what
happens to each request (convert / passthrough / reject). v0.9.0.

Три входных POST-эндпоинта адаптера — /v1/messages, /v1/chat/completions,
/v1/responses — управляются тремя env-переменными ``ADAPTER_*_TARGET``
(префикс переменной = входной эндпоинт; см. config.ADAPTER_*_TARGET).
Значение переменной задаёт целевой формат (messages|completions|responses),
автовыбор (auto) или запрет обработки входа (none).

Модуль — лист DAG: на верхнем уровне импортирует только ``backend_adapter.
config`` (корень DAG); его импортирует только ``server.py``. Чистая логика
без сети и HTTP — решение о маршруте принимается по кэшу проб
(``config.endpoint_support``), синхронных запросов к бэкенду во время
выбора НЕТ (решение пользователя для режима auto).
"""

from __future__ import annotations

from typing import Literal

from . import config

# Канонические форматы = входные эндпоинты = короткие имена ENDPOINT_PROBES
# (completions|messages|responses). Значение TARGET-переменной поверх них —
# те же три имени плюс auto/none.
Format = Literal["messages", "completions", "responses"]
TargetValue = Literal["messages", "completions", "responses", "auto", "none"]

# Входные пути адаптера. Зеркало ENDPOINT_PROBES (config.py) — для трёх
# форматов, участвующих в роутинге; соответствие путей форматам
# зафиксировано unit-тестом (не синхронизируется автоматически).
INPUT_PATHS: dict[Format, str] = {
    "messages": "/v1/messages",
    "completions": "/v1/chat/completions",
    "responses": "/v1/responses",
}

# Реестр реализованных пар «вход → выход» (явная конвертация форматов, НЕ
# passthrough). True = конвертер существует; False = пара объявлена
# НЕреализованной для этой итерации — директива на неё даёт ошибку агенту
# («преобразование не реализовано»), passthrough при этом возможен только
# когда вход == выход (формат один и тот же).
#
# Сейчас реализована ровно одна пара: messages → completions (запрос
# convert_messages_anthropic_to_openai, ответ convert_openai_to_anthropic,
# стрим stream_openai_to_anthropic — convert.py/streaming.py). Обратные и
# перекрёстные пары (completions→messages, messages→responses, …) в этой
# итерации не пишутся.
IMPLEMENTED_CONVERSIONS: dict[tuple[Format, Format], bool] = {
    ("messages", "completions"): True,
    ("completions", "messages"): False,
    ("messages", "responses"): False,
    ("responses", "messages"): False,
    ("completions", "responses"): False,
    ("responses", "completions"): False,
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

# Тексты ошибок маршрутизации (уходят клиенту как {"error": …}).
ERROR_DISABLED = "endpoint is disabled (ADAPTER_{env}=none)"
ERROR_UNIMPLEMENTED = "conversion '{inp}' -> '{out}' is not implemented by this adapter"
ERROR_UNSUPPORTED = "backend '{backend}' does not support {fmt} (probe: endpoint not found)"
ERROR_NO_ROUTE = (
    "no route for {inp} request to backend '{backend}': backend does not "
    "serve {inp} and no implemented conversion applies"
)


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
    снимок импорта): константы config выставляются на импорте из env и в
    runtime-пул не входят, поэтому чтение через ``getattr`` эквивалентно
    импортированной константе, но переживает reload конфига в тестах."""
    value = getattr(config, _ENV_NAMES[inp], _TARGET_DEFAULTS[inp])
    assert value in ("messages", "completions", "responses", "auto", "none"), value
    return value  # type: ignore[return-value]


def decide(inp: Format, backend_name: str) -> tuple[str, Format | None, str, int]:
    """Решение о маршруте одного запроса на входе ``inp`` к бэкенду.

    Возвращает ``(action, output_fmt, error_msg, http_status)``:
    - ``("passthrough", inp, "", 200)`` — тело запроса уходит бэкенду КАК
      ЕСТЬ (формат входа = формату бэкенда; поддержка подтверждена кэшем
      проб либо — в явном режиме без данных — не опровергнута);
    - ``("convert", out, "", 200)`` — реализованная конверсия inp → out
      (сегодня только messages → completions), бэкенд поддерживает ``out``;
    - ``("reject", None, текст, 400|502)`` — запрос валиден, но маршрута
      нет: нереализованная пара / auto без подходящего пути (400), либо
      бэкенд подтверждённо не поддерживает целевой формат (502);
    - ``("disabled", None, текст, 404)`` — вход выключен (TARGET=none).

    Решения — ТОЛЬКО по кэшу проб (``config.endpoint_support``): фоновая
    probe_endpoints + пер-модельные пробы usage-таблицы. Сети здесь НЕТ.

    Семантика None (эндпоинт не пробовался / пробы выключены) РАЗНАЯ для
    режимов (решение пользователя; ADAPTER_ENDPOINT_PROBE остаётся 1):
    - в ``auto`` None трактуется как False — passthrough/convert без
      подтверждения поддержки (found=True) не выбираются: маршрут
      выбирается только по факту, иначе reject 400 «no route»;
    - в ЯВНОМ режиме (TARGET=формат) None НЕ блокирует: «нет данных» →
      оптимистичная попытка (passthrough E→E / convert), как вёл бы себя
      адаптер без роутинга. Отказ (502) — только при подтверждённом
      found=False (проба была, не-HTTP-200).
    """
    target = target_for_input(inp)
    if target == "none":
        return ("disabled", None, ERROR_DISABLED.format(env=_ENV_NAMES[inp]), 404)

    supported_inp = config.endpoint_support(backend_name, inp)

    # --- auto: passthrough E→E при поддержке бэкендом входного формата ---
    # (для messages это passthrough messages→messages на антропик-совместимом
    # бэкенде — приоритет над конверсией в completions); иначе — реализованная
    # конверсия из этого входа; иначе — ошибка агенту (400 «no route»).
    if target == "auto":
        if supported_inp is True:
            return ("passthrough", inp, "", 200)
        for cand in ("messages", "completions", "responses"):
            if (inp, cand) in IMPLEMENTED_CONVERSIONS and IMPLEMENTED_CONVERSIONS[(inp, cand)]:
                if config.endpoint_support(backend_name, cand) is True:
                    return ("convert", cand, "", 200)
                break
        return ("reject", None, ERROR_NO_ROUTE.format(inp=inp, backend=backend_name), 400)

    # --- явный целевой формат (не auto/none) ---
    assert target in ("messages", "completions", "responses"), target
    out = target
    if out == inp:
        # Passthrough E→E: подтверждённая поддержка (found=True) либо нет
        # данных (None — оптимистично, «как без роутинга»); подтверждённый
        # отказ (found=False) — 502.
        if supported_inp is not False:
            return ("passthrough", inp, "", 200)
        return ("reject", None, ERROR_UNSUPPORTED.format(backend=backend_name, fmt=inp), 502)
    if IMPLEMENTED_CONVERSIONS.get((inp, out)):
        if config.endpoint_support(backend_name, out) is not False:
            return ("convert", out, "", 200)
        return ("reject", None, ERROR_UNSUPPORTED.format(backend=backend_name, fmt=out), 502)
    return ("reject", None, ERROR_UNIMPLEMENTED.format(inp=inp, out=out), 400)
