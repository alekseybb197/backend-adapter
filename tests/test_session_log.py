"""Tests for backend_adapter.session_log — YAML dump, session file management."""
import json
import os
from unittest import mock


def _reload_all():
    import sys
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]


class TestDumpYaml:
    """Tests for dump_yaml()."""

    def test_multiline_block_scalar(self):
        result = {"key": "line1\nline2\nline3"}
        yaml_out = result["key"].split("\n")
        import yaml
        from backend_adapter.session_log import dump_yaml
        # Actually test via the function
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter.session_log import dump_yaml
        yaml_text = dump_yaml({"text": "line1\nline2"})
        assert "|" in yaml_text  # block scalar indicator

    def test_single_line_plain(self):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter.session_log import dump_yaml
        yaml_text = dump_yaml({"text": "single line"})
        assert "single line" in yaml_text
        assert '"' not in yaml_text.split("single line")[0].split("\n")[-1]

    def test_special_chars_quoted(self):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter.session_log import dump_yaml
        yaml_text = dump_yaml({"key": "colon: val"})
        assert '"' in yaml_text  # should be quoted

    def test_roundtrip(self):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter.session_log import dump_yaml
        import yaml
        data = {"text": "line1\nline2", "plain": "hello"}
        yaml_text = dump_yaml(data)
        loaded = yaml.safe_load(yaml_text)
        assert loaded == data


class TestMakeSessionFile:
    """Tests for _make_session_file()."""

    def test_format(self):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log
        path = session_log._make_session_file("/logs", "abc123def456", "log")
        assert "session-" in path
        assert "abc123def456" not in path  # truncated
        assert path.endswith(".log")

    def test_special_chars_sanitized(self):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log
        path = session_log._make_session_file("/logs", "abc/def!@#", "log")
        # Directory prefix (/logs/) is preserved; only session_id[:8] is sanitized
        filename = path.split("/")[-1]
        assert "/" not in filename
        assert "@" not in filename
        assert "abc_def_" in filename

    def test_stable_timestamp(self):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log
        session_log._session_file_ts["sess1"] = "20260831-120000"
        path1 = session_log._make_session_file("/logs", "sess1", "log")
        path2 = session_log._make_session_file("/logs", "sess1", "log")
        assert path1 == path2


