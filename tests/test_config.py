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


class TestHostVars:
    """Tests for ADAPTER_ENDPOINT_HOST / ADAPTER_WEBUI_HOST env parsing.

    Both default to "127.0.0.1" (localhost). The `or "127.0.0.1"` idiom matters:
    an *empty* env value must ALSO resolve to the default — in a socketserver
    bind tuple "" would mean INADDR_ANY (all interfaces)."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_defaults(self):
        """Env unset / default (fresh_env sets both) → 127.0.0.1."""
        assert self.config.ADAPTER_ENDPOINT_HOST == "127.0.0.1"
        assert self.config.ADAPTER_WEBUI_HOST == "127.0.0.1"

    def test_empty_env_means_default(self, monkeypatch):
        """Empty env value must NOT become INADDR_ANY — resolves to default."""
        monkeypatch.setenv("ADAPTER_ENDPOINT_HOST", "")
        monkeypatch.setenv("ADAPTER_WEBUI_HOST", "")
        _reload_config()
        from backend_adapter import config
        assert config.ADAPTER_ENDPOINT_HOST == "127.0.0.1"
        assert config.ADAPTER_WEBUI_HOST == "127.0.0.1"

    def test_explicit_host_from_env(self, monkeypatch):
        """Explicit value (all interfaces) is honored."""
        monkeypatch.setenv("ADAPTER_ENDPOINT_HOST", "0.0.0.0")
        monkeypatch.setenv("ADAPTER_WEBUI_HOST", "0.0.0.0")
        _reload_config()
        from backend_adapter import config
        assert config.ADAPTER_ENDPOINT_HOST == "0.0.0.0"
        assert config.ADAPTER_WEBUI_HOST == "0.0.0.0"


class TestZeroConfigDefaults:
    """Default flags for the zero-config run (v0.7.2).

    With env vars *absent* the adapter should: process no tool logs
    (TOOLS_ERROR=0) and show status by default (WEBUI_ENABLE=1). Both parse
    off-words incl. "" — so asserting the default requires delenv, NOT
    setenv("", ...) (an empty env value parses as False for both)."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_webui_enable_defaults_true(self, monkeypatch):
        """WEBUI_ENABLE unset → enabled (status page up by default)."""
        monkeypatch.delenv("ADAPTER_WEBUI_ENABLE", raising=False)
        _reload_config()
        from backend_adapter import config
        assert config.ADAPTER_WEBUI_ENABLE is True
        assert config.ADAPTER_WEBUI_PORT == 8765

    def test_tools_error_defaults_false(self, monkeypatch):
        """TOOLS_ERROR unset → disabled (no tool-error log processing)."""
        monkeypatch.delenv("ADAPTER_DEBUG_TOOLS_ERROR", raising=False)
        _reload_config()
        from backend_adapter import config
        assert config.ADAPTER_DEBUG_TOOLS_ERROR is False

    def test_webui_explicit_off(self, monkeypatch):
        """WEBUI_ENABLE=0 → disabled."""
        monkeypatch.setenv("ADAPTER_WEBUI_ENABLE", "0")
        _reload_config()
        from backend_adapter import config
        assert config.ADAPTER_WEBUI_ENABLE is False


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

    def test_explicit_prefix(self):
        _reload_config()
        from backend_adapter import config
        config._BACKEND_BY_NAME = {"kl": {"name": "kl", "base": "http://kl", "key": "k"}}
        config._DEFAULT_BACKEND = config._BACKEND_BY_NAME["kl"]
        result = config._resolve_backend("kl.qwen36")
        assert result[0]["name"] == "kl"
        assert result[1] == "qwen36"

    def test_lookup_by_model_to_backend(self):
        _reload_config()
        from backend_adapter import config
        config._BACKEND_BY_NAME = {"kl": {"name": "kl", "base": "http://kl", "key": "k"}}
        config._MODEL_TO_BACKEND = {"qwen36": ("kl", config._BACKEND_BY_NAME["kl"])}
        config._DEFAULT_BACKEND = config._BACKEND_BY_NAME["kl"]
        result = config._resolve_backend("qwen36")
        assert result[0]["name"] == "kl"

    def test_fallback_to_default(self):
        _reload_config()
        from backend_adapter import config
        default = {"name": "default", "base": "http://def", "key": "def-key"}
        config._BACKEND_BY_NAME = {"default": default}
        config._DEFAULT_BACKEND = default
        result = config._resolve_backend("unknown-model")
        assert result[0]["name"] == "default"

    def test_no_backend_configured_raises(self):
        # Пустой конфиг при корректном старте недостижим (fatal), но на
        # случай неожиданных путей — явная ошибка, а не тихий fallback.
        _reload_config()
        from backend_adapter import config
        config._BACKEND_BY_NAME = {}
        config._MODEL_TO_BACKEND = {}
        config._DEFAULT_BACKEND = None
        try:
            config._resolve_backend("any-model")
            assert False, "Should have raised RuntimeError"
        except RuntimeError:
            pass


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

    def test_init_multi_backends_mutates_global_dicts_in_place(self, tmp_path):
        """Init должен МУТИРОВАТЬ глобальные словари на месте, а не
        переприсваивать их: server.py и другие модули делают
        ``from .config import _AVAILABLE_MODELS`` на импорте и держат ссылку
        на исходный объект. Пересоздание словаря оставляет этим ссылкам
        пустой/устаревший кэш (реальный баг: /v1/models -> 501, строгая
        валидация -> 400 на живых моделях)."""
        yaml_file = tmp_path / "init.yaml"
        yaml_file.write_text("""backend:
  - name: home
    base: http://home
    key: k
""")
        _reload_config()
        from backend_adapter import config

        # Ссылки-импортёры: держат исходные объекты словарей (как делает
        # server.py через `from .config import _AVAILABLE_MODELS`).
        server_view_models = config._AVAILABLE_MODELS
        server_view_map = config._MODEL_TO_BACKEND

        with mock.patch.object(
            config, "_fetch_models", return_value=[{"id": "m1"}, {"id": "m2"}]
        ):
            config._init_multi_backends(str(yaml_file))

        # Словари-объекты те же, содержимое новое (ссылки импортёров живые).
        assert config._AVAILABLE_MODELS is server_view_models
        assert config._MODEL_TO_BACKEND is server_view_map
        assert set(server_view_models) == {"m1", "m2"}
        assert set(server_view_map) == {"m1", "m2"}

