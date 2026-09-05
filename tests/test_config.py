"""Tests for backend_adapter.config — env var parsing, model mapping, backends."""
import os
import json
import importlib
import sys
import threading
import time
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
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["errors"] == {}
        # refresh_models дополнительно несёт результат пробы эндпойнтов
        assert "probe" in result
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


class TestBackgroundRefresh:
    """Tests for the background refresh worker (start_refresh/refresh_state).

    Contract: start_refresh() runs refresh_models in a daemon thread and
    returns True only when *this* call started the check (False while one is
    already running); refresh_state() is a copy of the immutable snapshot;
    the worker always publishes running=False on completion (even on
    exception)."""

    def _config(self):
        _reload_config()
        from backend_adapter import config

        config._REFRESH_JOB = None
        return config

    def _wait_for(self, cond, timeout=5.0):
        """Дождаться cond()==True (поток-воркер завершает асинхронно)."""
        deadline = time.monotonic() + timeout
        while not cond():
            assert time.monotonic() < deadline, "таймаут ожидания воркера"
            time.sleep(0.01)

    def test_initial_state_defaults(self):
        config = self._config()
        state = config.refresh_state()
        assert state["running"] is False
        assert state["started_at"] is None
        assert state["done_at"] is None
        assert state["ok"] is None
        assert state["count"] is None
        assert state["errors"] is None
        assert state["checked_at"] is None

    def test_state_is_a_copy(self):
        config = self._config()
        config._REFRESH_JOB = {"running": False, "ok": True, "x": 1}
        state = config.refresh_state()
        state["ok"] = False
        state["x"] = 999
        assert config._REFRESH_JOB["ok"] is True
        assert config._REFRESH_JOB["x"] == 1

    def test_start_runs_refresh_models_in_background(self):
        config = self._config()
        config._BACKENDS = [{"name": "AAA", "base": "http://aaa", "key": "k"}]
        config._BACKEND_BY_NAME = {"AAA": config._BACKENDS[0]}
        config._DEFAULT_BACKEND = config._BACKENDS[0]
        result = {"ok": True, "count": 3, "errors": {}, "probe": {}}
        with mock.patch.object(config, "refresh_models", return_value=result) as m_refresh:
            assert config.start_refresh(timeout=5.0) is True
            # вернулись сразу — воркер в фоне
            self._wait_for(lambda: m_refresh.called)
            self._wait_for(lambda: not config.refresh_state()["running"])
        state = config.refresh_state()
        assert m_refresh.call_args.kwargs.get("timeout") == 5.0
        assert state["ok"] is True
        assert state["count"] == 3
        assert state["errors"] == {}
        assert state["running"] is False
        assert state["done_at"] is not None
        assert state["checked_at"] == time.strftime("%H:%M:%S")

    def test_second_start_while_running_returns_false(self):
        config = self._config()
        started = threading.Event()
        release = threading.Event()

        def blocking_refresh(**kwargs):
            started.set()
            release.wait(5)

        with mock.patch.object(config, "refresh_models", side_effect=blocking_refresh):
            assert config.start_refresh() is True
            assert started.wait(5) is True  # воркер вошёл в refresh_models
            # пока проверка идёт — повторный запуск не создаёт поток
            assert config.refresh_state()["running"] is True
            assert config.start_refresh() is False
            assert config.start_refresh() is False
            release.set()
        self._wait_for(lambda: not config.refresh_state()["running"])

    def test_worker_publishes_failure_on_exception(self):
        config = self._config()
        with mock.patch.object(
            config,
            "refresh_models",
            side_effect=RuntimeError("boom from test"),
        ):
            assert config.start_refresh() is True
            self._wait_for(lambda: not config.refresh_state()["running"])
        state = config.refresh_state()
        assert state["ok"] is False
        assert "__worker__" in (state["errors"] or {})
        assert "boom from test" in state["errors"]["__worker__"]

    def test_worker_publishes_ok_false_result(self):
        # refresh_models вернул ok=False (полный провал — кэш не тронут):
        # воркер публикует это как итог, не как исключение.
        config = self._config()
        config._AVAILABLE_MODELS["old-m"] = {"id": "old-m"}
        result = {"ok": False, "count": 1, "errors": {"AAA": "down"}, "probe": {}}
        with mock.patch.object(config, "refresh_models", return_value=result):
            assert config.start_refresh() is True
            self._wait_for(lambda: not config.refresh_state()["running"])
        state = config.refresh_state()
        assert state["ok"] is False
        assert state["count"] == 1
        assert state["errors"] == {"AAA": "down"}


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


