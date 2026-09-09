"""SSE streaming: OpenAI backend chat.completions streaming -> Anthropic client SSE.

Reads SSE lines from the backend response chunk by chunk and immediately
translates each into Anthropic streaming events (message_start /
content_block_start / content_block_delta / content_block_stop /
message_delta / message_stop).
"""

import json
import uuid

from . import config
from .config import _cap
from .logger import _dr
from .session_log import write_debug_json, write_warn_file
from .tracer import _register_tool_use, _trace


def _sse_write(wfile, event: str, data: dict) -> None:
    """Записывает один SSE-event в поток клиента и сразу флашит буфер —
    без flush() событие может застрять в буфере сокета и не дойти до
    клиента вовремя, что свело бы на нет весь смысл стриминга."""
    chunk = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()
    wfile.write(chunk)
    wfile.flush()


def stream_openai_to_anthropic(
    resp,
    wfile,
    model,
    session_id,
    req_id,
    approx_prompt_chars=0,
    bytes_sink=None,
    out_body=None,
    backend_url="",
):
    """Построчно читает SSE-ответ backend'а (OpenAI chat.completions
    streaming формат: строки ``data: {...}``, завершается ``data: [DONE]``)
    и на лету конвертирует каждый чанк в поток Anthropic-событий
    (message_start / content_block_start / content_block_delta /
    content_block_stop / message_delta / message_stop), записывая их в
    wfile сразу по мере поступления.

    Логика конвертации структуры контента (text / tool_use, fallback
    tool-call'ов из текста, trace) намеренно повторяет
    convert_openai_to_anthropic — просто по кусочкам, а не одним объектом
    в конце. Бросает исключение наружу при обрыве соединения — вызывающий
    код (do_POST) решает, можно ли ещё retry или поток уже начался и надо
    сообщить об ошибке SSE-событием "error".

    ``bytes_sink`` — необязательный список-ячейка [int]: если передан, в
    bytes_sink[0] накапливается сумма длин ВСЕХ сырых строк ответа бэкенда
    (включая пустые разделители и не-data-строки). Список (а не возврат
    значения), чтобы не менять пару (stop_reason, usage), которую
    распаковывают вызывающие и тесты. Сервер больше не использует его для
    учёта (учёт переведён на токены usage), параметр сохранён как публичная
    опция.

    ``out_body``/``backend_url`` (v0.9.1, опционально) — полное тело запроса
    к бэкенду и его URL, которые вызывающий код (server.do_POST) передаёт,
    когда они уже построены. Нужны только для WARN-записи: если бэкенд не
    вернул usage и input_tokens оценён эвристически — событие [USAGE_WARN]
    пишется в .err-файл сессии (write_warn_file, полный [REQUEST] + репорт,
    безусловный канал). Пока out_body не передан (None) — WARN-запись не
    делается (консольная _dr-строка и trace пишутся всегда). Прямые вызовы
    без этих параметров (тесты, внешние пользователи) не меняют поведения."""
    message_id = f"msg_stream_{req_id}"
    _sse_write(
        wfile,
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    block_index = -1
    block_open = None  # "text" | "tool_use" | None — тип текущего открытого content_block
    text_buf = []
    reasoning_buf = []
    # OpenAI delta.tool_calls[].index -> {"anthropic_index", "id", "name", "args_buf"}
    # (у OpenAI streaming один tool_call собирается из нескольких чанков:
    # id/name обычно в первом, arguments — фрагментами в последующих)
    tool_state: dict[int, dict] = {}
    finish_reason = "stop"
    usage = {}

    def _close_current_block():
        nonlocal block_open
        if block_open is not None:
            _sse_write(
                wfile, "content_block_stop", {"type": "content_block_stop", "index": block_index}
            )
            block_open = None

    for raw_line in resp:
        if bytes_sink is not None:
            bytes_sink[0] += len(raw_line)
        line = raw_line.decode("utf-8", errors="replace").strip("\n").strip("\r")
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if payload == "[DONE]":
            break
        try:
            chunk = json.loads(payload)
        except json.JSONDecodeError:
            _dr(req_id, f"[STREAM_WARN] Не удалось распарсить SSE-чанк backend'а: {payload[:200]}")
            continue

        if chunk.get("usage"):
            usage = chunk["usage"]

        choices = chunk.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}
        if choice.get("finish_reason"):
            finish_reason = choice["finish_reason"]

        # reasoning_content не является отдельным Anthropic content-block'ом
        # (харнесс его не ждёт в этом протоколе) — только копим для trace,
        # как и в нестриминговом convert_openai_to_anthropic.
        if delta.get("reasoning_content"):
            reasoning_buf.append(delta["reasoning_content"])

        text_piece = delta.get("content")
        if text_piece:
            if block_open != "text":
                _close_current_block()
                block_index += 1
                block_open = "text"
                _sse_write(
                    wfile,
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {"type": "text", "text": ""},
                    },
                )
            text_buf.append(text_piece)
            _sse_write(
                wfile,
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": block_index,
                    "delta": {"type": "text_delta", "text": text_piece},
                },
            )

        for tc in delta.get("tool_calls") or []:
            oi = tc.get("index", 0)
            func = tc.get("function", {}) or {}
            st = tool_state.get(oi)
            if st is None:
                _close_current_block()
                block_index += 1
                block_open = "tool_use"
                tool_use_id = tc.get("id") or f"call_{uuid.uuid4().hex[:12]}"
                name = func.get("name", "")
                st = {
                    "anthropic_index": block_index,
                    "id": tool_use_id,
                    "name": name,
                    "args_buf": [],
                }
                tool_state[oi] = st
                # Регистрируем производителя tool_use_id сразу, как и в
                # нестриминговом пути (см. convert_openai_to_anthropic) —
                # причинность tool_use -> tool_result не должна зависеть от
                # того, стримился ответ или нет.
                _register_tool_use(session_id, tool_use_id, req_id, tool_name=name)
                _sse_write(
                    wfile,
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tool_use_id,
                            "name": name,
                            "input": {},
                        },
                    },
                )
            elif func.get("name") and not st["name"]:
                st["name"] = func["name"]

            args_piece = func.get("arguments")
            if args_piece:
                st["args_buf"].append(args_piece)
                # partial_json конкатенируется клиентом точно так же, как
                # OpenAI конкатенирует фрагменты arguments — реконструкция
                # полного JSON на стороне адаптера не нужна, пробрасываем
                # фрагмент как есть.
                _sse_write(
                    wfile,
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": st["anthropic_index"],
                        "delta": {"type": "input_json_delta", "partial_json": args_piece},
                    },
                )

    _close_current_block()

    stop_reason = finish_reason
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason not in ("stop", "length"):
        stop_reason = "end_turn"

    # РАНЬШЕ здесь пробрасывался только output_tokens — input_tokens нигде
    # за весь стрим не сообщался клиенту (message_start шлёт его нулём ДО
    # начала генерации, реальное значение backend отдаёт только в usage
    # последнего чанка). Из-за этого харнесс всю сессию оценивал заполнение
    # контекстного окна вслепую. Теперь: если backend прислал usage (см.
    # ADAPTER_STREAM_INCLUDE_USAGE/stream_options.include_usage) — отдаём
    # реальный prompt_tokens здесь же, в единственном месте потокового
    # ответа, где он физически может быть достоверным.
    input_tokens = usage.get("prompt_tokens")
    input_tokens_estimated = False
    if input_tokens is None:
        # Backend не поддержал stream_options.include_usage (или флаг
        # ADAPTER_STREAM_INCLUDE_USAGE=0) — не отдаём молчаливый 0, это и
        # была первопричина бага. Грубая эвристика лучше тишины, но
        # ЯВНО помечается как оценочная и в trace, и для будущей отладки.
        input_tokens = max(1, approx_prompt_chars // 4) if approx_prompt_chars else 0
        input_tokens_estimated = True
        warn_text = (
            f"Backend не вернул usage в стриме — input_tokens оценён "
            f"эвристически (~{input_tokens}, chars/4), реальное число неизвестно"
        )
        _dr(req_id, f"[USAGE_WARN] {warn_text}")
        if out_body is not None:
            # WARN-событие в .err-файл сессии (v0.9.1): полный запрос +
            # репорт, безусловный канал (вне ENABLE/PARTS/TRIM). out_body
            # передан только когда вызывающий код (server.do_POST) уже
            # построил тело — прямые вызовы без параметра записи не делают.
            write_warn_file(
                session_id,
                req_id,
                backend_url=backend_url,
                model=model,
                out_body=out_body,
                warn_body=warn_text,
            )

    _sse_write(
        wfile,
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": usage.get("completion_tokens", 0),
            },
        },
    )
    _sse_write(wfile, "message_stop", {"type": "message_stop"})

    _trace(
        session_id,
        req_id,
        "usage_report",
        input_tokens=input_tokens,
        input_tokens_estimated=input_tokens_estimated,
        output_tokens=usage.get("completion_tokens", 0),
        streamed=True,
    )

    # Трассировка результата — по аналогии с событием "response_content" в
    # convert_openai_to_anthropic, но собранная из фрагментов, накопленных
    # за время стрима, а не из одного целого ответа.
    tool_use_summaries = []
    for st in tool_state.values():
        try:
            parsed_input = json.loads("".join(st["args_buf"]) or "{}")
        except json.JSONDecodeError:
            parsed_input = {}
        traced_input = parsed_input
        if config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS > 0:
            serialized = json.dumps(parsed_input, ensure_ascii=False, default=str)
            if len(serialized) > config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS:
                traced_input = _cap(serialized, config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS)
        tool_use_summaries.append({"id": st["id"], "name": st["name"], "input": traced_input})

    full_text = "".join(text_buf)
    full_reasoning = "".join(reasoning_buf)

    # Лог агрегированного ответа стрима (аналог [RESPONSE] в нестриминговой
    # ветке server.py) — пишется один раз по завершении стрима, содержит
    # весь текст, tool calls и reasoning. Snapshot держит ПОЛНЫЙ текст (для
    # файла при ADAPTER_DEBUG_ENABLE=1); консоль обрежет строку до
    # ADAPTER_DEBUG_TRIM в logger._write. (Reasoning-поля _trace ниже
    # по-прежнему ограничиваются ADAPTER_TRACE_REASONING_MAX_CHARS — это
    # отдельный int-лимит trace, к консольному trim отношения не имеет.)
    # Санитайзер работает через _dr() → redact().
    resp_snapshot = {
        "streamed": True,
        "text_len": len(full_text),
        "reasoning_len": len(full_reasoning),
        "tool_uses_count": len(tool_use_summaries),
        "text": full_text,
        "reasoning": full_reasoning,
        "tool_uses": tool_use_summaries,
    }
    _dr(
        req_id,
        f"[RESPONSE] {json.dumps(resp_snapshot, ensure_ascii=False, default=str)}",
    )
    if config.ADAPTER_DEBUG_PARTS:
        write_debug_json(session_id, "RESPONSE", resp_snapshot)

    _trace(
        session_id,
        req_id,
        "response_content",
        text_len=len(full_text),
        tool_uses=tool_use_summaries,
        finish_reason_raw=finish_reason,
        stop_reason_mapped=stop_reason,
        reasoning_present=bool(full_reasoning.strip()),
        reasoning_len=len(full_reasoning),
        reasoning=_cap(full_reasoning, config.ADAPTER_TRACE_REASONING_MAX_CHARS),
        streamed=True,
    )

    return stop_reason, usage


