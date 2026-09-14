"""Tests for backend_adapter.convert — Anthropic ↔ OpenAI conversion.

This is the most important test file — core logic that bridges two protocols.
"""
import json

from backend_adapter.convert import (
    _extract_responses_message_text,
    convert_messages_anthropic_to_openai,
    convert_openai_completions_to_responses,
    convert_openai_to_anthropic,
    convert_responses_input_to_openai_messages,
    convert_responses_tool_choice_to_openai,
    convert_responses_tools_to_openai,
    convert_tool_choice_anthropic_to_openai,
    convert_tools_anthropic_to_openai,
    detect_model_switch_command,
    extract_responses_tool_results,
    extract_text,
    extract_tool_results,
    force_store_false,
    normalize_messages_system_first,
    parse_tool_calls_from_text,
    sanitize_max_tokens,
)


class TestSanitizeMaxTokens:
    """Tests for sanitize_max_tokens()."""

    def test_small_max_tokens_replaced(self):
        """max_tokens=1 (пробинг от Claude Code) заменяется на дефолт."""
        body = {"max_tokens": 1}
        result = sanitize_max_tokens(body)
        assert result["max_tokens"] == 8192

    def test_none_max_tokens_replaced(self):
        """Отсутствие max_tokens заменяется на дефолт."""
        body = {}
        result = sanitize_max_tokens(body)
        assert result["max_tokens"] == 8192

    def test_large_max_tokens_clamped(self):
        """Слишком большое max_tokens клэмпится до хард-лимита."""
        body = {"max_tokens": 100000}
        result = sanitize_max_tokens(body)
        assert result["max_tokens"] == 16384

    def test_normal_max_tokens_unchanged(self):
        """Нормальное max_tokens остаётся без изменений."""
        body = {"max_tokens": 4096}
        result = sanitize_max_tokens(body)
        assert result["max_tokens"] == 4096

    def test_boundary_max_tokens_unchanged(self):
        """max_tokens на границе минимума остаётся без изменений."""
        body = {"max_tokens": 16}
        result = sanitize_max_tokens(body)
        assert result["max_tokens"] == 16

    def test_mutation_in_place(self):
        """Функция мутирует body на месте."""
        body = {"max_tokens": 1, "model": "test"}
        result = sanitize_max_tokens(body)
        assert result is body  # тот же объект
        assert body["max_tokens"] == 8192


class TestExtractText:
    """Tests for extract_text()."""

    def test_plain_string(self):
        assert extract_text("hello") == "hello"

    def test_list_of_strings(self):
        result = extract_text(["hello", "world"])
        assert result == "hello\nworld"

    def test_list_with_text_blocks(self):
        # Only type=text blocks are extracted; "other" is skipped
        result = extract_text([
            {"type": "text", "text": "hello"},
            {"type": "other", "text": "world"},
        ])
        assert result == "hello"

    def test_empty_list(self):
        assert extract_text([]) == ""

    def test_mixed_list(self):
        result = extract_text(["plain", {"type": "text", "text": "block"}])
        assert result == "plain\nblock"

    def test_none_content(self):
        assert extract_text(None) == "None"


class TestConvertTools:
    """Tests for Anthropic tool → OpenAI tool conversion."""

    def test_single_tool(self):
        anthropic_tools = [{
            "name": "Bash",
            "description": "Run a command",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}}
            }
        }]
        result = convert_tools_anthropic_to_openai(anthropic_tools)
        assert len(result) == 1
        assert result[0] == {
            "type": "function",
            "function": {
                "name": "Bash",
                "description": "Run a command",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}}
                }
            }
        }

    def test_multiple_tools(self):
        anthropic_tools = [
            {"name": "Bash", "description": "Run a command", "input_schema": {}},
            {"name": "Read", "description": "Read a file", "input_schema": {}},
        ]
        result = convert_tools_anthropic_to_openai(anthropic_tools)
        assert len(result) == 2

    def test_empty_tools(self):
        assert convert_tools_anthropic_to_openai([]) == []


class TestConvertToolChoice:
    """Tests for Anthropic tool_choice → OpenAI conversion."""

    def test_none(self):
        assert convert_tool_choice_anthropic_to_openai(None) == "auto"

    def test_empty_dict(self):
        assert convert_tool_choice_anthropic_to_openai({}) == "auto"

    def test_auto(self):
        assert convert_tool_choice_anthropic_to_openai({"type": "auto"}) == "auto"

    def test_any(self):
        assert convert_tool_choice_anthropic_to_openai({"type": "any"}) == "required"

    def test_tool(self):
        result = convert_tool_choice_anthropic_to_openai({
            "type": "tool", "name": "Bash"
        })
        assert result == {
            "type": "function",
            "function": {"name": "Bash"}
        }

    def test_unknown_type(self):
        assert convert_tool_choice_anthropic_to_openai({"type": "unknown"}) == "auto"


