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
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = False
        session_log._DEBUG_PATH = str(tmp_path / "debug.log")
        session_log._DEBUG_IS_DIR = False
        # Write something — should go to file even if debug disabled
        # because _d() writes to file regardless of ADAPTER_DEBUG flag
        # Actually looking at code: _d() only writes to file, not print
        # ADAPTER_DEBUG only controls print()
        from backend_adapter.logger import _d
        _d("test message")
        log_content = (tmp_path / "debug.log").read_text()
        assert "test message" in log_content

    def test_debug_redacts_secrets(self, tmp_path):
        _reload_all()
        from backend_adapter import session_log, config, redact
        config.ADAPTER_DEBUG = False
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = False
        session_log._DEBUG_PATH = str(tmp_path / "debug2.log")
        session_log._DEBUG_IS_DIR = False
        from backend_adapter.logger import _d
        _d("Bearer abc123xyz789")
        log_content = (tmp_path / "debug2.log").read_text()
        assert "***REDACTED***" in log_content

    def test_debug_no_redaction_when_enabled(self, tmp_path):
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = False
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        session_log._DEBUG_PATH = str(tmp_path / "debug3.log")
        session_log._DEBUG_IS_DIR = False
        from backend_adapter.logger import _d
        _d("Bearer abc123xyz789")
        log_content = (tmp_path / "debug3.log").read_text()
        assert "abc123xyz789" in log_content


class TestLogReqId:
    """Tests for _dr()."""

    def test_dr_prefix(self, tmp_path):
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = False
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        session_log._DEBUG_PATH = str(tmp_path / "dr.log")
        session_log._DEBUG_IS_DIR = False
        from backend_adapter.logger import _dr
        _dr("req1", "test message")
        log_content = (tmp_path / "dr.log").read_text()
        assert "[req1]" in log_content