# --- Passthrough E→E: релей SSE-потока бэкенда клиенту без конвертации ---

# usage-ключи ответа по формату (в passthrough формат входа == формата
# выхода — тело не пересобирается): у chat.completions usage несёт
# prompt_tokens/completion_tokens, у responses/messages —
# input_tokens/output_tokens.
_USAGE_KEYS = {
    "completions": ("prompt_tokens", "completion_tokens"),
    "responses": ("input_tokens", "output_tokens"),
    "messages": ("input_tokens", "output_tokens"),
}


def _extract_usage(obj: dict, inp_fmt: str) -> dict:
    """Достаёт usage-блок из одного SSE-события по формату входа.

    Расположение usage в потоке разное: у completions/messages это поле
    верхнего уровня (у completions — финальный чанк при
    stream_options.include_usage, у messages — событие message_delta); у
    responses usage приходит во вложенном ``response.usage`` события
    response.completed (в верхнеуровневом ``type``/``sequence_number`` его
    нет). Возвращает {} для событий без usage."""
    if inp_fmt == "responses":
        resp_obj = obj.get("response")
        if not isinstance(resp_obj, dict):
            return {}
        u = resp_obj.get("usage")
        return u if isinstance(u, dict) else {}
    u = obj.get("usage")
    return u if isinstance(u, dict) else {}


