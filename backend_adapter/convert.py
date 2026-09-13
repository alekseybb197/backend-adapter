"""Message format conversion: Anthropic <-> OpenAI.

Includes pure conversion functions and the traced OpenAI->Anthropic
response converter (which depends on tracer package).
"""

import json
import re
from typing import Any

from . import config
from .config import _cap
from .tracer import _register_tool_use, _trace


def extract_text(content):
    """Извлекает plain text из Anthropic content (строка или массив blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts = []
        for block in content:
            if isinstance(block, str):
                texts.append(block)
            elif block.get("type") == "text":
                texts.append(block.get("text", ""))
        return "\n".join(texts)
    return str(content)


def normalize_messages_system_first(messages: list) -> list:
    """Возвращает НОВЫЙ список сообщений: все role=system переносятся в
    начало (в исходном порядке, БЕЗ склейки), остальные роли сохраняют
    относительный порядок. Для passthrough messages→messages (v0.9.2):
    некоторые бэкенды (vLLM-шаблон чата) требуют system первым
    (Jinja raise_exception 'System message must be at the beginning'),
    а [CC]-сессии несут system-сообщения по ходу диалога и
    <system-reminder> внутри user. В отличие от
    convert_messages_anthropic_to_openai (которая склеивает все system в
    ОДНО сообщение и пересобирает роли/блоки), здесь сообщения НЕ
    пересобираются — только переупорядочиваются: контракт Anthropic-
    формата E→E сохраняется максимально.
    """
    systems = [m for m in messages if m.get("role") == "system"]
    others = [m for m in messages if m.get("role") != "system"]
    return systems + others


def force_store_false(body: dict) -> bool:
    """Принудительно выставляет ``store: false`` в теле запроса Responses
    API (routing: responses→responses и TARGET=passthrough для входа
    responses, v0.9.6). Мутирует ``body`` на месте, возвращает True, если
    значение пришлось поменять (для лога вызывающей стороны).

    Зачем: серверный стейт Responses API (``previous_response_id``)
    физически может резолвить только тот бэкенд, который сам создал
    предыдущий ``response`` — сторонний бэкенд за этим адаптером чужих
    ответов не хранит, а при отсутствии поля Responses API трактует
    ``store`` как true по умолчанию. Пер-сессионный разбор реального
    трафика Codex CLI показал, что сам клиент уже шлёт ``store: false`` и
    пересобирает контекст целиком на своей стороне (см. разбор лога сессии
    в документации проекта) — но полагаться на добросовестность
    конкретного клиента адаптер не должен: это инвариант эндпоинта, а не
    факт одного наблюдённого запроса."""
    if body.get("store") is not False:
        body["store"] = False
        return True
    return False


# Команда переключения модели внутри сессии — см. detect_model_switch_command.
# Разрешён произвольный "непробельный" идентификатор модели без ограничения
# на алфавит (маппинг/валидация имени — уже существующая логика server.py:
# _MAP и ADAPTER_STRICT_MODELS/_AVAILABLE_MODELS, здесь заново не дублируется).
_MODEL_SWITCH_RE = re.compile(r"^/model\s+(\S+)\s*$", re.IGNORECASE)


def _extract_responses_input_text(item: dict) -> str | None:
    """Извлекает текст ОДНОГО input-сообщения Responses API, если оно
    целиком состоит из одного текстового content-блока (``input_text``
    или ``text``). None — сообщение сложнее (несколько блоков, изображение
    и т.п.) либо текстового содержимого нет.

    Используется ТОЛЬКО для detect_model_switch_command: команда
    распознаётся исключительно в «чистом» однородном сообщении, чтобы не
    перехватить настоящий запрос пользователя, который лишь упоминает
    "/model" где-то по ходу более длинного текста."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list) and len(content) == 1:
        block = content[0]
        if isinstance(block, dict) and block.get("type") in ("input_text", "text"):
            return block.get("text")
    return None


def detect_model_switch_command(input_items: list) -> str | None:
    """Ищет служебную команду ``/model <имя>`` в ПОСЛЕДНЕМ input-сообщении
    с ``role="user"`` (routing: responses→responses, v0.9.6). Возвращает
    запрошенное имя модели либо None, если команды нет.

    Назначение: нативное ``/model`` Codex CLI на кастомном
    model_provider ненадёжно — известны баги пикера моделей (перезаписывает
    профиль моделью [OI] при выборе из списка) и клонирования контекста
    предыдущего провайдера при смене модели без рестарта сессии (см.
    документацию проекта). Адаптер держит СОБСТВЕННЫЙ, полностью
    предсказуемый канал переключения: пользователь пишет "/model <имя>"
    обычным сообщением, сервер (server.py) перехватывает его этой функцией
    ДО пересылки бэкенду, сохраняет выбор в session_settings
    (set_model_override — переживает все последующие запросы сессии, пока
    не будет переключён снова или сессия не завершится) и отвечает
    синтетическим подтверждением (streaming.emit_responses_control_message /
    build_responses_control_response) — реальная модель на это сообщение
    не вызывается."""
    if not input_items:
        return None
    last = input_items[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return None
    if last.get("type") not in (None, "message"):
        return None
    text = _extract_responses_input_text(last)
    if not text:
        return None
    m = _MODEL_SWITCH_RE.match(text.strip())
    return m.group(1) if m else None


def convert_tools_anthropic_to_openai(tools):
    """Anthropic tool -> OpenAI tool."""
    openai_tools = []
    for tool in tools:
        openai_tools.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            }
        )
    return openai_tools


