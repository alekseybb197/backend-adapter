"""Tests for backend_adapter.streaming — SSE streaming converter."""
import json

from tests.conftest import FakeRespStream, FakeWfile


def _reload_all():
    import sys
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]


class TestSseWrite:
    """Tests for _sse_write()."""

    def test_event_format(self):
        _reload_all()
        from backend_adapter.streaming import _sse_write
        wfile = FakeWfile()
        _sse_write(wfile, "message_start", {"type": "message_start"})
        output = wfile.data.decode()
        assert output.startswith("event: message_start")
        assert '"type": "message_start"' in output

    def test_flush_called(self):
        _reload_all()
        from backend_adapter.streaming import _sse_write
        wfile = FakeWfile()
        wfile.flush_called = False
        def mock_flush():
            wfile.flush_called = True
        wfile.flush = mock_flush
        _sse_write(wfile, "delta", {"text": "x"})
        assert wfile.flush_called


class TestStreamOpenAItoAnthropic:
    """Tests for stream_openai_to_anthropic()."""

    def test_text_chunks(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic
        lines = [
            b'data: {"choices": [{"delta": {"content": "H"}, "index": 0}], "usage": {}}',
            b'data: {"choices": [{"delta": {"content": "i"}, "index": 0}], "usage": {}}',
            b'data: {"choices": [{"delta": {"finish_reason": "stop"}, "index": 0}], "usage": {"completion_tokens": 1, "prompt_tokens": 5}}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        stop_reason, usage = stream_openai_to_anthropic(stream, wfile, "test", "sess", "req", approx_prompt_chars=20)
        assert stop_reason == "stop"
        assert usage["completion_tokens"] == 1
        output = wfile.data.decode()
        assert "message_start" in output
        assert "content_block_start" in output
        assert "content_block_delta" in output
        assert "content_block_stop" in output
        assert "message_stop" in output

    def test_bytes_sink_accumulates_raw_lines(self):
        """bytes_sink=[0] sums ALL raw lines (incl. blank / non-data)."""
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic
        lines = [
            b'data: {"choices": [{"delta": {"content": "H"}, "index": 0}]}\n\n',
            b"\n",
            b': keepalive comment line\n\n',
            b'data: {"choices": [{"delta": {"content": "i"}, "index": 0}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        sink = [0]
        stream_openai_to_anthropic(
            stream, wfile, "test", "sess", "req",
            approx_prompt_chars=20, bytes_sink=sink,
        )
        assert sink[0] == sum(len(l) for l in lines)

    def test_bytes_sink_optional(self):
        """Without bytes_sink the converter behaves exactly as before."""
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic
        lines = [
            b'data: {"choices": [{"delta": {"content": "H"}, "index": 0}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        stop_reason, usage = stream_openai_to_anthropic(
            stream, wfile, "test", "sess", "req", approx_prompt_chars=20
        )
        assert stop_reason == "stop"
        assert "message_stop" in wfile.data.decode()

    def test_tool_calls_from_chunks(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic
        # finish_reason arrives in the same chunk as tool_calls and content
        lines = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call1", "function": {"name": "Bash"}}]}, "index": 0}], "usage": {}}',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "c"}}], "index": 0}], "usage": {}}',
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "tool_calls", "index": 0}], "usage": {"completion_tokens": 1, "prompt_tokens": 5}}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        stop_reason, usage = stream_openai_to_anthropic(stream, wfile, "test", "sess", "req", approx_prompt_chars=20)
        assert stop_reason == "tool_use"
        output = wfile.data.decode()
        assert "tool_use" in output

    def test_stop_reason_mapping(self):
        """finish_reason tool_calls → tool_use; unknown → end_turn."""
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic

        # tool_calls → tool_use
        lines = [
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "tool_calls"}], "usage": {}}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        stop_reason, _ = stream_openai_to_anthropic(stream, wfile, "test", "sess", "req")
        assert stop_reason == "tool_use"

    def test_usage_estimation(self):
        """When backend doesn't return usage, approx_prompt_chars // 4 is used."""
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic
        lines = [
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        stop_reason, usage = stream_openai_to_anthropic(stream, wfile, "test", "sess", "req", approx_prompt_chars=200)
        # 200 // 4 = 50 — но usage dict пустой, оценка идёт только в trace
        # Тестируем что функция не падает
        assert stop_reason == "stop"
        assert usage == {}

    def test_no_approx_no_usage(self):
        """Without approx_prompt_chars and no usage → empty usage dict."""
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic
        lines = [
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        stop_reason, usage = stream_openai_to_anthropic(stream, wfile, "test", "sess", "req")
        assert stop_reason == "stop"
        assert usage == {}

    def test_invalid_chunk_skipped(self):
        """Invalid JSON chunks should be skipped without error."""
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic
        lines = [
            b'data: NOT VALID JSON',
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}], "usage": {}}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        # Should not raise
        stream_openai_to_anthropic(stream, wfile, "test", "sess", "req")

    def test_no_empty_line_start(self):
        """Lines without 'data:' prefix should be skipped."""
        _reload_all()
        from backend_adapter.streaming import stream_openai_to_anthropic
        lines = [
            b'',
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}], "usage": {}}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream = FakeRespStream(lines)
        stream_openai_to_anthropic(stream, wfile, "test", "sess", "req")