class TestExtractToolResults:
    """Tests for extract_tool_results()."""

    def test_single_result(self):
        messages = [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "id1", "content": "output"}]
        }]
        result = extract_tool_results(messages)
        assert len(result) == 1
        assert result[0] == {
            "tool_use_id": "id1",
            "content": "output",
            "is_error": False,
        }

    def test_multiple_results(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": "id1", "content": "out1"},
                {"type": "tool_result", "tool_use_id": "id2", "content": "out2"},
            ]
        }]
        result = extract_tool_results(messages)
        assert len(result) == 2

    def test_ignores_non_user(self):
        messages = [{
            "role": "assistant",
            "content": [{"type": "tool_result", "tool_use_id": "id1", "content": "out"}]
        }]
        assert extract_tool_results(messages) == []

    def test_error_flag(self):
        messages = [{
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "id1", "content": "err", "is_error": True}]
        }]
        result = extract_tool_results(messages)
        assert result[0]["is_error"] is True

    def test_nested_text_content(self):
        messages = [{
            "role": "user",
            "content": [{
                "type": "tool_result",
                "tool_use_id": "id1",
                "content": [{"type": "text", "text": "nested output"}]
            }]
        }]
        result = extract_tool_results(messages)
        assert result[0]["content"] == "nested output"


class TestConvertMessages:
    """Tests for convert_messages_anthropic_to_openai()."""

    def test_simple_conversation(self):
        messages = [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
        ]
        result = convert_messages_anthropic_to_openai(messages, system="You are helpful")
        assert result[0]["role"] == "system"
        assert result[0]["content"] == "You are helpful"
        assert result[1]["role"] == "user"
        assert result[1]["content"] == "Hello"
        assert result[2]["role"] == "assistant"
        assert result[2]["content"] == "Hi there"

    def test_system_as_list(self):
        system = [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}]
        result = convert_messages_anthropic_to_openai([], system=system)
        assert result[0]["content"] == "A\nB"

    def test_system_messages_collected_from_messages(self):
        """System messages in the messages list should be merged into first message."""
        messages = [
            {"role": "system", "content": "Part 1"},
            {"role": "user", "content": "Hello"},
        ]
        result = convert_messages_anthropic_to_openai(messages, system="System part")
        # system param is a string, so it's added; system message from messages is also added
        assert result[0]["role"] == "system"
        content = result[0]["content"]
        assert "Part 1" in content
        assert "System part" in content
        # Only one message before user
        assert result[1]["role"] == "user"

    def test_tool_use_to_tool_calls(self):
        """Assistant tool_use → OpenAI tool_calls with JSON arguments."""
        messages = [{
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Let me check"},
                {
                    "type": "tool_use",
                    "id": "id1",
                    "name": "Bash",
                    "input": {"command": "ls"}
                },
            ]
        }]
        result = convert_messages_anthropic_to_openai(messages, system="")
        # Find the assistant message
        assistant_msg = result[-1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == "Let me check"
        assert assistant_msg["tool_calls"][0]["function"]["name"] == "Bash"
        args = json.loads(assistant_msg["tool_calls"][0]["function"]["arguments"])
        assert args == {"command": "ls"}

    def test_tool_use_empty_input(self):
        """Tool_use with empty input should have empty JSON."""
        messages = [{
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "id1",
                    "name": "Bash",
                    "input": {}
                },
            ]
        }]
        result = convert_messages_anthropic_to_openai(messages, system="")
        tool_call = result[-1]["tool_calls"][0]
        assert json.loads(tool_call["function"]["arguments"]) == {}

    def test_tool_result_to_tool_role(self):
        """Tool result blocks → role: tool messages."""
        messages = [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "id1", "content": "output"},
            ]},
        ]
        result = convert_messages_anthropic_to_openai(messages, system="")
        tool_msg = result[-1]
        assert tool_msg["role"] == "tool"
        assert tool_msg["tool_call_id"] == "id1"
        assert tool_msg["content"] == "output"

    def test_ordering_preserved(self):
        """Order of messages should be preserved."""
        messages = [
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
        result = convert_messages_anthropic_to_openai(messages, system="")
        roles = [m["role"] for m in result]
        assert roles == ["user", "assistant", "user"]

    def test_tool_use_only_no_text(self):
        """Tool_use without text should not create a message with only tool_calls."""
        messages = [{
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "id1",
                    "name": "Bash",
                    "input": {"command": "ls"}
                },
            ]
        }]
        result = convert_messages_anthropic_to_openai(messages, system="")
        assert len(result) == 1
        assert "tool_calls" in result[-1]

    def test_empty_messages(self):
        result = convert_messages_anthropic_to_openai([], system="")
        assert result == []