def convert_tool_choice_anthropic_to_openai(tc):
    """Anthropic tool_choice -> OpenAI tool_choice."""
    if not tc:
        return "auto"
    t = tc.get("type")
    if t == "auto":
        return "auto"
    if t == "any":
        return "required"
    if t == "tool":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


def extract_tool_results(messages):
    """Достаёт все tool_result-блоки из входящих Anthropic messages "как есть",
    отдельно от convert_messages_anthropic_to_openai (которая тоже их видит,
    но для целей конвертации, а не трассировки). Возвращает список
    {tool_use_id, content, is_error}. Используется в do_POST, чтобы
    эмитить событие "tool_result" ПЕРЕД конвертацией в OpenAI-формат."""
    results = []
    for msg in messages:
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if block.get("type") == "tool_result":
                results.append(
                    {
                        "tool_use_id": block.get("tool_use_id", ""),
                        "content": extract_text(block.get("content")),
                        "is_error": bool(block.get("is_error", False)),
                    }
                )
    return results


def convert_messages_anthropic_to_openai(messages, system):
    """Конвертирует Anthropic messages + system в OpenAI messages.
    ВАЖНО: все system messages ДОЛЖНЫ быть в ОДНОМ сообщении в начале."""
    system_parts = []
    other_msgs = []

    if system:
        if isinstance(system, str):
            system_parts.append(system)
        elif isinstance(system, list):
            for block in system:
                if block.get("type") == "text":
                    system_parts.append(block.get("text", ""))

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")

        if role == "system":
            system_parts.append(extract_text(content))

        elif role == "user":
            if isinstance(content, list):
                text_parts = []
                tool_results = []
                for block in content:
                    if block.get("type") == "tool_result":
                        tool_results.append(
                            {
                                "role": "tool",
                                "tool_call_id": block.get("tool_use_id", ""),
                                "content": extract_text(block.get("content")),
                            }
                        )
                    elif block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                if text_parts:
                    other_msgs.append({"role": "user", "content": "\n".join(text_parts)})
                other_msgs.extend(tool_results)
            else:
                other_msgs.append({"role": "user", "content": extract_text(content)})

        elif role == "assistant":
            if isinstance(content, list):
                text_parts = []
                # assistant_msg собирает и строку текста, и список [OI]-tool_calls —
                # гетерогенные значения под разными ключами; широкая аннотация
                # (а не вывод dict[str, str] по первому литералу) нужна, чтобы
                # mypy не сузил тип ключа "tool_calls" до str.
                assistant_msg: dict[str, Any] = {"role": "assistant"}
                tool_calls = []
                for block in content:
                    if block.get("type") == "text":
                        text_parts.append(block.get("text", ""))
                    elif block.get("type") == "tool_use":
                        tool_calls.append(
                            {
                                "id": block.get("id", ""),
                                "type": "function",
                                "function": {
                                    "name": block.get("name", ""),
                                    "arguments": json.dumps(block.get("input", {})),
                                },
                            }
                        )
                if text_parts:
                    assistant_msg["content"] = "\n".join(text_parts)
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                if len(assistant_msg) > 1:
                    other_msgs.append(assistant_msg)
            else:
                other_msgs.append({"role": "assistant", "content": extract_text(content)})

    result = []
    if system_parts:
        result.append({"role": "system", "content": "\n".join(system_parts)})
    result.extend(other_msgs)
    return result


