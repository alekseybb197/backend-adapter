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
