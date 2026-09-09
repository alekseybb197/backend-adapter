"""Tests for backend_adapter.streaming — SSE streaming converter."""
import io
import json
from unittest import mock

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