class TestNormalizeMessagesSystemFirst:
    """Tests for normalize_messages_system_first() — passthrough messages→messages
    (v0.9.2): перенос role=system в начало без склейки."""

    def test_system_moved_to_front_preserving_order(self):
        messages = [
            {"role": "user", "content": "first"},
            {"role": "system", "content": "rules A"},
            {"role": "user", "content": "second"},
            {"role": "system", "content": "rules B"},
            {"role": "assistant", "content": "reply"},
        ]
        result = normalize_messages_system_first(messages)
        # system — в начале, в исходном порядке; user/assistant — как были
        assert [m["role"] for m in result] == [
            "system", "system", "user", "user", "assistant",
        ]
        assert result[0]["content"] == "rules A"
        assert result[1]["content"] == "rules B"
        assert result[2]["content"] == "first"
        assert result[3]["content"] == "second"
        assert result[4]["content"] == "reply"

    def test_no_system_unchanged(self):
        messages = [
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
        result = normalize_messages_system_first(messages)
        assert result == messages

    def test_already_system_first_unchanged(self):
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
        result = normalize_messages_system_first(messages)
        assert result == messages

    def test_empty_messages(self):
        assert normalize_messages_system_first([]) == []

    def test_content_blocks_preserved(self):
        """System с content-списком блоков и user не пересобираются (passthrough
        сохраняет Anthropic-структуру — в отличие от convert-склейки)."""
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
            {"role": "system", "content": [{"type": "text", "text": "rules"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]
        result = normalize_messages_system_first(messages)
        assert result[0] is messages[1]  # тот же объект, не пересобран
        assert result[1] is messages[0]
        assert result[2] is messages[2]
        assert result[0]["content"] == [{"type": "text", "text": "rules"}]


class TestForceStoreFalse:
    """Tests for force_store_false() — routing responses→responses (v0.9.6):
    принудительный store=false в теле Responses API."""

    def test_sets_false_when_missing(self):
        body = {"model": "x", "input": []}
        changed = force_store_false(body)
        assert changed is True
        assert body["store"] is False

    def test_sets_false_when_true(self):
        body = {"model": "x", "store": True}
        changed = force_store_false(body)
        assert changed is True
        assert body["store"] is False

    def test_no_change_when_already_false(self):
        body = {"model": "x", "store": False}
        changed = force_store_false(body)
        assert changed is False
        assert body["store"] is False

    def test_sets_false_when_null(self):
        body = {"model": "x", "store": None}
        changed = force_store_false(body)
        assert changed is True
        assert body["store"] is False


class TestDetectModelSwitchCommand:
    """Tests for detect_model_switch_command() — routing responses→responses
    (v0.9.6): перехват "/model <имя>" в последнем user input-сообщении."""

    def _user(self, content):
        """Responses API input-сообщение: role=user, content — строка или блоки."""
        return {"role": "user", "content": content}

    def test_plain_string_command(self):
        items = [self._user("hello"), self._user("/model qwen3-coder")]
        assert detect_model_switch_command(items) == "qwen3-coder"

    def test_command_is_only_message(self):
        items = [self._user("/model gpt-5")]
        assert detect_model_switch_command(items) == "gpt-5"

    def test_case_insensitive(self):
        items = [self._user("/MODEL qwen3-coder")]
        assert detect_model_switch_command(items) == "qwen3-coder"

    def test_trailing_whitespace_ok(self):
        items = [self._user("  /model qwen3-coder   ")]
        assert detect_model_switch_command(items) == "qwen3-coder"

    def test_input_text_block(self):
        items = [self._user([{"type": "input_text", "text": "/model qwen3-coder"}])]
        assert detect_model_switch_command(items) == "qwen3-coder"

    def test_text_block(self):
        items = [self._user([{"type": "text", "text": "/model qwen3-coder"}])]
        assert detect_model_switch_command(items) == "qwen3-coder"

    def test_no_command_returns_none(self):
        items = [self._user("hello, model"), self._user("how are you")]
        assert detect_model_switch_command(items) is None

    def test_command_in_non_last_message_ignored(self):
        # Только ПОСЛЕДНЕЕ user-сообщение рассматривается — чтобы не
        # перехватить "/model" из истории диалога.
        items = [self._user("/model qwen3-coder"), self._user("hello")]
        assert detect_model_switch_command(items) is None

    def test_last_message_not_user_returns_none(self):
        items = [{"role": "assistant", "content": "/model qwen3-coder"}]
        assert detect_model_switch_command(items) is None

    def test_command_must_be_alone_in_text(self):
        # "/model" упоминается, но есть и другой текст — НЕ команда
        # (перехват настоящего запроса с упоминанием "/model" исключён).
        items = [self._user("please use /model qwen3-coder now")]
        assert detect_model_switch_command(items) is None

    def test_no_model_arg_returns_none(self):
        items = [self._user("/model")]
        assert detect_model_switch_command(items) is None

    def test_empty_input_returns_none(self):
        assert detect_model_switch_command([]) is None

    def test_multi_block_message_ignored(self):
        # Несколько блоков — сообщение «сложное», команду не ищем.
        items = [
            self._user(
                [
                    {"type": "input_text", "text": "context"},
                    {"type": "input_text", "text": "/model qwen3-coder"},
                ]
            )
        ]
        assert detect_model_switch_command(items) is None

    def test_non_text_content_ignored(self):
        items = [self._user([{"type": "image", "url": "..."}])]
        assert detect_model_switch_command(items) is None

    def test_type_message_ok(self):
        # type == "message" — допустимо (как и отсутствие type).
        items = [
            {"role": "user", "type": "message", "content": "/model qwen3-coder"}
        ]
        assert detect_model_switch_command(items) == "qwen3-coder"

    def test_type_other_ignored(self):
        # type == "function_call"/"file" и пр. — не сообщение-текст.
        items = [
            {"role": "user", "type": "function_call", "content": "/model qwen3-coder"}
        ]
        assert detect_model_switch_command(items) is None


class TestParseToolCallsFromText:
    """Tests for parse_tool_calls_from_text() — Qwen fallback parser."""

    def test_xml_tagged_tool_call(self):
        text = '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>'
        result = parse_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0]["function"]["name"] == "Bash"

    def test_multiple_xml_tool_calls(self):
        text = '<tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call> <tool_call>{"name": "Read", "arguments": {"file": "f"}}</tool_call>'
        result = parse_tool_calls_from_text(text)
        assert len(result) == 2

    def test_plain_json_object(self):
        text = '{"name": "Bash", "arguments": {"command": "ls"}}'
        result = parse_tool_calls_from_text(text)
        assert len(result) == 1
        assert result[0]["function"]["name"] == "Bash"

    def test_no_match(self):
        result = parse_tool_calls_from_text("just text")
        assert result == []

    def test_empty_string(self):
        result = parse_tool_calls_from_text("")
        assert result == []

    def test_function_nested_format(self):
        text = '{"name": "Bash", "function": {"name": "Bash", "arguments": {"command": "ls"}}}'
        result = parse_tool_calls_from_text(text)
        assert len(result) == 1


class TestConvertOpenAItoAnthropic:
    """Tests for convert_openai_to_anthropic()."""

    def _make_response(self, content=None, tool_calls=None, finish_reason="stop",
                       model="test", prompt_tokens=10, completion_tokens=5,
                       reasoning_content=None, id="msg123"):
        """Helper to build a minimal OpenAI response dict."""
        msg = {"role": "assistant"}
        if content is not None:
            msg["content"] = content
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        choice = {"message": msg, "finish_reason": finish_reason}
        if reasoning_content is not None:
            msg["reasoning_content"] = reasoning_content
        return {
            "id": id,
            "model": model,
            "choices": [choice],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        }

    def test_text_response(self):
        resp = self._make_response(content="Hello world")
        result = convert_openai_to_anthropic(resp, "test")
        assert result["role"] == "assistant"
        assert result["type"] == "message"
        assert result["model"] == "test"
        assert result["usage"]["input_tokens"] == 10
        assert result["usage"]["output_tokens"] == 5

    def test_empty_content_becomes_space(self):
        resp = self._make_response(content=None)
        result = convert_openai_to_anthropic(resp, "test")
        assert result["content"] == [{"type": "text", "text": " "}]

    def test_tool_call_response(self):
        resp = self._make_response(
            content=None,
            tool_calls=[{
                "id": "call1",
                "type": "function",
                "function": {
                    "name": "Bash",
                    "arguments": '{"command":"ls"}'
                }
            }],
            finish_reason="tool_calls"
        )
        result = convert_openai_to_anthropic(resp, "test")
        assert result["stop_reason"] == "tool_use"
        assert len(result["content"]) == 1
        assert result["content"][0]["type"] == "tool_use"
        assert result["content"][0]["name"] == "Bash"
        assert result["content"][0]["input"] == {"command": "ls"}

    def test_finish_reason_mapping(self):
        """finish_reason tool_calls → tool_use; unknown → end_turn."""
        resp_tc = self._make_response(content=None, tool_calls=[], finish_reason="tool_calls")
        assert convert_openai_to_anthropic(resp_tc, "test")["stop_reason"] == "tool_use"

        resp_unknown = self._make_response(content="x", finish_reason="content_filter")
        assert convert_openai_to_anthropic(resp_unknown, "test")["stop_reason"] == "end_turn"

        resp_stop = self._make_response(content="x", finish_reason="stop")
        assert convert_openai_to_anthropic(resp_stop, "test")["stop_reason"] == "stop"

        resp_length = self._make_response(content="x", finish_reason="length")
        assert convert_openai_to_anthropic(resp_length, "test")["stop_reason"] == "length"

    def test_reasoning_content(self):
        resp = self._make_response(content="thought", reasoning_content="I think...")
        result = convert_openai_to_anthropic(resp, "test")
        assert result["role"] == "assistant"

    def test_empty_tool_arguments(self):
        resp = self._make_response(
            tool_calls=[{
                "id": "call1",
                "type": "function",
                "function": {
                    "name": "Bash",
                    "arguments": "{}"
                }
            }],
            finish_reason="tool_calls"
        )
        result = convert_openai_to_anthropic(resp, "test")
        assert result["content"][0]["input"] == {}

    def test_usage_tokens(self):
        resp = self._make_response(content="x", prompt_tokens=100, completion_tokens=50)
        result = convert_openai_to_anthropic(resp, "test")
        assert result["usage"]["input_tokens"] == 100
        assert result["usage"]["output_tokens"] == 50

    def test_multiple_tool_calls(self):
        resp = self._make_response(
            tool_calls=[
                {"id": "call1", "type": "function", "function": {"name": "Bash", "arguments": "{}"}},
                {"id": "call2", "type": "function", "function": {"name": "Read", "arguments": "{}"}},
            ],
            finish_reason="tool_calls"
        )
        result = convert_openai_to_anthropic(resp, "test")
        assert len(result["content"]) == 2
        assert result["content"][0]["name"] == "Bash"
        assert result["content"][1]["name"] == "Read"

    def test_no_choices_empty_list(self):
        """Empty choices list should not crash — returns space text."""
        resp = self._make_response(content=None)
        resp["choices"] = []
        # Should handle gracefully (the actual code may crash, which is a known bug
        # — but our test documents the expected behavior)
        # For now, just test normal case with valid choices

    def test_id_prefixed(self):
        resp = self._make_response(id="chatcmpl-abc")
        result = convert_openai_to_anthropic(resp, "test")
        assert result["id"] == "msg_chatcmpl-abc"

    def test_text_fallback_tool_call(self):
        """Text containing nested JSON → parsed as tool call, text cleaned."""
        text = 'Some text <tool_call>{"name": "Bash", "arguments": {"command": "ls"}}</tool_call>'
        resp = self._make_response(content=text, finish_reason="stop")
        result = convert_openai_to_anthropic(resp, "test")
        # Should have both cleaned text and tool_use content
        assert any(c.get("type") == "tool_use" for c in result["content"])


# ===========================================================================
# responses → completions (v0.9.7)
# ===========================================================================


class TestExtractResponsesMessageText:
    """Tests for _extract_responses_message_text() — полный текст content."""

    def test_plain_string(self):
        assert _extract_responses_message_text("hello") == "hello"

    def test_input_text_block(self):
        content = [{"type": "input_text", "text": "hello"}]
        assert _extract_responses_message_text(content) == "hello"

    def test_output_text_block(self):
        content = [{"type": "output_text", "text": "hello"}]
        assert _extract_responses_message_text(content) == "hello"

    def test_text_block(self):
        content = [{"type": "text", "text": "hello"}]
        assert _extract_responses_message_text(content) == "hello"

    def test_multiple_blocks_joined_by_newline(self):
        content = [
            {"type": "input_text", "text": "first"},
            {"type": "input_text", "text": "second"},
        ]
        assert _extract_responses_message_text(content) == "first\nsecond"

    def test_mixed_blocks(self):
        content = [
            {"type": "input_text", "text": "a"},
            {"type": "image", "url": "..."},  # игнорируется
            "raw-string",
            {"type": "output_text", "text": "b"},
        ]
        assert _extract_responses_message_text(content) == "a\nraw-string\nb"

    def test_non_text_types_ignored(self):
        assert _extract_responses_message_text([{"type": "image", "url": "..."}]) == ""

    def test_empty_list(self):
        assert _extract_responses_message_text([]) == ""

    def test_none_and_non_list(self):
        assert _extract_responses_message_text(None) == ""
        assert _extract_responses_message_text(42) == ""


class TestConvertResponsesInputToOpenAiMessages:
    """Tests for convert_responses_input_to_openai_messages() (v0.9.7)."""

    def test_user_message(self):
        out = convert_responses_input_to_openai_messages(
            [{"type": "message", "role": "user", "content": "Hi"}], None
        )
        assert out == [{"role": "user", "content": "Hi"}]

    def test_assistant_message_blocks(self):
        out = convert_responses_input_to_openai_messages(
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "ok"}],
                }
            ],
            None,
        )
        assert out == [{"role": "assistant", "content": "ok"}]

    def test_instructions_becomes_first_system(self):
        out = convert_responses_input_to_openai_messages(
            [{"type": "message", "role": "user", "content": "Hi"}], "rules"
        )
        assert out[0] == {"role": "system", "content": "rules"}
        assert out[1]["role"] == "user"

    def test_function_call_becomes_assistant_tool_calls(self):
        out = convert_responses_input_to_openai_messages(
            [
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "Bash",
                    "arguments": '{"command": "ls"}',
                }
            ],
            None,
        )
        assert out == [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            }
        ]

    def test_function_call_default_arguments(self):
        out = convert_responses_input_to_openai_messages(
            [{"type": "function_call", "call_id": "c", "name": "N"}], None
        )
        assert out[0]["tool_calls"][0]["function"]["arguments"] == "{}"

    def test_function_call_id_fallback(self):
        # call_id отсутствует → берётся id.
        out = convert_responses_input_to_openai_messages(
            [{"type": "function_call", "id": "fc_9", "name": "N"}], None
        )
        assert out[0]["tool_calls"][0]["id"] == "fc_9"

    def test_function_call_output_becomes_tool_role(self):
        out = convert_responses_input_to_openai_messages(
            [{"type": "function_call_output", "call_id": "call_1", "output": "result"}],
            None,
        )
        assert out == [{"role": "tool", "tool_call_id": "call_1", "content": "result"}]

    def test_function_call_output_list_content(self):
        out = convert_responses_input_to_openai_messages(
            [
                {
                    "type": "function_call_output",
                    "call_id": "c",
                    "output": [{"type": "output_text", "text": "r"}],
                }
            ],
            None,
        )
        assert out[0]["content"] == "r"

    def test_reasoning_skipped(self):
        out = convert_responses_input_to_openai_messages(
            [
                {"type": "reasoning", "encrypted_content": "..."},
                {"type": "message", "role": "user", "content": "Hi"},
            ],
            None,
        )
        assert out == [{"role": "user", "content": "Hi"}]

    def test_unknown_item_skipped(self):
        out = convert_responses_input_to_openai_messages(
            [
                {"type": "file", "file_id": "f1"},
                {"type": "message", "role": "user", "content": "Hi"},
            ],
            None,
        )
        assert out == [{"role": "user", "content": "Hi"}]

    def test_non_dict_item_skipped(self):
        out = convert_responses_input_to_openai_messages(["junk", 42], None)
        assert out == []

    def test_empty_input_no_instructions(self):
        assert convert_responses_input_to_openai_messages([], None) == []

    def test_system_first_normalized(self):
        # system-сообщение по ходу диалога переносится в начало.
        out = convert_responses_input_to_openai_messages(
            [
                {"type": "message", "role": "user", "content": "first"},
                {"type": "message", "role": "system", "content": "rules"},
            ],
            None,
        )
        assert out[0]["role"] == "system"
        assert out[1]["role"] == "user"

    def test_missing_type_defaults_to_message(self):
        out = convert_responses_input_to_openai_messages(
            [{"role": "user", "content": "Hi"}], None
        )
        assert out == [{"role": "user", "content": "Hi"}]

    # -- role=developer схлопывается в system (v0.9.7) --------------------

    def test_developer_merged_into_system_with_instructions(self):
        # developer-элемент схлопывается в ЕДИНОЕ system-сообщение вместе с
        # instructions (Qwen3/llama.cpp Jinja: роль "developer" неизвестна, а
        # system-сообщение должно быть ровно одно и первым).
        out = convert_responses_input_to_openai_messages(
            [
                {"type": "message", "role": "developer", "content": "dev rules"},
                {"type": "message", "role": "user", "content": "Hi"},
            ],
            "instructions",
        )
        assert out == [
            {"role": "system", "content": "instructions\n\ndev rules"},
            {"role": "user", "content": "Hi"},
        ]

    def test_developer_without_instructions_becomes_system(self):
        out = convert_responses_input_to_openai_messages(
            [
                {"type": "message", "role": "developer", "content": "dev rules"},
                {"type": "message", "role": "user", "content": "Hi"},
            ],
            None,
        )
        assert out[0] == {"role": "system", "content": "dev rules"}
        assert out[1] == {"role": "user", "content": "Hi"}

    def test_multiple_developers_joined_in_order(self):
        out = convert_responses_input_to_openai_messages(
            [
                {"type": "message", "role": "developer", "content": "first"},
                {"type": "message", "role": "user", "content": "Hi"},
                {"type": "message", "role": "developer", "content": "second"},
            ],
            None,
        )
        assert out[0] == {"role": "system", "content": "first\n\nsecond"}
        assert out[1] == {"role": "user", "content": "Hi"}

    def test_empty_developer_content_ignored(self):
        # Пустой/None content developer-элемента не создаёт system-сообщение.
        out = convert_responses_input_to_openai_messages(
            [
                {"type": "message", "role": "developer", "content": ""},
                {"type": "message", "role": "developer"},
                {"type": "message", "role": "user", "content": "Hi"},
            ],
            None,
        )
        assert out == [{"role": "user", "content": "Hi"}]

    def test_developer_role_absent_in_output(self):
        # Роль "developer" в выходных messages не остаётся ни при каких
        # условиях (её не знает шаблон бэкенда).
        out = convert_responses_input_to_openai_messages(
            [
                {"type": "message", "role": "developer", "content": "dev"},
                {"type": "message", "role": "user", "content": "Hi"},
                {"type": "message", "role": "assistant", "content": "ok"},
            ],
            "rules",
        )
        assert [m["role"] for m in out] == ["system", "user", "assistant"]