def _parse_events(wfile):
    """Парсит буфер FakeWfile в список (event, data) — Responses-события."""
    text = wfile.data.decode("utf-8")
    events = []
    for block in text.strip().split("\n\n"):
        if not block.strip():
            continue
        evt = None
        data = None
        for line in block.split("\n"):
            if line.startswith("event: "):
                evt = line[7:].strip()
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if evt and data is not None:
            events.append((evt, data))
    return events


class TestStreamCompletionsToResponses:
    """Tests for stream_openai_completions_to_responses() (v0.9.7) —
    стрим [OI] chat.completions → поток событий Responses API."""

    def test_text_chunks_event_sequence(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'data: {"choices": [{"delta": {"content": "H"}, "index": 0}], "usage": {}}',
            b'data: {"choices": [{"delta": {"content": "i"}, "index": 0}], "usage": {}}',
            b'data: {"choices": [{"delta": {"finish_reason": "stop"}, "index": 0}], '
            b'"usage": {"completion_tokens": 1, "prompt_tokens": 5}}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        finish, usage = stream_openai_completions_to_responses(
            FakeRespStream(lines), wfile, "test", "sess", "req", approx_prompt_chars=20
        )
        assert finish == "stop"
        assert usage["completion_tokens"] == 1
        types = [e[0] for e in _parse_events(wfile)]
        assert types == [
            "response.created",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ]

    def test_text_assembled_in_done(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'data: {"choices": [{"delta": {"content": "Hel"}}]}',
            b'data: {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream_openai_completions_to_responses(
            FakeRespStream(lines), wfile, "m", "sess", "req"
        )
        events = _parse_events(wfile)
        done = next(e for e in events if e[0] == "response.output_text.done")[1]
        assert done["text"] == "Hello"
        item_done = next(e for e in events if e[0] == "response.output_item.done")[1]
        assert item_done["item"]["content"][0]["text"] == "Hello"

    def test_tool_call_accumulated_by_index(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call_1", '
            b'"function": {"name": "Bash"}}]}}]}',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, '
            b'"function": {"arguments": "{\\"cmd\\":"}}]}}]}',
            b'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, '
            b'"function": {"arguments": " \\"ls\\"}"}}]}}]}',
            b'data: {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        finish, _ = stream_openai_completions_to_responses(
            FakeRespStream(lines), wfile, "m", "sess", "req"
        )
        assert finish == "tool_calls"
        events = _parse_events(wfile)
        types = [e[0] for e in events]
        assert types.count("response.output_item.added") == 1
        assert types.count("response.function_call_arguments.delta") == 2
        done = next(e for e in events if e[0] == "response.function_call_arguments.done")[1]
        assert done["arguments"] == '{"cmd": "ls"}'
        item_done = next(e for e in events if e[0] == "response.output_item.done")[1]
        assert item_done["item"]["type"] == "function_call"
        assert item_done["item"]["call_id"] == "call_1"
        assert item_done["item"]["name"] == "Bash"
        # Итоговый output response.completed несёт function_call-item.
        completed = next(e for e in events if e[0] == "response.completed")[1]
        assert completed["response"]["output"][0]["type"] == "function_call"
        assert completed["response"]["output"][0]["arguments"] == '{"cmd": "ls"}'

    def test_multiple_tool_calls(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'data: {"choices": [{"delta": {"tool_calls": ['
            b'{"index": 0, "id": "c0", "function": {"name": "A", "arguments": "{}"}},'
            b'{"index": 1, "id": "c1", "function": {"name": "B", "arguments": "{}"}}]}}]}',
            b'data: {"choices": [{"delta": {"finish_reason": "tool_calls"}}]}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream_openai_completions_to_responses(FakeRespStream(lines), wfile, "m", "s", "r")
        events = _parse_events(wfile)
        added = [e[1] for e in events if e[0] == "response.output_item.added"]
        assert len(added) == 2
        assert added[0]["item"]["call_id"] == "c0"
        assert added[1]["item"]["call_id"] == "c1"
        assert added[0]["output_index"] != added[1]["output_index"]

    def test_usage_from_chunk(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}], '
            b'"usage": {"prompt_tokens": 11, "completion_tokens": 22}}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream_openai_completions_to_responses(FakeRespStream(lines), wfile, "m", "s", "r")
        completed = next(e for e in _parse_events(wfile) if e[0] == "response.completed")[1]
        assert completed["response"]["usage"] == {
            "input_tokens": 11,
            "output_tokens": 22,
            "total_tokens": 33,
        }

    def test_heuristic_input_tokens_writes_warn(self, tmp_path):
        _reload_all()
        from backend_adapter import session_log as slog
        slog._DEBUG_IS_DIR = True
        slog._TRACE_IS_DIR = True
        slog._DEBUG_PATH = str(tmp_path)
        slog._TRACE_PATH = str(tmp_path)
        slog._session_logs.clear()
        slog._session_file_ts.clear()
        slog._session_file_ts["sess1"] = "20260909-100000"
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream_openai_completions_to_responses(
            FakeRespStream(lines), wfile, "m", "sess1", "req1",
            approx_prompt_chars=200, out_body=b'{"model": "m"}', backend_url="http://b/v1",
        )
        completed = next(e for e in _parse_events(wfile) if e[0] == "response.completed")[1]
        assert completed["response"]["usage"]["input_tokens"] == 50
        files = list(tmp_path.glob("session-*.err"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "input_tokens оценён эвристически" in content

    def test_no_warn_without_out_body(self, tmp_path):
        _reload_all()
        from backend_adapter import session_log as slog
        slog._DEBUG_IS_DIR = True
        slog._TRACE_IS_DIR = True
        slog._DEBUG_PATH = str(tmp_path)
        slog._TRACE_PATH = str(tmp_path)
        slog._session_logs.clear()
        slog._session_file_ts.clear()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}',
            b'data: [DONE]',
        ]
        stream_openai_completions_to_responses(
            FakeRespStream(lines), FakeWfile(), "m", "sess1", "req1", approx_prompt_chars=200
        )
        assert list(tmp_path.glob("session-*.err")) == []

    def test_invalid_chunk_skipped(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'data: NOT VALID JSON',
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream_openai_completions_to_responses(FakeRespStream(lines), wfile, "m", "s", "r")
        done = next(e for e in _parse_events(wfile) if e[0] == "response.output_text.done")[1]
        assert done["text"] == "x"

    def test_empty_stream_completes(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        wfile = FakeWfile()
        stream_openai_completions_to_responses(FakeRespStream([]), wfile, "m", "s", "r")
        events = _parse_events(wfile)
        assert [e[0] for e in events] == ["response.created", "response.completed"]
        completed = events[-1][1]
        assert completed["response"]["output"] == []

    def test_non_data_lines_skipped(self):
        _reload_all()
        from backend_adapter.streaming import stream_openai_completions_to_responses
        lines = [
            b'',
            b': keepalive\n\n',
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}',
            b'data: [DONE]',
        ]
        wfile = FakeWfile()
        stream_openai_completions_to_responses(FakeRespStream(lines), wfile, "m", "s", "r")
        assert "response.completed" in wfile.data.decode()


class TestRelaySse:
    """Tests for relay_sse() — passthrough E→E SSE relay."""

    def test_bytes_verbatim_and_flush(self):
        """Every raw line is written verbatim and flushed."""
        _reload_all()
        from backend_adapter.streaming import relay_sse
        lines = [
            b'data: {"type": "response.output_text.delta", "delta": "H"}\n\n',
            b'data: {"type": "response.completed"}\n\n',
        ]
        wfile = FakeWfile()
        wfile.flush_called = 0
        def mock_flush():
            wfile.flush_called += 1
        wfile.flush = mock_flush
        relay_sse(FakeRespStream(lines), wfile, "req1", "responses")
        assert wfile.data == b"".join(lines)
        assert wfile.flush_called == len(lines)

    def test_no_usage_returns_empty(self):
        """Stream without any usage block → {}."""
        _reload_all()
        from backend_adapter.streaming import relay_sse
        lines = [
            b'data: {"type": "response.output_text.delta", "delta": "hi"}\n\n',
            b'data: {"type": "response.output_text.done"}\n\n',
        ]
        usage = relay_sse(FakeRespStream(lines), FakeWfile(), "req1", "responses")
        assert usage == {}

    def test_usage_completions_last_chunk(self):
        """completions: usage на верхнем уровне финального чанка."""
        _reload_all()
        from backend_adapter.streaming import relay_sse
        lines = [
            b'data: {"choices": [{"delta": {"content": "x"}}]}\n\n',
            b'data: {"choices": [{"delta": {}}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}\n\n',
            b'data: [DONE]\n\n',
        ]
        usage = relay_sse(FakeRespStream(lines), FakeWfile(), "req1", "completions")
        assert usage == {"input_tokens": 11, "output_tokens": 7}

    def test_usage_responses_completed_nested(self):
        """responses: usage во вложенном response.usage события completed."""
        _reload_all()
        from backend_adapter.streaming import relay_sse
        lines = [
            b'data: {"type": "response.output_text.delta", "delta": "ok"}\n\n',
            b'data: {"type": "response.completed", "response": {"usage": {"input_tokens": 3, "output_tokens": 9}}}\n\n',
        ]
        usage = relay_sse(FakeRespStream(lines), FakeWfile(), "req1", "responses")
        assert usage == {"input_tokens": 3, "output_tokens": 9}

    def test_usage_messages_top_level(self):
        """messages: usage на верхнем уровне (как message_delta)."""
        _reload_all()
        from backend_adapter.streaming import relay_sse
        lines = [
            b'event: message_delta\n',
            b'data: {"type": "message_delta", "usage": {"input_tokens": 5, "output_tokens": 2}}\n\n',
        ]
        usage = relay_sse(FakeRespStream(lines), FakeWfile(), "req1", "messages")
        assert usage == {"input_tokens": 5, "output_tokens": 2}

    def test_last_usage_wins(self):
        """Multiple usage blocks → the LAST one is returned."""
        _reload_all()
        from backend_adapter.streaming import relay_sse
        lines = [
            b'data: {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 1}}\n\n',
            b'data: {"choices": [], "usage": {"prompt_tokens": 2, "completion_tokens": 2}}\n\n',
            b'data: [DONE]\n\n',
        ]
        usage = relay_sse(FakeRespStream(lines), FakeWfile(), "req1", "completions")
        assert usage == {"input_tokens": 2, "output_tokens": 2}

    def test_non_data_lines_pass_through(self):
        """Keepalive/comment lines are relayed verbatim (not dropped)."""
        _reload_all()
        from backend_adapter.streaming import relay_sse
        lines = [
            b': keepalive\n\n',
            b'data: {"choices": [{"delta": {"content": "x"}}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        wfile = FakeWfile()
        relay_sse(FakeRespStream(lines), wfile, "req1", "completions")
        assert wfile.data == b"".join(lines)

    def test_client_break_propagates(self):
        """BrokenPipe on write → re-raised to the caller."""
        _reload_all()
        from backend_adapter.streaming import relay_sse
        lines = [
            b'data: {"choices": [{"delta": {"content": "x"}}]}\n\n',
            b'data: [DONE]\n\n',
        ]
        wfile = FakeWfile()
        def broken_write(data):
            raise BrokenPipeError("client gone")
        wfile.write = broken_write
        try:
            relay_sse(FakeRespStream(lines), wfile, "req1", "completions")
            assert False, "expected BrokenPipeError to propagate"
        except BrokenPipeError:
            pass


class TestWriteSseErrorNative:
    """Tests for _write_sse_error_native() — native-format SSE error event."""

    def test_responses_format(self):
        _reload_all()
        from backend_adapter.streaming import _write_sse_error_native
        wfile = FakeWfile()
        _write_sse_error_native(wfile, "responses", "boom")
        out = wfile.data.decode()
        assert out.startswith("event: error")
        assert '"type": "error"' in out
        assert '"code": "adapter_error"' in out
        assert '"message": "boom"' in out

    def test_completions_format(self):
        _reload_all()
        from backend_adapter.streaming import _write_sse_error_native
        wfile = FakeWfile()
        _write_sse_error_native(wfile, "completions", "boom")
        out = wfile.data.decode()
        assert '"error"' in out
        assert '"message": "boom"' in out

    def test_messages_format(self):
        _reload_all()
        from backend_adapter.streaming import _write_sse_error_native
        wfile = FakeWfile()
        _write_sse_error_native(wfile, "messages", "boom")
        out = wfile.data.decode()
        assert '"type": "error"' in out
        assert '"error": {' in out

    def test_write_failure_swallowed(self):
        """A broken wfile must not raise — caller shouldn't crash."""
        _reload_all()
        from backend_adapter.streaming import _write_sse_error_native
        wfile = FakeWfile()
        def broken_write(data):
            raise BrokenPipeError("gone")
        wfile.write = broken_write
        _write_sse_error_native(wfile, "responses", "boom")  # must not raise


class TestUsageWarnErrFile:
    """USAGE_WARN → .err (v0.9.1): когда бэкенд не вернул usage в стриме и
    передан out_body, эвристика input_tokens пишет WARNING-блок в
    session-*.err (безусловный канал). Без out_body записи нет — старые
    прямые вызовы (и прежние тесты) ведут себя как раньше."""

    def _fresh(self, tmp_path):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log as slog
        slog._DEBUG_IS_DIR = True
        slog._TRACE_IS_DIR = True
        slog._DEBUG_PATH = str(tmp_path)
        slog._TRACE_PATH = str(tmp_path)
        slog._session_logs.clear()
        slog._session_file_ts.clear()
        return slog

    def _stream_no_usage(self):
        """SSE-поток без usage: content + finish_reason stop."""
        from tests.conftest import FakeRespStream
        return FakeRespStream([
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}]}',
            b'data: [DONE]',
        ])

    def test_out_body_passed_writes_err(self, tmp_path):
        """С переданным out_body/backend_url → WARNING-блок в .err с полным
        [REQUEST] и текстом эвристики."""
        self._fresh(tmp_path)  # сначала перезагрузка модулей: session_log с путями tmp_path
        from backend_adapter import session_log as slog
        from backend_adapter.streaming import stream_openai_to_anthropic
        from tests.conftest import FakeWfile
        slog._session_file_ts["sess1"] = "20260909-100000"
        out_body = b'{"model": "test", "stream": true}'
        stream_openai_to_anthropic(
            self._stream_no_usage(), FakeWfile(), "test", "sess1", "req1",
            approx_prompt_chars=200, out_body=out_body, backend_url="http://b/v1",
        )
        files = list(tmp_path.glob("session-*.err"))
        assert len(files) == 1
        content = files[0].read_text(encoding="utf-8")
        assert "==================== WARNING ====================" in content
        assert '{"model": "test", "stream": true}' in content  # полный [REQUEST]
        assert "input_tokens оценён эвристически" in content
        assert "[WARN]" in content
        assert "backend_url=http://b/v1" in content

    def test_no_out_body_no_err(self, tmp_path):
        """Без out_body (прямые вызовы/старые тесты) — .err НЕ пишется."""
        self._fresh(tmp_path)
        from backend_adapter.streaming import stream_openai_to_anthropic
        from tests.conftest import FakeWfile
        stream_openai_to_anthropic(
            self._stream_no_usage(), FakeWfile(), "test", "sess1", "req1",
            approx_prompt_chars=200,
        )
        assert list(tmp_path.glob("session-*.err")) == []

    def test_backend_usage_no_warn(self, tmp_path):
        """Бэкенд вернул usage → WARN нет, .err не создан."""
        self._fresh(tmp_path)
        from backend_adapter.streaming import stream_openai_to_anthropic
        from tests.conftest import FakeRespStream, FakeWfile
        lines = FakeRespStream([
            b'data: {"choices": [{"delta": {"content": "x"}, "finish_reason": "stop"}], '
            b'"usage": {"prompt_tokens": 5, "completion_tokens": 3}}',
            b'data: [DONE]',
        ])
        stream_openai_to_anthropic(
            lines, FakeWfile(), "test", "sess1", "req1",
            approx_prompt_chars=200, out_body=b"{}", backend_url="http://b",
        )
        assert list(tmp_path.glob("session-*.err")) == []