def relay_sse(resp, wfile, req_id: str, inp_fmt: str) -> dict:
    """Релей SSE-потока бэкенда клиенту ДОСЛОВНО (passthrough E→E).

    Читает ответ бэкенда построчно и пишет байты в wfile как есть — без
    пере-фрейминга и пере-сериализации (это НЕ конвертация, а релей:
    клиент получает ровно тот поток, что прислал бэкенд, в родном формате
    входа). Конец стрима — по EOF ответа бэкенда (адаптер уже отдал
    ``Connection: close`` в _start_sse, поэтому клиент понимает конец и без
    терминального события).

    Побочно сканирует проходящие строки на usage-блоки (для учёта
    WEBUI-таблицы): вернёт usage ПОСЛЕДНЕГО встреченного usage-события
    (или {}). Ключи возвращаемого dict — НОРМАЛИЗОВАННЫЕ
    input_tokens/output_tokens по формату входа (см. _USAGE_KEYS); у
    responses usage приходит событием response.completed, у completions —
    usage-полем финального чанка (когда stream_options.include_usage).

    Бросает наружу исключения чтения/записи (обрыв соединения бэкенда или
    клиента) — вызывающий код (do_POST) решает: CLIENT_GONE / SSE-error
    после старта.
    """
    usage_raw = {}
    try:
        for raw_line in resp:
            wfile.write(raw_line)
            wfile.flush()
            try:
                line = raw_line.decode("utf-8", errors="replace").strip()
            except Exception:
                continue
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            u = _extract_usage(obj, inp_fmt)
            if u:
                usage_raw = u  # последний usage-блок за стрим
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        raise
    if not usage_raw:
        return {}
    in_key, out_key = _USAGE_KEYS.get(inp_fmt, ("input_tokens", "output_tokens"))
    return {
        "input_tokens": int(usage_raw.get(in_key) or 0),
        "output_tokens": int(usage_raw.get(out_key) or 0),
    }