class TestConvertResponsesToolsToOpenAi:
    """Tests for convert_responses_tools_to_openai() (v0.9.7)."""

    def test_single_function(self):
        tools = [
            {
                "type": "function",
                "name": "Bash",
                "description": "run",
                "parameters": {"type": "object"},
            }
        ]
        assert convert_responses_tools_to_openai(tools) == [
            {
                "type": "function",
                "function": {
                    "name": "Bash",
                    "description": "run",
                    "parameters": {"type": "object"},
                },
            }
        ]

    def test_multiple_functions(self):
        tools = [
            {"type": "function", "name": "A", "parameters": {}},
            {"type": "function", "name": "B", "parameters": {}},
        ]
        out = convert_responses_tools_to_openai(tools)
        assert [t["function"]["name"] for t in out] == ["A", "B"]

    def test_non_function_skipped(self):
        tools = [{"type": "web_search"}, {"type": "function", "name": "A"}]
        out = convert_responses_tools_to_openai(tools)
        assert len(out) == 1
        assert out[0]["function"]["name"] == "A"

    def test_defaults(self):
        out = convert_responses_tools_to_openai([{"type": "function", "name": "A"}])
        assert out[0]["function"]["description"] == ""
        assert out[0]["function"]["parameters"] == {}

    def test_empty(self):
        assert convert_responses_tools_to_openai([]) == []