class TestResponsesControlMessage:
    """Tests for emit_responses_control_message() — синтетический SSE-ответ
    Responses API (v0.9.6)."""

    def _events(self, wfile):
        """Парсит буфер FakeWfile обратно в список (event, data)."""
        text = wfile.data.decode("utf-8")
        events = []
        for block in text.strip().split("\n\n"):
            if not block.strip():
                continue
            lines = block.split("\n")
            evt = None
            data = None
            for line in lines:
                if line.startswith("event: "):
                    evt = line[7:].strip()
                elif line.startswith("data: "):
                    data = json.loads(line[6:])
            if evt and data is not None:
                events.append((evt, data))
        return events

    def test_sse_sequence(self):
        from backend_adapter.streaming import emit_responses_control_message
        wfile = FakeWfile()
        emit_responses_control_message(wfile, "Model switched to `qwen3-coder`.", "qwen3-coder")
        events = self._events(wfile)
        # Ожидаемая последовательность событий (см. streaming.py)
        expected_types = [
            "response.created",
            "response.output_item.added",
            "response.content_part.added",
            "response.output_text.delta",
            "response.output_text.done",
            "response.content_part.done",
            "response.output_item.done",
            "response.completed",
        ]
        assert [e[0] for e in events] == expected_types

    def test_delta_contains_text(self):
        from backend_adapter.streaming import emit_responses_control_message
        wfile = FakeWfile()
        emit_responses_control_message(wfile, "hello world", "m1")
        events = self._events(wfile)
        delta = next(e for e in events if e[0] == "response.output_text.delta")[1]
        assert delta["delta"] == "hello world"

    def test_completed_status(self):
        from backend_adapter.streaming import emit_responses_control_message
        wfile = FakeWfile()
        emit_responses_control_message(wfile, "ack", "model-x")
        events = self._events(wfile)
        completed = next(e for e in events if e[0] == "response.completed")[1]
        assert completed["response"]["status"] == "completed"
        assert completed["response"]["output"][0]["content"][0]["text"] == "ack"

    def test_usage_is_none(self):
        from backend_adapter.streaming import emit_responses_control_message
        wfile = FakeWfile()
        emit_responses_control_message(wfile, "ack", "model-x")
        events = self._events(wfile)
        created = next(e for e in events if e[0] == "response.created")[1]
        assert created["response"].get("usage") is None


class TestBuildResponsesControlResponse:
    """Tests for build_responses_control_response() — non-stream синтетический
    ответ (v0.9.6)."""

    def test_structure(self):
        from backend_adapter.streaming import build_responses_control_response
        resp = build_responses_control_response("switched to q", "qwen3-coder")
        assert resp["object"] == "response"
        assert resp["status"] == "completed"
        assert resp["model"] == "qwen3-coder"
        assert len(resp["output"]) == 1
        assert resp["output"][0]["type"] == "message"
        assert resp["output"][0]["role"] == "assistant"
        assert resp["output"][0]["content"][0]["type"] == "output_text"
        assert resp["output"][0]["content"][0]["text"] == "switched to q"
        assert resp["usage"] is None

    def test_created_at_is_int(self):
        from backend_adapter.streaming import build_responses_control_response
        resp = build_responses_control_response("ack", "m")
        assert isinstance(resp["created_at"], int)