class TestRefreshModels:
    """Tests for refresh_models() — on-demand model cache refresh.

    Contract: ok=True → cache rebuilt from *responding* backends (partial
    failure drops the failed one); ok=False (total failure / empty answer /
    nothing configured) → old cache kept untouched, count = old size.
    """

    def _solo_config(self):
        """Reload config with a single backend configured via multi-backend
        globals (единственный режим конфигурации — YAML-конфиг)."""
        _reload_config()
        from backend_adapter import config
        solo = {"name": "solo", "base": "http://solo.local", "key": "k"}
        config._BACKENDS = [solo]
        config._BACKEND_BY_NAME = {"solo": solo}
        config._DEFAULT_BACKEND = solo
        return config

    def test_solo_success_updates_cache(self):
        config = self._solo_config()
        config._AVAILABLE_MODELS["old-m"] = {"id": "old-m"}
        with mock.patch.object(
            config, "_fetch_models",
            return_value=[{"id": "new-m1"}, {"id": "new-m2", "owned_by": "me"}],
        ) as m_fetch:
            result = config.refresh_models(timeout=7.0)
        assert result == {"ok": True, "count": 2, "errors": {}}
        assert set(config._AVAILABLE_MODELS) == {"new-m1", "new-m2"}
        assert "old-m" not in config._AVAILABLE_MODELS
        # timeout пробрасывается в _fetch_models (короткий — из веб-страницы)
        assert m_fetch.call_args.kwargs.get("timeout") == 7.0

    def test_solo_fetch_error_keeps_old_cache(self):
        config = self._solo_config()
        config._AVAILABLE_MODELS["old-m"] = {"id": "old-m"}
        with mock.patch.object(
            config, "_fetch_models", side_effect=OSError("Connection refused by test")
        ):
            result = config.refresh_models()
        assert result["ok"] is False
        assert result["count"] == 1            # прежний размер кэша
        assert "solo" in result["errors"]
        assert "Connection refused by test" in str(result["errors"]["solo"])
        assert set(config._AVAILABLE_MODELS) == {"old-m"}   # кэш не тронут

    def test_solo_empty_answer_keeps_old_cache(self):
        config = self._solo_config()
        config._AVAILABLE_MODELS["old-m"] = {"id": "old-m"}
        with mock.patch.object(config, "_fetch_models", return_value=[]):
            result = config.refresh_models()
        assert result["ok"] is False
        assert result["errors"] == {}
        assert set(config._AVAILABLE_MODELS) == {"old-m"}

    def test_multi_success_and_prefix_collision(self):
        _reload_config()
        from backend_adapter import config
        aaa = {"name": "AAA", "base": "http://aaa", "key": "k-aaa"}
        bbb = {"name": "BBB", "base": "http://bbb", "key": "k-bbb"}
        config._BACKENDS = [aaa, bbb]
        config._BACKEND_BY_NAME = {"AAA": aaa, "BBB": bbb}
        config._DEFAULT_BACKEND = aaa

        def fake_fetch(base, key, timeout=None):
            if "aaa" in base:
                return [{"id": "shared"}, {"id": "only-aaa"}]
            return [{"id": "shared"}]           # коллизия на "shared"

        with mock.patch.object(config, "_fetch_models", side_effect=fake_fetch):
            result = config.refresh_models()
        assert result["ok"] is True
        assert result["errors"] == {}
        assert "AAA.shared" in config._AVAILABLE_MODELS
        assert "BBB.shared" in config._AVAILABLE_MODELS
        assert "only-aaa" in config._AVAILABLE_MODELS
        # префиксы — и в маршрутизации
        assert config._MODEL_TO_BACKEND["AAA.shared"][0] == "AAA"
        assert config._MODEL_TO_BACKEND["BBB.shared"][0] == "BBB"

    def test_multi_partial_failure_drops_failed_backend(self):
        _reload_config()
        from backend_adapter import config
        aaa = {"name": "AAA", "base": "http://aaa", "key": "k-aaa"}
        bbb = {"name": "BBB", "base": "http://bbb", "key": "k-bbb"}
        config._BACKENDS = [aaa, bbb]
        config._BACKEND_BY_NAME = {"AAA": aaa, "BBB": bbb}
        config._DEFAULT_BACKEND = aaa
        # Старый кэш содержал модели обоих бэкендов.
        config._AVAILABLE_MODELS["m-aaa"] = {"id": "m-aaa"}
        config._AVAILABLE_MODELS["m-bbb"] = {"id": "m-bbb"}

        def fake_fetch(base, key, timeout=None):
            if "bbb" in base:
                raise OSError("Connection refused by test")
            return [{"id": "m-aaa"}, {"id": "m-aaa-new"}]

        with mock.patch.object(config, "_fetch_models", side_effect=fake_fetch):
            result = config.refresh_models()
        # ok=True: ответивший бэкенд пересобрал кэш; упавший выпал из него
        assert result["ok"] is True
        assert "BBB" in result["errors"]
        assert "Connection refused by test" in str(result["errors"]["BBB"])
        assert set(config._AVAILABLE_MODELS) == {"m-aaa", "m-aaa-new"}
        assert "m-bbb" not in config._AVAILABLE_MODELS

    def test_multi_total_failure_keeps_old_cache(self):
        _reload_config()
        from backend_adapter import config
        aaa = {"name": "AAA", "base": "http://aaa", "key": "k-aaa"}
        bbb = {"name": "BBB", "base": "http://bbb", "key": "k-bbb"}
        config._BACKENDS = [aaa, bbb]
        config._BACKEND_BY_NAME = {"AAA": aaa, "BBB": bbb}
        config._DEFAULT_BACKEND = aaa
        config._AVAILABLE_MODELS["old-m"] = {"id": "old-m"}
        config._MODEL_TO_BACKEND["old-m"] = ("AAA", aaa)

        with mock.patch.object(
            config, "_fetch_models", side_effect=OSError("all down")
        ):
            result = config.refresh_models()
        assert result["ok"] is False
        assert result["count"] == 1
        assert set(result["errors"]) == {"AAA", "BBB"}
        assert set(config._AVAILABLE_MODELS) == {"old-m"}     # кэш не тронут
        assert set(config._MODEL_TO_BACKEND) == {"old-m"}

    def test_multi_empty_answers_keeps_old_cache(self):
        _reload_config()
        from backend_adapter import config
        aaa = {"name": "AAA", "base": "http://aaa", "key": "k-aaa"}
        config._BACKENDS = [aaa]
        config._BACKEND_BY_NAME = {"AAA": aaa}
        config._DEFAULT_BACKEND = aaa
        config._AVAILABLE_MODELS["old-m"] = {"id": "old-m"}

        with mock.patch.object(config, "_fetch_models", return_value=[]):
            result = config.refresh_models()
        assert result["ok"] is False
        assert set(config._AVAILABLE_MODELS) == {"old-m"}

    def test_standalone_reads_yaml_and_raises_globals(self, tmp_path):
        # Старт адаптера не было (глобалы пусты), но ADAPTER_BACKEND_CONFIG
        # задан — refresh перечитывает YAML, поднимает глобалы и опрашивает.
        _reload_config()
        from backend_adapter import config
        config._BACKENDS = []                    # как в standalone-процессе
        config._BACKEND_BY_NAME = {}
        config._DEFAULT_BACKEND = None
        yaml_file = tmp_path / "backends.yaml"
        yaml_file.write_text("""backend:
  - name: AAA
    base: http://aaa
    key: ADAPTER_TEST_KEY_AAA
""")
        os.environ["ADAPTER_TEST_KEY_AAA"] = "token-aaa"
        os.environ["ADAPTER_BACKEND_CONFIG"] = str(yaml_file)
        config.ADAPTER_BACKEND_CONFIG = str(yaml_file)   # импортное значение

        with mock.patch.object(
            config, "_fetch_models", return_value=[{"id": "solo-m"}]
        ):
            result = config.refresh_models()
        assert result["ok"] is True
        assert result["count"] == 1
        assert config._BACKENDS[0]["name"] == "AAA"
        assert config._BACKEND_BY_NAME["AAA"]["key"] == "token-aaa"
        assert config._DEFAULT_BACKEND["name"] == "AAA"
        assert set(config._AVAILABLE_MODELS) == {"solo-m"}

    def test_standalone_nothing_configured(self):
        # Ни бэкендов, ни env: refresh нечего делать — ok=False, кэш пуст.
        _reload_config()
        from backend_adapter import config
        config._BACKENDS = []
        config._BACKEND_BY_NAME = {}
        config._DEFAULT_BACKEND = None
        config.ADAPTER_BACKEND_CONFIG = ""
        os.environ["ADAPTER_BACKEND_CONFIG"] = ""
        with mock.patch.object(config, "_fetch_models") as m_fetch:
            result = config.refresh_models()
        assert result["ok"] is False
        assert result["count"] == 0
        assert result["errors"] == {}
        m_fetch.assert_not_called()              # в сеть не ходили

    def test_timeout_not_inherited_from_adapter(self):
        # refresh_models(timeout=...) должен передать явный timeout в
        # _fetch_models, а не молча использовать ADAPTER_TIMEOUT (300 с) —
        # страница статуса не должна висеть. Проверяем контракт на уровне
        # _fetch_models: timeout=None → ADAPTER_TIMEOUT.
        _reload_config()
        from backend_adapter import config
        with mock.patch("urllib.request.urlopen") as m_urlopen:
            mock_response = mock.Mock()
            mock_response.read.return_value = json.dumps({"data": []}).encode()
            m_urlopen.return_value = mock_response
            config._fetch_models("http://x", "k")
            assert m_urlopen.call_args.kwargs["timeout"] == config.ADAPTER_TIMEOUT
            config._fetch_models("http://x", "k", timeout=5.0)
            assert m_urlopen.call_args.kwargs["timeout"] == 5.0