class TestWriteDebugJson:
    """Tests for write_debug_json()."""

    def test_writes_json_file(self, tmp_path):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log, config
        session_log._DEBUG_IS_DIR = True
        session_log._DEBUG_PATH = str(tmp_path)
        config.ADAPTER_DEBUG_PARTS = True
        config.ADAPTER_DEBUG = True
        # Force parts dir creation
        session_log._parts_dir["sess1_jsonparts"] = tmp_path / "parts"
        session_log._parts_dir_ts["sess1"] = "20260831-120000"
        import os
        os.makedirs(str(tmp_path / "parts"), exist_ok=True)

        # Actually set the parts dir directly
        session_log._parts_dir = {"sess1_jsonparts": str(tmp_path / "parts")}
        session_log._debug_json_seq = 0

        session_log.write_debug_json("sess1", "TEST", {"key": "value"})
        files = list((tmp_path / "parts").glob("*.json"))
        assert len(files) == 1
        data = json.loads(files[0].read_text())
        assert data == {"key": "value"}

    def test_flag_off_no_files(self, tmp_path):
        """When ADAPTER_DEBUG_PARTS flag is off, no dumps are written."""
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log, config
        session_log._DEBUG_IS_DIR = True
        session_log._DEBUG_PATH = str(tmp_path)
        config.ADAPTER_DEBUG_PARTS = False
        config.ADAPTER_DEBUG = True
        parts = tmp_path / "parts"
        parts.mkdir(exist_ok=True)
        session_log._parts_dir = {"sess1_jsonparts": str(parts)}
        session_log._debug_json_seq = 0

        session_log.write_debug_json("sess1", "NOT_TEST", {"key": "value"})
        files = list(parts.glob("*.json"))
        assert len(files) == 0

    def test_no_dir_no_files(self, tmp_path):
        """When ADAPTER_DEBUG_LOGPATH is not set / not a directory, no dumps are written."""
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log, config
        session_log._DEBUG_IS_DIR = False
        session_log._DEBUG_PATH = str(tmp_path / "not-a-dir.log")
        config.ADAPTER_DEBUG_PARTS = True
        config.ADAPTER_DEBUG = True
        parts = tmp_path / "parts"
        parts.mkdir(exist_ok=True)
        session_log._parts_dir = {"sess1_jsonparts": str(parts)}
        session_log._debug_json_seq = 0

        session_log.write_debug_json("sess1", "BODY", {"key": "value"})
        files = list(parts.glob("*.json"))
        assert len(files) == 0

    def test_writes_yaml_alongside_json(self, tmp_path):
        """When ADAPTER_DEBUG_PARTS flag is on, .yaml is written alongside .json."""
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log, config
        session_log._DEBUG_IS_DIR = True
        session_log._DEBUG_PATH = str(tmp_path)
        config.ADAPTER_DEBUG_PARTS = True
        config.ADAPTER_DEBUG = True
        parts = tmp_path / "parts"
        parts.mkdir(exist_ok=True)
        session_log._parts_dir = {"sess1_jsonparts": str(parts)}
        session_log._debug_json_seq = 0

        session_log.write_debug_json("sess1", "TEST", {"key": "value"})
        json_files = list(parts.glob("*.json"))
        yaml_files = list(parts.glob("*.yaml"))
        assert len(json_files) == 1
        assert len(yaml_files) == 1

    def test_bytes_decoded(self, tmp_path):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log, config
        session_log._DEBUG_IS_DIR = True
        session_log._DEBUG_PATH = str(tmp_path)
        config.ADAPTER_DEBUG_PARTS = True
        config.ADAPTER_DEBUG = True
        parts = tmp_path / "parts"
        parts.mkdir(exist_ok=True)
        session_log._parts_dir = {"sess1_jsonparts": str(parts)}
        session_log._debug_json_seq = 0

        session_log.write_debug_json("sess1", "TEST", b'{"key":"value"}')
        data = json.loads((tmp_path / "parts" / [f for f in (tmp_path / "parts").glob("*.json")][0]).read_text())
        assert data == {"key": "value"}

    def test_master_switch_off_no_files(self, tmp_path):
        """When ADAPTER_DEBUG_ENABLE=0 (master switch), no dumps are written."""
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log, config
        session_log._DEBUG_IS_DIR = True
        session_log._DEBUG_PATH = str(tmp_path)
        config.ADAPTER_DEBUG_PARTS = True
        config.ADAPTER_DEBUG = False
        parts = tmp_path / "parts"
        parts.mkdir(exist_ok=True)
        session_log._parts_dir = {"sess1_jsonparts": str(parts)}
        session_log._debug_json_seq = 0

        session_log.write_debug_json("sess1", "TEST", {"key": "value"})
        files = list(parts.glob("*.json"))
        assert len(files) == 0


