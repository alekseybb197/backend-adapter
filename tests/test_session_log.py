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
        config.ADAPTER_DEBUG_TAGS_JSON = ["TEST"]
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

    def test_ignores_tag_outside_list(self, tmp_path):
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log, config
        session_log._DEBUG_IS_DIR = True
        session_log._DEBUG_PATH = str(tmp_path)
        config.ADAPTER_DEBUG_TAGS_JSON = ["TEST"]
        # Make sure parts dir exists
        parts = tmp_path / "parts"
        parts.mkdir(exist_ok=True)
        session_log._parts_dir = {"sess1_jsonparts": str(parts)}
        session_log._debug_json_seq = 0

        session_log.write_debug_json("sess1", "NOT_TEST", {"key": "value"})
        files = list(parts.glob("*.json"))
        assert len(files) == 0

    def test_writes_yaml_when_tag_in_yaml_list(self, tmp_path):
        """When tag is in ADAPTER_DEBUG_TAGS_YAML, .yaml should also be written."""
        import sys
        to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import session_log, config
        session_log._DEBUG_IS_DIR = True
        session_log._DEBUG_PATH = str(tmp_path)
        config.ADAPTER_DEBUG_TAGS_JSON = ["TEST"]
        config.ADAPTER_DEBUG_TAGS_YAML = ["TEST"]
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
        config.ADAPTER_DEBUG_TAGS_JSON = ["TEST"]
        parts = tmp_path / "parts"
        parts.mkdir(exist_ok=True)
        session_log._parts_dir = {"sess1_jsonparts": str(parts)}
        session_log._debug_json_seq = 0

        session_log.write_debug_json("sess1", "TEST", b'{"key":"value"}')
        data = json.loads((tmp_path / "parts" / [f for f in (tmp_path / "parts").glob("*.json")][0]).read_text())
        assert data == {"key": "value"}


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