def _write_sse_error_native(wfile, inp_fmt: str, message: str) -> None:
    """SSE-событие ошибки в РОДНОМ формате входа (passthrough E→E, сбой
    после старта потока — заголовки и часть событий уже ушли клиенту,
    откат на JSON-ответ невозможен).

    Формат события повторяет то, что бэкенд сам прислал бы при ошибке на
    этом эндпоинте (клиент умеет разбирать его в родном протоколе):
    - responses: ``event: error`` + data {type: "error", code, message};
    - messages/completions: событие ``error`` c Anthropic/[OI]-обвязкой
      (error: {type: "error", message: …}).

    Запись молча глотает любые исключения (клиент мог уже отвалиться) —
    вызывающий код не должен падать из-за невозможности дописать поток."""
    try:
        # Явная аннотация: ветки несут разные формы (плоский dict против
        # вложенного error-объекта), иначе mypy выводит dict[str, str] из
        # первой ветки и ругается на остальные.
        payload: dict[str, object]
        if inp_fmt == "responses":
            payload = {
                "type": "error",
                "code": "adapter_error",
                "message": message,
            }
        elif inp_fmt == "completions":
            payload = {"error": {"message": message, "type": "server_error"}}
        else:  # messages — антропик-обвязка ошибки
            payload = {"type": "error", "error": {"type": "error", "message": message}}
        _sse_write(wfile, "error", payload)
    except Exception:
        pass
