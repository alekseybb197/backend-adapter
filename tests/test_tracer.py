"""Tests for backend_adapter.tracer — JSONL trace logging and tool-use causality."""
import json
import os
from unittest import mock


def _reload_all():
    import sys
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]


class TestNextSeq:
    """Tests for _next_seq() — monotonic sequence numbers."""

    def test_monotonic_across_calls(self):
        _reload_all()
        from backend_adapter import tracer
        s1 = tracer._next_seq("sess")
        s2 = tracer._next_seq("sess")
        assert s2 > s1

    def test_independent_sessions(self):
        """Each session has independent seq counters."""
        _reload_all()
        from backend_adapter import tracer
        tracer._session_seq["a"] = 5
        tracer._session_seq["b"] = 10
        # _next_seq returns the current value then increments
        assert tracer._next_seq("a") == 5
        assert tracer._next_seq("a") == 6
        assert tracer._next_seq("b") == 10
        assert tracer._next_seq("b") == 11


class TestToolUseCausality:
    """Tests for _register_tool_use, _lookup_tool_use_producer, _lookup_tool_use_name."""

    def test_register_and_lookup(self):
        _reload_all()
        from backend_adapter import tracer
        tracer._register_tool_use("sess", "tool1", "req1", "Bash")
        assert tracer._lookup_tool_use_producer("sess", "tool1") == "req1"
        assert tracer._lookup_tool_use_name("sess", "tool1") == "Bash"

    def test_empty_tool_use_id(self):
        _reload_all()
        from backend_adapter import tracer
        tracer._register_tool_use("sess", "", "req1")
        assert tracer._lookup_tool_use_producer("sess", "") is None

    def test_lookup_nonexistent(self):
        _reload_all()
        from backend_adapter import tracer
        assert tracer._lookup_tool_use_producer("sess", "missing") is None
        assert tracer._lookup_tool_use_name("sess", "missing") is None

    def test_fifo_eviction(self, tmp_path):
        """After exceeding _TOOL_USE_INDEX_MAX_PER_SESSION, oldest should be evicted."""
        _reload_all()
        from backend_adapter import tracer
        tracer._TOOL_USE_INDEX_MAX_PER_SESSION = 3
        # Register 5 entries
        for i in range(5):
            tracer._register_tool_use("sess", f"tool{i}", f"req{i}", f"tool{i}")
        # tool0 and tool1 should be evicted
        assert tracer._lookup_tool_use_producer("sess", "tool0") is None
        assert tracer._lookup_tool_use_producer("sess", "tool1") is None
        assert tracer._lookup_tool_use_producer("sess", "tool2") == "req2"
        assert tracer._lookup_tool_use_producer("sess", "tool4") == "req4"


class TestTrace:
    """Tests for _trace() — file writing behavior."""

    def test_trace_writes_jsonl(self, tmp_path):
        _reload_all()
        from backend_adapter import config
        config.ADAPTER_DEBUG = True
        from backend_adapter import session_log, tracer
        trace_dir = tmp_path / "traces"
        trace_dir.mkdir()
        session_log._TRACE_PATH = str(trace_dir)
        session_log._TRACE_IS_DIR = True
        tracer._trace("sess1", "req1", "test_event", field="value")
        files = list(trace_dir.glob("session-*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().split("\n")
        record = json.loads(lines[0])
        assert record["session_id"] == "sess1"
        assert record["req_id"] == "req1"
        assert record["event"] == "test_event"
        assert record["field"] == "value"

    def test_trace_noop_when_no_path(self):
        _reload_all()
        from backend_adapter import session_log, tracer
        session_log._TRACE_PATH = ""
        session_log._TRACE_IS_DIR = False
        # Should not raise
        tracer._trace("sess1", "req1", "test_event", field="value")

    def test_trace_redacts_secrets(self, tmp_path):
        _reload_all()
        from backend_adapter import config
        config.ADAPTER_DEBUG = True
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = False
        from backend_adapter import session_log, tracer
        trace_dir = tmp_path / "traces"
        trace_dir.mkdir()
        session_log._TRACE_PATH = str(trace_dir)
        session_log._TRACE_IS_DIR = True
        tracer._trace("sess1", "req1", "test_event",
                      token="Bearer abc123xyz789")
        files = list(trace_dir.glob("session-*.jsonl"))
        assert len(files) == 1
        line = files[0].read_text().strip()
        assert "***REDACTED***" in line

    def test_trace_dir_mode(self, tmp_path):
        _reload_all()
        from backend_adapter import config
        config.ADAPTER_DEBUG = True
        from backend_adapter import session_log, tracer
        trace_dir = tmp_path / "traces"
        trace_dir.mkdir()
        session_log._TRACE_PATH = str(trace_dir)
        session_log._TRACE_IS_DIR = True
        tracer._trace("sess1", "req1", "test_event")
        # Should write to a session file in the dir
        files = list(trace_dir.glob("session-*"))
        assert len(files) == 1
