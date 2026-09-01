"""Tests for backend_adapter.config — env var parsing, model mapping, backends."""
import os
import json
import importlib
import sys
from unittest import mock


def _reload_config():
    """Remove backend_adapter modules from sys.modules and reimport."""
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]


class TestParseModelsMapping:
    """Tests for _parse_models_mapping()."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_empty_string(self):
        assert self.config._parse_models_mapping("") == {}
        assert self.config._parse_models_mapping("  ") == {}

    def test_simple_mapping(self):
        result = self.config._parse_models_mapping("a:b")
        assert result == {"a": "b"}

    def test_multiple_mappings(self):
        result = self.config._parse_models_mapping("a:b,c:d")
        assert result == {"a": "b", "c": "d"}

    def test_with_spaces(self):
        result = self.config._parse_models_mapping("a : b , c : d")
        assert result == {"a": "b", "c": "d"}

    def test_skip_pairs_without_colon(self):
        result = self.config._parse_models_mapping("abc xyz")
        assert result == {}

    def test_skip_empty_values(self):
        result = self.config._parse_models_mapping("a: , :b")
        assert result == {}


class TestCap:
    """Tests for _cap()."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_zero_no_trim(self):
        assert self.config._cap("hello", 0) == "hello"

    def test_negative_no_trim(self):
        assert self.config._cap("hello", -1) == "hello"

    def test_shorter_than_max(self):
        assert self.config._cap("hello", 10) == "hello"

    def test_longer_than_max(self):
        result = self.config._cap("hello world", 5)
        assert result.startswith("hello")
        assert "[TRUNCATED" in result
        assert result.endswith("chars]")

    def test_none_text(self):
        assert self.config._cap(None, 5) is None


class TestTrimLimit:
    """Tests for _trim_limit()."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_trim_on(self):
        """Trim ON, tag not in full list → returns ADAPTER_DEBUG_TRIM."""
        self.config.ADAPTER_DEBUG_TRIM = 100
        self.config._ADAPTER_DEBUG_TAGS_FULL_SET = frozenset()
        assert self.config._trim_limit("BODY") == 100

    def test_trim_off_for_tag(self):
        """Trim OFF for specific tag → returns None."""
        self.config.ADAPTER_DEBUG_TRIM = 100
        self.config._ADAPTER_DEBUG_TAGS_FULL_SET = frozenset({"BODY"})
        assert self.config._trim_limit("BODY") is None


class TestParseBackendYaml:
    """Tests for _parse_backend_yaml()."""

    def test_valid_two_backends(self, tmp_path):
        yaml_content = """backend:
  - name: home
    base: http://localhost:8002
    key: ADAPTER_HOME_KEY
  - name: litellm
    base: https://litellm.example.com
    key: ADAPTER_LITELLM_KEY
"""
        yaml_file = tmp_path / "config.yaml"
        yaml_file.write_text(yaml_content)

        _reload_config()
        from backend_adapter import config
        os.environ["ADAPTER_HOME_KEY"] = "home-token"
        os.environ["ADAPTER_LITELLM_KEY"] = "litellm-token"
        result = config._parse_backend_yaml(str(yaml_file))

        assert result is not None
        assert len(result) == 2
        assert result[0]["name"] == "home"
        assert result[0]["base"] == "http://localhost:8002"
        assert result[0]["key"] == "home-token"

    def test_quotes_stripped(self, tmp_path):
        yaml_content = """backend:
  - name: 'home'
    base: "http://localhost:8002"
    key: KEY1
"""
        yaml_file = tmp_path / "quoted.yaml"
        yaml_file.write_text(yaml_content)

        _reload_config()
        from backend_adapter import config
        os.environ["KEY1"] = "val"
        result = config._parse_backend_yaml(str(yaml_file))
        assert result is not None
        assert result[0]["name"] == "home"
        assert result[0]["base"] == "http://localhost:8002"

    def test_comments_and_markers(self, tmp_path):
        yaml_content = """---
# comment
backend:
  - name: home
    # another comment
    base: http://localhost:8002
    key: KEY1
...
"""
        yaml_file = tmp_path / "comments.yaml"
        yaml_file.write_text(yaml_content)

        _reload_config()
        from backend_adapter import config
        os.environ["KEY1"] = "val"
        result = config._parse_backend_yaml(str(yaml_file))
        assert result is not None
        assert result[0]["name"] == "home"

    def test_incomplete_entry_skipped(self, tmp_path):
        yaml_content = """backend:
  - name: home
    base: http://localhost:8002
    key: KEY1
  - name: broken
    base: http://localhost:8003