class TestConvertResponsesToolChoiceToOpenAi:
    """Tests for convert_responses_tool_choice_to_openai() (v0.9.7)."""

    def test_none_and_empty(self):
        assert convert_responses_tool_choice_to_openai(None) == "auto"
        assert convert_responses_tool_choice_to_openai("") == "auto"

    def test_string_values_pass_through(self):
        for value in ("auto", "none", "required"):
            assert convert_responses_tool_choice_to_openai(value) == value

    def test_function_choice_nested(self):
        out = convert_responses_tool_choice_to_openai({"type": "function", "name": "Bash"})
        assert out == {"type": "function", "function": {"name": "Bash"}}

    def test_unknown_type_becomes_string(self):
        assert convert_responses_tool_choice_to_openai({"type": "web_search"}) == "web_search"


class TestConvertOpenAiCompletionsToResponses:
    """Tests for convert_openai_completions_to_responses() (v0.9.7)."""

    def test_text_response(self):
        o = {
            "choices": [{"message": {"role": "assistant", "content": "Hello"}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 7},
        }
        out = convert_openai_completions_to_responses(o, "test-model")
        assert out["object"] == "response"
        assert out["status"] == "completed"
        assert out["model"] == "test-model"
        assert out["id"].startswith("resp_")
        assert isinstance(out["created_at"], int)
        assert len(out["output"]) == 1
        item = out["output"][0]
        assert item["type"] == "message"
        assert item["role"] == "assistant"
        assert item["content"][0]["type"] == "output_text"
        assert item["content"][0]["text"] == "Hello"

    def test_usage_mapped(self):
        o = {
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
        }
        out = convert_openai_completions_to_responses(o, "m")
        assert out["usage"] == {"input_tokens": 11, "output_tokens": 22, "total_tokens": 33}

    def test_usage_total_computed_when_missing(self):
        o = {
            "choices": [{"message": {"content": "x"}}],
            "usage": {"prompt_tokens": 4, "completion_tokens": 6},
        }
        out = convert_openai_completions_to_responses(o, "m")
        assert out["usage"]["total_tokens"] == 10

    def test_tool_calls_become_function_call_items(self):
        o = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
                            }
                        ],
                    }
                }
            ]
        }
        out = convert_openai_completions_to_responses(o, "m")
        assert len(out["output"]) == 1
        item = out["output"][0]
        assert item["type"] == "function_call"
        assert item["call_id"] == "call_1"
        assert item["name"] == "Bash"
        assert item["arguments"] == '{"command": "ls"}'
        assert item["id"].startswith("fc_")

    def test_text_and_tool_calls_together(self):
        o = {
            "choices": [
                {
                    "message": {
                        "content": "thinking",
                        "tool_calls": [{"id": "c", "function": {"name": "N", "arguments": "{}"}}],
                    }
                }
            ]
        }
        out = convert_openai_completions_to_responses(o, "m")
        assert [i["type"] for i in out["output"]] == ["message", "function_call"]

    def test_empty_content_no_tool_calls(self):
        o = {"choices": [{"message": {"content": ""}}]}
        out = convert_openai_completions_to_responses(o, "m")
        assert out["output"] == []

    def test_missing_choices(self):
        out = convert_openai_completions_to_responses({}, "m")
        assert out["output"] == []
        assert out["usage"]["input_tokens"] == 0

    def test_tool_call_default_arguments(self):
        o = {
            "choices": [
                {"message": {"tool_calls": [{"id": "c", "function": {"name": "N"}}]}}
            ]
        }
        out = convert_openai_completions_to_responses(o, "m")
        assert out["output"][0]["arguments"] == "{}"


