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

    def test_debug_disabled_no_file_output(self, tmp_path, capsys):
        """ADAPTER_DEBUG_ENABLE=0 (мастер-выключатель ФАЙЛОВОЙ записи): _d()
        НЕ создаёт session-файл, но строка всё равно печатается в консоль
        (консольные debug-логи безусловны — v0.8.6)."""
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = False
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._last_log_session_id = "sess1"
        from backend_adapter.logger import _d
        _d("test message")
        assert list(tmp_path.glob("session-*")) == []
        out = capsys.readouterr().out
        assert "test message" in out

    def test_console_trimmed_file_full(self, tmp_path, capsys):
        """v0.8.6-реформа: консоль всегда обрезана до ADAPTER_DEBUG_TRIM,
        файл при ADAPTER_DEBUG_ENABLE=1 несёт ПОЛНУЮ строку (без обрезки)."""
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = True
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        config.ADAPTER_DEBUG_TRIM = 10
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._last_log_session_id = "sess1"
        from backend_adapter.logger import _d
        _d("x" * 100)
        out = capsys.readouterr().out
        assert "x" * 10 in out
        assert "x" * 11 not in out  # хвост обрезан консолью
        files = list(tmp_path.glob("session-*.log"))
        assert len(files) == 1
        assert "x" * 100 in files[0].read_text()  # файл полный

    def test_console_trim_zero_no_trim(self, tmp_path, capsys):
        """ADAPTER_DEBUG_TRIM=0 — «без обрезки»: консоль получает полную
        строку (0 = выкл., не ошибка конфигурации)."""
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = False
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        config.ADAPTER_DEBUG_TRIM = 0
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._last_log_session_id = "sess1"
        from backend_adapter.logger import _d
        _d("x" * 100)
        out = capsys.readouterr().out
        assert "x" * 100 in out  # без обрезки

    def test_console_trim_runtime_live(self, tmp_path, capsys):
        """Лимит консоли читается ЖИВО через config.trim_limit(): смена
        ADAPTER_DEBUG_TRIM через runtime-пул действует на следующий же _d()."""
        _reload_all()
        from backend_adapter import session_log, config
        config.ADAPTER_DEBUG = False
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        config.ADAPTER_DEBUG_TRIM = 5
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._DEBUG_IS_DIR = True
        session_log._last_log_session_id = "sess1"
        from backend_adapter.logger import _d
        _d("x" * 50)
        out = capsys.readouterr().out
        assert "x" * 5 in out
        assert "x" * 6 not in out
        config.ADAPTER_DEBUG_TRIM = 100  # runtime-переключение
        _d("y" * 50)
        out = capsys.readouterr().out
        assert "y" * 50 in out  # новый лимит применён на следующем вызове

    def test_debug_redacts_secrets(self, tmp_path, capsys):
        _reload_all()
        from backend_adapter import session_log, config
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
        # Санитайзер работает и в консоли (единственный всегда-включённый канал)
        out = capsys.readouterr().out
        assert "abc123xyz789" not in out
        assert "***REDACTED***" in out

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