class TestRuntimeConfig:
    """Tests for runtime config pool — get/set via /config endpoint."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_valid_keys_applied(self):
        """Valid bool/int values are applied and visible in get_runtime_config()."""
        result = self.config.set_runtime_config(
            ADAPTER_DEBUG=False,
            ADAPTER_DEBUG_TAGS_OUT=True,
            ADAPTER_TRACE_REASONING_MAX_CHARS=500,
        )
        assert result["ADAPTER_DEBUG"] is False
        assert result["ADAPTER_DEBUG_TAGS_OUT"] is True
        assert result["ADAPTER_TRACE_REASONING_MAX_CHARS"] == 500
        # Проверка через get
        current = self.config.get_runtime_config()
        assert current["ADAPTER_DEBUG"] is False
        assert current["ADAPTER_DEBUG_TAGS_OUT"] is True
        assert current["ADAPTER_TRACE_REASONING_MAX_CHARS"] == 500

    def test_unknown_key_ignored(self):
        """Unknown key is silently ignored."""
        before = self.config.get_runtime_config()
        result = self.config.set_runtime_config(UNKNOWN_KEY="value")
        after = self.config.get_runtime_config()
        assert result == before == after
        assert "UNKNOWN_KEY" not in result

    def test_key_outside_pool_ignored(self):
        """Key outside RUNTIME_CONFIG_POOL is silently ignored."""
        before = self.config.get_runtime_config()
        webui_before = self.config.ADAPTER_WEBUI_ENABLE
        result = self.config.set_runtime_config(ADAPTER_WEBUI_ENABLE=not webui_before)
        after = self.config.get_runtime_config()
        assert result == before == after
        assert self.config.ADAPTER_WEBUI_ENABLE is webui_before  # не изменилось

    def test_wrong_type_not_applied(self):
        """Wrong type for known key is not applied; other keys still apply."""
        debug_before = self.config.ADAPTER_DEBUG
        result = self.config.set_runtime_config(
            ADAPTER_DEBUG="not-a-bool",  # неверный тип
            ADAPTER_DEBUG_TAGS_OUT=True,  # верный тип
        )
        assert result["ADAPTER_DEBUG"] is debug_before  # осталось прежнее значение
        assert result["ADAPTER_DEBUG_TAGS_OUT"] is True  # применилось

    def test_bool_not_passed_as_int(self):
        """Bool is checked BEFORE int — bool doesn't pass as int field."""
        result = self.config.set_runtime_config(
            ADAPTER_TRACE_REASONING_MAX_CHARS=True  # bool, а не int
        )
        # Не применилось (int-поле отклоняет bool)
        assert result["ADAPTER_TRACE_REASONING_MAX_CHARS"] == 0  # дефолт

    def test_return_value_matches_sent(self):
        """Return value reflects actual values after application."""
        result = self.config.set_runtime_config(
            ADAPTER_DEBUG=False,
            ADAPTER_DEBUG_TRIM=1000,
        )
        assert result["ADAPTER_DEBUG"] is False
        assert result["ADAPTER_DEBUG_TRIM"] == 1000
        # Возвращает актуальные значения (могли отличаться от посланных, если что-то отклонилось)

    def test_pool_not_extended(self):
        """Return value has exactly 7 keys from RUNTIME_CONFIG_POOL."""
        result = self.config.set_runtime_config(ADAPTER_DEBUG=False)
        assert len(result) == 7
        assert set(result.keys()) == set(self.config.RUNTIME_CONFIG_POOL)
