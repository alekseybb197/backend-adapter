"""Tests for backend_adapter.logger — human-readable debug logging."""
import os
from unittest import mock


def _reload_all():
    import sys
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]


class TestLog:
    """Tests for _d()."""

    def test_debug_disabled_no_output(self, tmp_path):
        """ADAPTER_DEBUG_ENABLE=0 (master switch): _d() writes nothing,
        session file is not created."""
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = False
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._last_log_session_id = "sess1"
        from backend_adapter.logger import _d
        _d("test message")
        assert list(tmp_path.glob("session-*")) == []

    def test_debug_redacts_secrets(self, tmp_path):
        _reload_all()
        from backend_adapter import session_log, config, redact
        config.ADAPTER_DEBUG = True
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = False
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._last_log_session_id = "sess1"
        from backend_adapter.logger import _d
        _d("Bearer abc123xyz789")
        files = list(tmp_path.glob("session-*.log"))
        assert len(files) == 1
        log_content = files[0].read_text()
        assert "***REDACTED***" in log_content

    def test_debug_no_redaction_when_enabled(self, tmp_path):
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = True
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._last_log_session_id = "sess1"
        from backend_adapter.logger import _d
        _d("Bearer abc123xyz789")
        files = list(tmp_path.glob("session-*.log"))
        assert len(files) == 1
        log_content = files[0].read_text()
        assert "abc123xyz789" in log_content


class TestLogReqId:
    """Tests for _dr()."""

    def test_dr_prefix(self, tmp_path):
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = True
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._last_log_session_id = "sess1"
        from backend_adapter.logger import _dr
        _dr("req1", "test message")
        files = list(tmp_path.glob("session-*.log"))
        assert len(files) == 1
        log_content = files[0].read_text()
        assert "[req1]" in log_content