class TestEndpointProbe:
    """Tests for the «smoke» API-endpoint probe (v0.8.2).

    probe_endpoints() must: use per-endpoint models from the YAML ``probe``
    key (default model otherwise), skip ONLY endpoints whose probe-model is
    absent from the backend /v1/models, classify responses by HTTP code
    (found=True only for 200; any other code and 404 → not found; network →
    backend error), cache results for ENDPOINT_PROBE_TTL (second call within
    TTL → no network), log one [ENDPOINT_PROBE] line per real probe, and
    honor ADAPTER_ENDPOINT_PROBE=0 (no network at all).
    """

    # -- Helpers -----------------------------------------------------------

    def _setup(self, backend=None, models=None, probe_enabled=True):
        """Fresh config + one backend in globals (+ models in _MODEL_TO_BACKEND).

        Возвращает конфиг с записью бэкенда, которая уже «опрошена» на
        /v1/models: в _MODEL_TO_BACKEND лежат модели ``models`` (список id).
        backend=None → запись без ключа probe; probe_enabled=False → флаг
        ADAPTER_ENDPOINT_PROBE выключен (как при env-значении 0)."""
        _reload_config()
        from backend_adapter import config
        if backend is None:
            backend = {"name": "AAA", "base": "http://aaa.local", "key": "k-aaa"}
        config._BACKENDS = [backend]
        config._BACKEND_BY_NAME = {backend["name"]: backend}
        config._DEFAULT_BACKEND = backend
        for mid in models or []:
            config._MODEL_TO_BACKEND[mid] = (backend["name"], backend)
        config.ADAPTER_ENDPOINT_PROBE = probe_enabled
        return config

    def _probe_ok(self, *paths, status=200):
        """side_effect для _http_json: (status, {}, None) по любому пути."""
        return [(status, {}, None)] * len(paths)

    def _assert_all_probed(self, m_http, paths):
        """Все пути из ENDPOINT_PROBES реально пробованы (по одному разу)."""
        assert m_http.call_count == len(paths)
        urls = [c.args[1] for c in m_http.call_args_list]
        for path in paths:
            assert any(u.endswith(path) for u in urls), f"{path} not probed: {urls}"

    # -- _parse_backend_yaml: ключ probe ----------------------------------

    def test_yaml_probe_key_parsed(self, tmp_path):
        yaml_content = """backend:
  - name: llm-service
    base: https://llm.service.example.com
    key: ADAPTER_BACKEND_KEY_LLM_SERVICE
    probe:
      - completions: qwen3.6
      - messages:
      - responses: gpt-5-sol
      - embeddings: text-embedding-3-small
"""
        yaml_file = tmp_path / "probe.yaml"
        yaml_file.write_text(yaml_content)
        _reload_config()
        from backend_adapter import config
        result = config._parse_backend_yaml(str(yaml_file))
        assert result is not None
        assert result[0]["probe"] == {
            "completions": "qwen3.6",
            "messages": "",
            "responses": "gpt-5-sol",
            "embeddings": "text-embedding-3-small",
        }

    def test_yaml_without_probe_valid(self, tmp_path):
        yaml_content = """backend:
  - name: AAA
    base: http://aaa.local
    key: KEY
"""
        yaml_file = tmp_path / "noprobe.yaml"
        yaml_file.write_text(yaml_content)
        _reload_config()
        from backend_adapter import config
        result = config._parse_backend_yaml(str(yaml_file))
        assert result is not None
        assert "probe" not in result[0]

    def test_yaml_probe_unknown_names_kept(self, tmp_path):
        # Имена, которых нет в ENDPOINT_PROBES, парсинг не ломают — они
        # отсекаются на этапе выбора модели (в пробе не участвуют).
        yaml_content = """backend:
  - name: AAA
    base: http://aaa.local
    key: KEY
    probe:
      - completions: m1
      - strange-name: m2
"""
        yaml_file = tmp_path / "strange.yaml"
        yaml_file.write_text(yaml_content)
        _reload_config()
        from backend_adapter import config
        result = config._parse_backend_yaml(str(yaml_file))
        assert result is not None
        assert result[0]["probe"] == {"completions": "m1", "strange-name": "m2"}

    # -- Выбор probe-модели -----------------------------------------------

    def test_probe_model_explicit_and_default(self):
        cfg = self._setup(
            backend={"name": "AAA", "base": "http://aaa", "key": "k",
                     "probe": {"completions": "explicit", "messages": ""}},
            models=["def-model", "explicit"],
        )
        # Перечислен с моделью (и модель есть в /v1/models) → эта модель.
        assert cfg._probe_model(cfg._BACKENDS[0], "completions") == ("explicit", True)
        # Пустое значение (messages:) и неперечисленные эндпойнты —
        # моделью по умолчанию (первая из /v1/models этого бэкенда).
        assert cfg._probe_model(cfg._BACKENDS[0], "messages") == ("def-model", None)
        assert cfg._probe_model(cfg._BACKENDS[0], "responses") == ("def-model", None)

    def test_probe_model_specified_but_missing(self):
        # Модель задана в probe, но её НЕТ среди моделей бэкенда → (None,
        # False): эндпоинт пропускается точечно, с текстом в errors.
        cfg = self._setup(
            backend={"name": "AAA", "base": "http://aaa", "key": "k",
                     "probe": {"completions": "ghost-model"}},
            models=["def-model"],
        )
        assert cfg._probe_model(cfg._BACKENDS[0], "completions") == (None, False)
        # Не перечисленные эндпойнты — моделью по умолчанию (не задеты)
        assert cfg._probe_model(cfg._BACKENDS[0], "messages") == ("def-model", None)

    def test_probe_model_no_probe_key_default(self):
        cfg = self._setup(models=["m1", "m2"])
        assert cfg._probe_model(cfg._BACKENDS[0], "completions") == ("m1", None)
        assert cfg._probe_model(cfg._BACKENDS[0], "embeddings") == ("m1", None)

    def test_probe_model_no_default_returns_none(self):
        cfg = self._setup()
        assert cfg._probe_model(cfg._BACKENDS[0], "completions") == (None, None)

    # -- Классификация HTTP-кодов ------------------------------------------

    def test_classification_200_404_400(self):
        cfg = self._setup(models=["m"])
        codes = {
            "http://aaa.local/v1/chat/completions": 200,   # completions — работает
            "http://aaa.local/v1/messages": 400,           # messages — ключ/тело не подошли
            "http://aaa.local/v1/responses": 404,          # responses — не реализован
            "http://aaa.local/v1/embeddings": 501,         # embeddings — 5xx, тоже не работает
        }
        def fake_http(method, url, headers, body, timeout):
            return codes.get(url, 404), {}, None
        with mock.patch.object(cfg, "_http_json", side_effect=fake_http) as m_http:
            res = cfg._probe_backend_endpoints(cfg._BACKENDS[0],
                                               {p: "m" for _, p, _ in cfg.ENDPOINT_PROBES},
                                               timeout=None)
        eps = res["endpoints"]
        # found=True ТОЛЬКО для HTTP 200; любой не-200 код (400, 404, 501) — False.
        assert eps["/v1/chat/completions"] == {"status": 200, "found": True}
        assert eps["/v1/messages"] == {"status": 400, "found": False}
        assert eps["/v1/responses"] == {"status": 404, "found": False}
        assert eps["/v1/embeddings"] == {"status": 501, "found": False}
        assert res["errors"] == {}
        self._assert_all_probed(m_http, codes)

    def test_classification_non_200_4xx_5xx_not_found(self):
        cfg = self._setup(models=["m"])
        codes = {
            "http://aaa.local/v1/chat/completions": 401,
            "http://aaa.local/v1/messages": 405,
            "http://aaa.local/v1/responses": 500,
            "http://aaa.local/v1/embeddings": 503,
        }
        def fake_http(method, url, headers, body, timeout):
            return codes.get(url, 404), {}, None
        with mock.patch.object(cfg, "_http_json", side_effect=fake_http):
            res = cfg._probe_backend_endpoints(cfg._BACKENDS[0],
                                               {p: "m" for _, p, _ in cfg.ENDPOINT_PROBES},
                                               timeout=None)
        eps = res["endpoints"]
        assert all(e["found"] is False for e in eps.values())
        assert eps["/v1/chat/completions"] == {"status": 401, "found": False}
        assert eps["/v1/messages"] == {"status": 405, "found": False}
        assert eps["/v1/responses"] == {"status": 500, "found": False}
        assert eps["/v1/embeddings"] == {"status": 503, "found": False}

    def test_network_error_classified_backend_error(self):
        cfg = self._setup(models=["m"])
        def fake_http(method, url, headers, body, timeout):
            return None, None, "Connection refused by test"
        with mock.patch.object(cfg, "_http_json", side_effect=fake_http) as m_http:
            res = cfg._probe_backend_endpoints(cfg._BACKENDS[0],
                                               {p: "m" for _, p, _ in cfg.ENDPOINT_PROBES},
                                               timeout=None)
        # Сетевая ошибка: found=False (не «работает») + текст ошибки в errors.
        assert res["errors"] != {}
        assert all(e["found"] is False for e in res["endpoints"].values())
        assert all(e["status"] is None for e in res["endpoints"].values())
        assert m_http.call_count == 4

    # -- Тело запроса (максимально 1 токен; [AN]-заголовок) ------------------

    def test_body_max_tokens_and_anthropic_header(self):
        cfg = self._setup(models=["m"])
        captured = []
        def fake_http(method, url, headers, body, timeout):
            captured.append((url, headers, body))
            return 200, {}, None
        with mock.patch.object(cfg, "_http_json", side_effect=fake_http):
            cfg._probe_backend_endpoints(cfg._BACKENDS[0],
                                         {p: "m" for _, p, _ in cfg.ENDPOINT_PROBES},
                                         timeout=None)
        bodies = {u: b for u, _h, b in captured}
        # completions: {model, messages, max_tokens:1}
        assert bodies["http://aaa.local/v1/chat/completions"]["max_tokens"] == 1
        # messages: тот же шаблон + [AN]-версия в заголовках
        msgs = bodies["http://aaa.local/v1/messages"]
        assert msgs["max_tokens"] == 1
        mh = {u: h for u, h, _b in captured}
        assert mh["http://aaa.local/v1/messages"]["anthropic-version"] == "2023-06-01"
        # responses: max_output_tokens:1 (а не max_tokens)
        assert bodies["http://aaa.local/v1/responses"]["max_output_tokens"] == 1

    # -- probe_endpoints: интеграция ---------------------------------------

    def test_probe_endpoints_all_probed_with_default_model(self):
        cfg = self._setup(models=["def-model"])
        # Кэш пуст → реальная проба всеми 4 эндпойнтами моделью по умолчанию.
        with mock.patch.object(cfg, "_http_json", return_value=(404, {}, None)) as m_http:
            res = cfg.probe_endpoints(timeout=5.0)
        assert res["ok"] is True
        state = res["endpoints"]["AAA"]["endpoints"]
        assert len(state) == 4
        assert all(v["found"] is False for v in state.values())
        self._assert_all_probed(m_http, ["/v1/chat/completions", "/v1/messages",
                                         "/v1/responses", "/v1/embeddings"])
        # Модель по умолчанию ушла в каждый запрос
        for c in m_http.call_args_list:
            assert c.args[3]["model"] == "def-model"

    def test_probe_endpoints_uses_yaml_probe_models(self):
        # Каждый эндпойнт пробуется СВОЕЙ моделью из probe-ключа.
        cfg = self._setup(
            backend={"name": "AAA", "base": "http://aaa", "key": "k",
                     "probe": {
                         "completions": "qwen3.6",
                         "responses": "gpt-5-sol",
                         "embeddings": "text-embedding-3-small",
                     }},
            models=["def-model", "qwen3.6", "gpt-5-sol", "text-embedding-3-small"],
        )
        with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)) as m_http:
            cfg.probe_endpoints()
        bodies = {c.args[1]: c.args[3] for c in m_http.call_args_list}
        assert bodies["http://aaa/v1/chat/completions"]["model"] == "qwen3.6"
        assert bodies["http://aaa/v1/responses"]["model"] == "gpt-5-sol"
        assert bodies["http://aaa/v1/embeddings"]["model"] == "text-embedding-3-small"
        # messages не перечислен (или пуст) → модель по умолчанию
        assert bodies["http://aaa/v1/messages"]["model"] == "def-model"

    def test_probe_skips_single_endpoint_with_missing_model(self):
        # Модель для messages задана в probe, но её НЕТ среди /v1/models
        # бэкенда — пропускается ТОЛЬКО messages, остальные пробуются.
        cfg = self._setup(
            backend={"name": "AAA", "base": "http://aaa", "key": "k",
                     "probe": {"messages": "ghost-model"}},
            models=["def-model"],
        )
        with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)) as m_http:
            res = cfg.probe_endpoints()
        urls = [c.args[1] for c in m_http.call_args_list]
        assert "/v1/messages" not in urls            # этот эндпоинт не пробован
        assert len(urls) == 3                        # остальные — да
        # Пропущенный эндпоинт НЕ в результатах, текст — в errors
        state = res["endpoints"]["AAA"]["endpoints"]
        assert "/v1/messages" not in state
        assert "messages" in res["errors"]["AAA"]
        assert "ghost-model" in res["errors"]["AAA"]

    def test_probe_backend_without_models_skipped_entirely(self):
        # У бэкенда нет ни одной модели в кэше (упал на /v1/models):
        # пробовать нечем — весь бэкенд пропущен, _http_json не вызван.
        cfg = self._setup()
        with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)) as m_http:
            res = cfg.probe_endpoints()
        assert m_http.call_count == 0
        assert "AAA" in res["errors"]
        assert "no models available" in res["errors"]["AAA"]
        assert "AAA" not in res["endpoints"]

    # -- Кэш (TTL) ----------------------------------------------------------

    def test_cache_second_call_within_ttl_no_network(self):
        cfg = self._setup(models=["m"])
        with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)) as m_http:
            first = cfg.probe_endpoints()
            second = cfg.probe_endpoints()
        assert first["ok"] is True
        assert m_http.call_count == 4          # повторный вызов — кэш, без сети
        assert second["endpoints"]["AAA"]["endpoints"] == first["endpoints"]["AAA"]["endpoints"]

    # -- refresh_models: ключ "probe" и флаг ADAPTER_ENDPOINT_PROBE ----------

    def test_refresh_returns_probe_key(self):
        cfg = self._setup(models=["m"])
        cfg.ADAPTER_ENDPOINT_PROBE = True
        with mock.patch.object(cfg, "_fetch_models", return_value=[{"id": "m"}]):
            with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)):
                result = cfg.refresh_models()
        assert result["ok"] is True
        assert "probe" in result
        assert result["probe"]["ok"] is True
        assert result["probe"]["endpoints"]["AAA"]["endpoints"] != {}

    def test_refresh_with_probe_disabled_still_refreshes_models(self):
        cfg = self._setup(models=["m"], probe_enabled=False)
        with mock.patch.object(cfg, "_fetch_models", return_value=[{"id": "fresh-m"}]):
            with mock.patch.object(cfg, "_http_json") as m_http:
                result = cfg.refresh_models()
        assert result["ok"] is True            # модели обновились как обычно
        assert "probe" in result
        m_http.assert_not_called()             # но проба эндпойнтов — нет

    def test_endpoint_probe_disabled_no_network(self):
        cfg = self._setup(models=["m"], probe_enabled=False)
        with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)) as m_http:
            res = cfg.probe_endpoints()
        assert res["ok"] is False
        assert res["endpoints"] == {}
        m_http.assert_not_called()             # в сеть не ходили

    # -- Логирование [ENDPOINT_PROBE] ---------------------------------------

    def test_log_line_emitted_on_real_probe(self, capsys):
        cfg = self._setup(models=["m"])
        cfg.ADAPTER_DEBUG = True          # гейт лог-строки — как в проде
        with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)):
            cfg.probe_endpoints()
        out = capsys.readouterr().out
        assert "[ENDPOINT_PROBE] backend 'AAA'" in out
        assert "completions=200" in out

    def test_no_log_line_on_cache_hit(self, capsys):
        cfg = self._setup(models=["m"])
        cfg.ADAPTER_DEBUG = True          # первый вызов обязан залогироваться
        with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)):
            cfg.probe_endpoints()
            first_out = capsys.readouterr().out
            assert "[ENDPOINT_PROBE]" in first_out   # фактическая проба — есть
            cfg.probe_endpoints()              # кэш-хит — строки быть НЕ должно
        out = capsys.readouterr().out
        assert "[ENDPOINT_PROBE]" not in out

    def test_no_log_line_when_debug_disabled(self, capsys):
        cfg = self._setup(models=["m"])
        cfg.ADAPTER_DEBUG = False
        with mock.patch.object(cfg, "_http_json", return_value=(200, {}, None)):
            cfg.probe_endpoints()
        assert "[ENDPOINT_PROBE]" not in capsys.readouterr().out

    # -- Интеграция с fake_backend (реальный HTTP) ----------------------------

    def test_probe_against_fake_backend(self, fake_backend):
        # Реальная сеть: fake_backend отвечает на POST /v1/chat/completions
        # (completions_status=200), остальные пути — 404, пока не заданы
        # extra_post_paths. Проба находит только completions.
        _reload_config()
        from backend_adapter import config
        cfg = config
        cfg.ADAPTER_ENDPOINT_PROBE = True
        fake_backend.serve()             # фикстура только создаёт; стартуем сами
        fake_backend.models_response = {"object": "list", "data": [{"id": "m1"}]}
        fake_backend.completions_status = 200
        fake_backend.extra_post_paths = {}
        backend = {"name": "AAA", "base": fake_backend.base_url, "key": "k"}
        cfg._BACKENDS = [backend]
        cfg._BACKEND_BY_NAME = {"AAA": backend}
        cfg._DEFAULT_BACKEND = backend
        cfg._MODEL_TO_BACKEND["m1"] = ("AAA", backend)

        with mock.patch.object(cfg, "_fetch_models", return_value=[{"id": "m1"}]):
            result = cfg.probe_endpoints(timeout=5.0)

        state = result["endpoints"]["AAA"]["endpoints"]
        assert state["/v1/chat/completions"] == {"status": 200, "found": True}
        assert state["/v1/messages"]["found"] is False       # 404
        assert state["/v1/responses"]["found"] is False
        assert state["/v1/embeddings"]["found"] is False

    def test_probe_extra_paths_responses_200(self, fake_backend):
        # Эндпоинты из extra_post_paths отвечают настроенным статусом —
        # проба находит /v1/responses и /v1/embeddings.
        _reload_config()
        from backend_adapter import config
        cfg = config
        cfg.ADAPTER_ENDPOINT_PROBE = True
        fake_backend.serve()             # фикстура только создаёт; стартуем сами
        fake_backend.models_response = {"object": "list", "data": [{"id": "m1"}]}
        fake_backend.extra_post_paths = {
            "/v1/responses": 200,
            "/v1/embeddings": 200,
        }
        backend = {"name": "AAA", "base": fake_backend.base_url, "key": "k"}
        cfg._BACKENDS = [backend]
        cfg._BACKEND_BY_NAME = {"AAA": backend}
        cfg._DEFAULT_BACKEND = backend
        cfg._MODEL_TO_BACKEND["m1"] = ("AAA", backend)

        with mock.patch.object(cfg, "_fetch_models", return_value=[{"id": "m1"}]):
            result = cfg.probe_endpoints(timeout=5.0)

        state = result["endpoints"]["AAA"]["endpoints"]
        assert state["/v1/responses"] == {"status": 200, "found": True}
        assert state["/v1/embeddings"] == {"status": 200, "found": True}
        assert state["/v1/messages"]["found"] is False       # 404 (не настроен)
        assert state["/v1/chat/completions"]["found"] is True  # 200 от handler