def parse_tool_calls_from_text(text):
    """Fallback: парсит <tool_call>...</tool_call> из текста (Qwen-формат)."""
    tool_calls = []
    pattern = r"<tool_call>\s*(\{.*?\})\s*</tool_call>"
    matches = re.findall(pattern, text, re.DOTALL)
    for match in matches:
        try:
            data = json.loads(match)
            name = data.get("name") or data.get("function", {}).get("name")
            args = (
                data.get("arguments")
                or data.get("function", {}).get("arguments")
                or data.get("parameters", {})
            )
            if isinstance(args, str):
                args = json.loads(args)
            if name:
                tool_calls.append(
                    {
                        "id": f"call_{abs(hash(match)) % 10000000000}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                )
        except Exception:
            continue

    if not tool_calls and text.strip().startswith("{"):
        try:
            data = json.loads(text.strip())
            name = data.get("name")
            args = data.get("arguments", {})
            if isinstance(args, str):
                args = json.loads(args)
            if name:
                tool_calls.append(
                    {
                        "id": f"call_{abs(hash(text)) % 10000000000}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                )
        except Exception:
            pass
    return tool_calls


def convert_openai_to_anthropic(o, model, session_id="unknown", req_id="unknown"):
    """OpenAI response -> Anthropic response.
    Дополнительно (по сравнению с v6): извлекает reasoning_content и
    трассирует ветвления (fallback-парсинг, маппинг finish_reason)."""
    choice = o.get("choices", [{}])[0]
    message = choice.get("message", {}) or {}
    text = message.get("content") or ""
    tool_calls = message.get("tool_calls", [])
    finish_reason = choice.get("finish_reason", "stop")
    usage = o.get("usage", {})
    reasoning = message.get("reasoning_content", "") or ""

    used_text_fallback = False
    if not tool_calls and text:
        parsed = parse_tool_calls_from_text(text)
        if parsed:
            used_text_fallback = True
            tool_calls = parsed
            clean_text = re.sub(
                r"<tool_call>\s*\{.*?\}\s*</tool_call>", "", text, flags=re.DOTALL
            ).strip()
            text = clean_text if clean_text else ""

    if used_text_fallback:
        # ВАЖНО для оценки качества следования инструкциям: модель не смогла
        # (или харнесс не смог) использовать нативный tool-calling формат
        # backend'а и адаптеру пришлось парсить JSON из текста руками.
        # Это деградация, повышающая риск некорректного вызова инструмента
        # (обрезанный JSON, лишний текст рядом и т.п.). Это реальное
        # ветвление поведения адаптера с последствиями — поэтому у него
        # собственное событие, а не общий "harness_branch".
        _trace(
            session_id,
            req_id,
            "tool_call_fallback",
            parsed_count=len(tool_calls),
            raw_text_len=len(text),
        )

    content = []
    if text and text.strip():
        content.append({"type": "text", "text": text})

    tool_use_summaries = []
    for tc in tool_calls:
        if tc.get("type") == "function":
            func = tc.get("function", {})
            try:
                input_data = json.loads(func.get("arguments", "{}"))
            except (json.JSONDecodeError, TypeError):
                input_data = {}
            name = func.get("name", "")
            tool_use_id = tc.get("id", "")
            content.append(
                {"type": "tool_use", "id": tool_use_id, "name": name, "input": input_data}
            )
            # Регистрируем, что ИМЕННО ЭТОТ запрос (req_id) породил данный
            # tool_use_id — это и есть узел "родитель" для последующей
            # причинной связи, когда где-то в будущем запросе придёт
            # соответствующий tool_result (см. do_POST/extract_tool_results
            # и событие "tool_result" ниже).
            _register_tool_use(session_id, tool_use_id, req_id, tool_name=name)
            traced_input = input_data
            if config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS > 0:
                serialized = json.dumps(input_data, ensure_ascii=False, default=str)
                if len(serialized) > config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS:
                    traced_input = _cap(serialized, config.ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS)
            tool_use_summaries.append(
                {
                    "id": tool_use_id,
                    "name": name,
                    # Полные аргументы вызова, не только имя — без них нельзя
                    # отличить содержательно разные вызовы одного инструмента
                    # (см. пример с двумя разными "ls" из параллельных веток).
                    # Секреты вычищаются позже, при записи всей trace-строки
                    # (см. _trace -> redact(line)).
                    "input": traced_input,
                }
            )

    if not content:
        content = [{"type": "text", "text": " "}]

    stop_reason = finish_reason
    if finish_reason == "tool_calls":
        stop_reason = "tool_use"
    elif finish_reason not in ("stop", "length"):
        stop_reason = "end_turn"

    # Маппинг finish_reason -> stop_reason — детерминированная функция
    # одного значения в другое, а не решение/ветвление; раньше под неё
    # заводилось отдельное событие "harness_branch". Теперь это просто два
    # поля внутри response_content, где и так уже есть остальной результат
    # этого же ответа модели.
    _trace(
        session_id,
        req_id,
        "response_content",
        text_len=len(text),
        tool_uses=tool_use_summaries,
        finish_reason_raw=finish_reason,
        stop_reason_mapped=stop_reason,
        reasoning_present=bool(reasoning.strip()),
        reasoning_len=len(reasoning),
        # Полный reasoning, а не reasoning[:500] — обрезка убивала как
        # раз ту часть рассуждения, где объясняется выбор ветки/тула.
        reasoning=_cap(reasoning, config.ADAPTER_TRACE_REASONING_MAX_CHARS),
    )

    return {
        "id": f"msg_{o.get('id', 'local')}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
        "stop_reason": stop_reason,
    }