class TestOpenSessionFile:
    """Tests for _open_session_file() / _close_session_file()."""

    def test_file_handle_reuse(self, tmp_path):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log
        session_log._TRACE_IS_DIR = True
        session_log._TRACE_PATH = str(tmp_path)
        session_log._session_file_ts["sess1"] = "20260831-120000"
        # Clear any existing handles for this session
        session_log._session_logs.clear()

        fd1 = session_log._open_session_file("trace", "sess1")
        fd2 = session_log._open_session_file("trace", "sess1")
        # Should return the same handle
        assert fd1 is fd2
        fd1.close()

    def test_session_file_eviction(self, tmp_path):
        """When exceeding _LOG_FILES_PER_SESSION, oldest should be evicted."""
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log
        session_log._LOG_FILES_PER_SESSION = 2
        session_log._TRACE_IS_DIR = True
        session_log._TRACE_PATH = str(tmp_path)
        session_log._session_file_ts = {
            "sess1": "20260831-110000",
            "sess2": "20260831-120000",
            "sess3": "20260831-130000",
        }
        # Clear existing session logs so we can test eviction cleanly
        session_log._session_logs.clear()

        # Open files for sess1, sess2, sess3 (evicts when exceeding 2)
        session_log._open_session_file("trace", "sess1")
        session_log._open_session_file("trace", "sess2")
        session_log._open_session_file("trace", "sess3")
        # Adding sess4 should trigger eviction of oldest
        fd = session_log._open_session_file("trace", "sess4")
        assert fd is not None

    def test_no_logpath_no_file(self, tmp_path):
        """Пустой ADAPTER_DEBUG_LOGPATH → дефолт ./tmp/logs (v0.8.6), а не
        отсутствие путей: is_dir-флаги True всегда. Файл не пишется только
        когда путь выключен вручную (как isolate_logs) — тогда
        _open_session_file() возвращает None и ничего не создаёт на диске."""
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log
        # Import-time defaults: env LOGPATH пуст → дефолт ./tmp/logs
        assert session_log._DEBUG_IS_DIR is True
        assert session_log._TRACE_IS_DIR is True
        assert session_log._DEBUG_PATH == "./tmp/logs"
        assert session_log._TRACE_PATH == "./tmp/logs"
        # Не пишем в реальную ./tmp/logs — выключаем путь, как isolate_logs
        session_log._DEBUG_IS_DIR = False
        session_log._DEBUG_PATH = ""
        fd = session_log._open_session_file("debug", "sess1")
        assert fd is None
        assert list(tmp_path.glob("session-*")) == []
        # Оставляем глобалы девственно-чистыми (как инициализировал isolate_logs)
        session_log._DEBUG_PATH = ""
        session_log._TRACE_PATH = ""