"""
        yaml_file = tmp_path / "broken.yaml"
        yaml_file.write_text(yaml_content)

        _reload_config()
        from backend_adapter import config
        os.environ["KEY1"] = "val"
        result = config._parse_backend_yaml(str(yaml_file))
        assert result is not None
        assert len(result) == 1

    def test_nonexistent_file_returns_none(self):
        _reload_config()
        from backend_adapter import config
        result = config._parse_backend_yaml("/nonexistent/path.yaml")
        assert result is None


class TestResolveBackend:
    """Tests for _resolve_backend()."""

    def test_legacy_mode(self):
        _reload_config()
        from backend_adapter import config
        config.BACKEND_BASE = "http://localhost:9999"
        config.BACKEND_KEY = "test-key"
        config._BACKEND_LEGACY = True
        result = config._resolve_backend("my-model")
        assert result[0]["key"] == "test-key"
        assert result[1] == "my-model"

    def test_explicit_prefix(self):
        _reload_config()
        from backend_adapter import config
        config._BACKEND_LEGACY = False
        config._BACKEND_BY_NAME = {"kl": {"name": "kl", "base": "http://kl", "key": "k"}}
        config._DEFAULT_BACKEND = config._BACKEND_BY_NAME["kl"]
        result = config._resolve_backend("kl.qwen36")
        assert result[0]["name"] == "kl"
        assert result[1] == "qwen36"

    def test_lookup_by_model_to_backend(self):
        _reload_config()
        from backend_adapter import config
        config._BACKEND_LEGACY = False
        config._BACKEND_BY_NAME = {"kl": {"name": "kl", "base": "http://kl", "key": "k"}}
        config._MODEL_TO_BACKEND = {"qwen36": ("kl", config._BACKEND_BY_NAME["kl"])}
        config._DEFAULT_BACKEND = config._BACKEND_BY_NAME["kl"]
        result = config._resolve_backend("qwen36")
        assert result[0]["name"] == "kl"

    def test_fallback_to_default(self):
        _reload_config()
        from backend_adapter import config
        config._BACKEND_LEGACY = False
        default = {"name": "default", "base": "http://def", "key": "def-key"}
        config._BACKEND_BY_NAME = {"default": default}
        config._DEFAULT_BACKEND = default
        result = config._resolve_backend("unknown-model")
        assert result[0]["name"] == "default"


class TestInitMultiBackends:
    """Tests for _init_multi_backends() and _fetch_models()."""

    def test_fetch_models_success(self):
        _reload_config()
        from backend_adapter import config
        # Simulate successful model fetch
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = mock.Mock()
            mock_response.read.return_value = json.dumps({
                "object": "list", "data": [{"id": "m1"}, {"id": "m2"}]
            }).encode()
            mock_urlopen.return_value = mock_response
            result = config._fetch_models("http://test", "key")
            assert result == [{"id": "m1"}, {"id": "m2"}]

    def test_fetch_models_list_format(self):
        _reload_config()
        from backend_adapter import config
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_response = mock.Mock()
            mock_response.read.return_value = json.dumps([
                {"id": "m1"}, {"id": "m2"}
            ]).encode()
            mock_urlopen.return_value = mock_response
            result = config._fetch_models("http://test", "key")
            assert result == [{"id": "m1"}, {"id": "m2"}]

    def test_fetch_models_connection_error(self):
        _reload_config()
        from backend_adapter import config
        with mock.patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = Exception("Connection refused")
            try:
                config._fetch_models("http://test", "key")
                assert False, "Should have raised"
            except Exception:
                pass

    def test_init_multi_backends_no_models_exits(self, tmp_path):
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("""backend:
  - name: home
    base: http://localhost:9999
    key: KEY
""")
        _reload_config()
        from backend_adapter import config
        with mock.patch.object(config, "_fetch_models", return_value=[]):
            try:
                config._init_multi_backends(str(yaml_file))
                assert False, "Should have exited"
            except SystemExit:
                pass

    def test_init_multi_backends_prefix_collision(self, tmp_path):
        yaml_file = tmp_path / "collision.yaml"
        yaml_file.write_text("""backend:
  - name: kl
    base: http://kl
    key: k1
  - name: litellm
    base: http://litellm
    key: k2
""")
        _reload_config()
        from backend_adapter import config
        def fake_fetch(base, key):
            if "kl" in base:
                return [{"id": "qwen36"}]
            return [{"id": "qwen36"}]
        with mock.patch.object(config, "_fetch_models", side_effect=fake_fetch):
            config._init_multi_backends(str(yaml_file))
        # qwen36 exists on both → should be prefixed
        assert "kl.qwen36" in config._AVAILABLE_MODELS
        assert "litellm.qwen36" in config._AVAILABLE_MODELS