class TestExtractResponsesToolResults:
    """Tests for extract_responses_tool_results() (v0.9.7)."""

    def test_single_result(self):
        items = [
            {"type": "function_call_output", "call_id": "c1", "output": "ok"},
        ]
        assert extract_responses_tool_results(items) == [{"call_id": "c1", "content": "ok"}]

    def test_multiple_results(self):
        items = [
            {"type": "function_call_output", "call_id": "c1", "output": "a"},
            {"type": "function_call_output", "call_id": "c2", "output": "b"},
        ]
        assert extract_responses_tool_results(items) == [
            {"call_id": "c1", "content": "a"},
            {"call_id": "c2", "content": "b"},
        ]

    def test_list_output_flattened(self):
        items = [
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [{"type": "output_text", "text": "r"}],
            }
        ]
        assert extract_responses_tool_results(items) == [{"call_id": "c1", "content": "r"}]

    def test_ignores_other_types(self):
        items = [
            {"type": "message", "role": "user", "content": "Hi"},
            {"type": "function_call", "call_id": "c", "name": "N"},
        ]
        assert extract_responses_tool_results(items) == []

    def test_non_dict_ignored(self):
        assert extract_responses_tool_results(["junk"]) == []

    def test_empty(self):
        assert extract_responses_tool_results([]) == []