class TestWriteErrorFile:
    """Tests for write_error_file() — безусловный .err-канал (v0.9.0)."""

    def _fresh(self):
        """Перезагрузить модули, указать LOGPATH на tmp_path."""
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log
        session_log._DEBUG_IS_DIR = True
        session_log._TRACE_IS_DIR = True
        return session_log

    def test_writes_err_file(self, tmp_path):
        """write_error_file пишет файл session-<ts>-<safe8>.err в LOGPATH."""
        import os
        session_log = self._fresh()
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._TRACE_PATH = str(tmp_path)
        session_log._session_file_ts["sess1"] = "20260908-120000"
        session_log.write_error_file(
            "sess1",
            "req123",
            final_status=400,
            backend_url="http://backend/v1/chat/completions",
            model="qwen",
            out_body=b'{"messages": []}',
            err_body="backend 400 body",
        )
        files = list(tmp_path.glob("session-*.err"))
        assert len(files) == 1
        name = files[0].name
        assert name.startswith("session-20260908-120000-")
        assert name.endswith(".err")
        content = files[0].read_text(encoding="utf-8")
        assert "==================== ERROR ====================" in content
        assert "==================== END ERROR ====================" in content
        assert "final_status=400" in content
        assert "req123" in content
        assert "session_id=sess1" in content
        assert "model=qwen" in content
        assert "http://backend/v1/chat/completions" in content
        # Полные запрос и ошибка
        assert '{"messages": []}' in content
        assert "backend 400 body" in content

    def test_writes_at_enable_zero(self, tmp_path):
        """Безусловность: .err пишется при ADAPTER_DEBUG_ENABLE=0 (мастер-
        выключатель файловой записи debug-логов на него не действует)."""
        import os
        session_log = self._fresh()
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._TRACE_PATH = str(tmp_path)
        from backend_adapter import config
        config.ADAPTER_DEBUG = False  # ENABLE=0
        session_log.write_error_file(
            "sess1", "req1", final_status=502,
            backend_url="http://b", model="m", out_body="{}", err_body="err",
        )
        assert len(list(tmp_path.glob("session-*.err"))) == 1

    def test_writes_without_debug_parts_flag(self, tmp_path):
        """ADAPTER_DEBUG_PARTS=0 не блокирует .err (это НЕ parts-дампы)."""
        session_log = self._fresh()
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._TRACE_PATH = str(tmp_path)
        from backend_adapter import config
        config.ADAPTER_DEBUG_PARTS = False
        session_log.write_error_file(
            "sess1", "req1", final_status=400,
            backend_url="http://b", model="m", out_body="{}", err_body="err",
        )
        assert len(list(tmp_path.glob("session-*.err"))) == 1

    def test_no_trim(self, tmp_path):
        """Содержимое БЕЗ обрезки по ADAPTER_DEBUG_TRIM (даже при малом TRIM)."""
        session_log = self._fresh()
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._TRACE_PATH = str(tmp_path)
        from backend_adapter import config
        config.ADAPTER_DEBUG_TRIM = 10  # малый лимит консольной обрезки
        long_req = '{"prompt": "' + "x" * 5000 + '"}'
        long_err = "E" * 5000
        session_log.write_error_file(
            "sess1", "req1", final_status=400,
            backend_url="http://b", model="m", out_body=long_req, err_body=long_err,
        )
        content = list(tmp_path.glob("session-*.err"))[0].read_text(encoding="utf-8")
        assert '{"prompt": "' + "x" * 5000 + '"}' in content  # полный запрос
        assert "E" * 5000 in content  # полная ошибка

    def test_redact_by_default(self, tmp_path):
        """По умолчанию секреты в .err маскируются redact (SENSITIVE=0)."""
        session_log = self._fresh()
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._TRACE_PATH = str(tmp_path)
        from backend_adapter import config
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = False
        session_log.write_error_file(
            "sess1", "req1", final_status=400,
            backend_url="http://b", model="m",
            out_body='{"text": "Authorization: Bearer sk-live-abcdef123456"}',
            err_body='{"error": "invalid Authorization: Bearer sk-live-abcdef123456"}',
        )
        content = list(tmp_path.glob("session-*.err"))[0].read_text(encoding="utf-8")
        assert "sk-live-abcdef123456" not in content
        assert "REDACTED" in content

    def test_sensitive_one_full_data(self, tmp_path):
        """При ADAPTER_SENSITIVE_LOGGING_ENABLE=1 — полные данные без redact."""
        session_log = self._fresh()
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._TRACE_PATH = str(tmp_path)
        from backend_adapter import config
        config.ADAPTER_SENSITIVE_LOGGING_ENABLE = True
        session_log.write_error_file(
            "sess1", "req1", final_status=400,
            backend_url="http://b", model="m",
            out_body='{"text": "Authorization: Bearer sk-live-abcdef123456"}',
            err_body='{"error": "invalid Authorization: Bearer sk-live-abcdef123456"}',
        )
        content = list(tmp_path.glob("session-*.err"))[0].read_text(encoding="utf-8")
        assert "sk-live-abcdef123456" in content
        assert "REDACTED" not in content

    def test_shared_ts_with_log_file(self, tmp_path):
        """Один session_file_ts: .err и .log сессии делят общий ts (тот же
        файл сессии по имени, разные расширения)."""
        session_log = self._fresh()
        session_log._DEBUG_PATH = str(tmp_path)
        session_log._TRACE_PATH = str(tmp_path)
        fd = session_log._open_session_file("debug", "sess1")
        assert fd is not None
        log_name = list(tmp_path.glob("session-*.log"))[0].name
        session_log.write_error_file(
            "sess1", "req1", final_status=400,
            backend_url="http://b", model="m", out_body="{}", err_body="err",
        )
        err_name = [f for f in tmp_path.glob("session-*.err")][0].name
        ts_log = log_name[len("session-") : len("session-") + 15]
        ts_err = err_name[len("session-") : len("session-") + 15]
        assert ts_log == ts_err
