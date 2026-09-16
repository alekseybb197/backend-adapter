"""Tests for backend_adapter.config — env var parsing, model mapping, backends."""
import os
import json
import importlib
import sys
import threading
import time
from unittest import mock

import pytest


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
    """Tests for trim_limit() (v0.8.6-реформа: консольный лимит обрезки,
    без тега — файловый канал полный; 0 = без обрезки)."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_trim_on(self):
        """TRIM=N → возвращает N (консоль обрезается до N символов)."""
        self.config.ADAPTER_DEBUG_TRIM = 100
        assert self.config.trim_limit() == 100

    def test_trim_off_zero(self):
        """TRIM=0 → «без обрезки» (0 = выкл., не ошибка конфигурации)."""
        self.config.ADAPTER_DEBUG_TRIM = 0
        assert self.config.trim_limit() == 0

    def test_default(self):
        """Env не задан → дефолт ADAPTER_DEBUG_TRIM=3000."""
        assert self.config.trim_limit() == 3000


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
    """Default flags for the zero-config run (v0.8.6).

    With env vars *absent* the adapter should: NOT write *.parts дампы
    (ADAPTER_DEBUG_PARTS=0), NOT write log files (ADAPTER_DEBUG_ENABLE=0) and
    keep the WEBUI always up (флага отключения больше нет — см. v0.8.6).
    Both parse off-words incl. "" — so asserting the default requires delenv,
    NOT setenv("", ...) (an empty env value parses as False for both)."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_parts_defaults_false(self, monkeypatch):
        """ADAPTER_DEBUG_PARTS unset → disabled (no per-session parts dumps)."""
        monkeypatch.delenv("ADAPTER_DEBUG_PARTS", raising=False)
        _reload_config()
        from backend_adapter import config
        assert config.ADAPTER_DEBUG_PARTS is False


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

    def test_invalid_base_url_skipped(self, tmp_path, capsys):
        # base без http(s):// — запись пропускается с [WARN] (v0.9.6):
        # иначе бэкенд с мусорным адресом падал бы позже на urlopen.
        yaml_content = """backend:
  - name: home
    base: localhost:8002
    key: KEY1
  - name: good
    base: http://localhost:8003
    key: KEY2
"""
        yaml_file = tmp_path / "badurl.yaml"
        yaml_file.write_text(yaml_content)

        _reload_config()
        from backend_adapter import config
        os.environ["KEY1"] = "v1"
        os.environ["KEY2"] = "v2"
        result = config._parse_backend_yaml(str(yaml_file))

        assert result is not None
        assert [b["name"] for b in result] == ["good"]
        out = capsys.readouterr().out
        assert "[WARN]" in out and "localhost:8002" in out

    def test_missing_fields_warn(self, tmp_path, capsys):
        # Нет key — запись пропускается с [WARN] и названным полем.
        yaml_content = """backend:
  - name: nokey
    base: http://localhost:8002
"""
        yaml_file = tmp_path / "nofield.yaml"
        yaml_file.write_text(yaml_content)

        _reload_config()
        from backend_adapter import config
        result = config._parse_backend_yaml(str(yaml_file))

        assert result is None  # нет валидных записей
        out = capsys.readouterr().out
        assert "[WARN]" in out and "nokey" in out and "key" in out


    def test_probe_key_ignored(self, tmp_path):
        """v0.9.9: ключ probe в YAML больше не разбирается — молча игнорируется."""
        yaml_content = """backend:
  - name: home
    base: http://localhost:8002
    key: KEY1
    probe:
      completions: qwen3.6
      messages: claude-m
"""
        yaml_file = tmp_path / "probe.yaml"
        yaml_file.write_text(yaml_content)

        _reload_config()
        from backend_adapter import config
        os.environ["KEY1"] = "val"
        result = config._parse_backend_yaml(str(yaml_file))
        assert result is not None
        assert len(result) == 1
        assert result[0]["name"] == "home"
        assert "probe" not in result[0]


class TestIsHttpUrl:
    """_is_http_url: base бэкенда обязан быть http(s)-URL (v0.9.6)."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    @pytest.mark.parametrize(
        "value",
        [
            "http://localhost:8002",
            "https://litellm.example.com",
            "https://api.example.com/v1",
            "HTTPS://EXAMPLE.COM",
            "  http://x.local  ",
        ],
    )
    def test_accepts(self, value):
        assert self.config._is_http_url(value) is True

    @pytest.mark.parametrize(
        "value",
        ["localhost:8002", "ftp://x.local", "example.com", "", "http://", "://x"],
    )
    def test_rejects(self, value):
        assert self.config._is_http_url(value) is False


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

    def test_init_multi_backends_mutates_backends_list_in_place(self, tmp_path):
        """v0.9.8: список/словарь БЭКЕНДОВ тоже мутируются на месте.

        Точка входа (``backend-adapter.py``) делает
        ``from backend_adapter.config import _BACKENDS`` и печатает стартовый
        баннер; переприсваивание ``_BACKENDS = blocks`` оставляло ей исходный
        ПУСТОЙ список — баннер всегда показывал «0 configured». Ссылки на
        объекты (как у любого импортёра-по-значению) обязаны остаться живыми.
        """
        yaml_file = tmp_path / "init.yaml"
        yaml_file.write_text("""backend:
  - name: home
    base: http://home
    key: k
  - name: work
    base: http://work
    key: k
""")
        _reload_config()
        from backend_adapter import config

        entrypoint_view_backends = config._BACKENDS
        entrypoint_view_by_name = config._BACKEND_BY_NAME

        with mock.patch.object(config, "_fetch_models", return_value=[{"id": "m1"}]):
            config._init_multi_backends(str(yaml_file))

        assert config._BACKENDS is entrypoint_view_backends
        assert config._BACKEND_BY_NAME is entrypoint_view_by_name
        assert [b["name"] for b in entrypoint_view_backends] == ["home", "work"]
        assert set(entrypoint_view_by_name) == {"home", "work"}


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
        result = {"ok": True, "count": 3, "errors": {}}
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
        result = {"ok": False, "count": 1, "errors": {"AAA": "down"}}
        with mock.patch.object(config, "refresh_models", return_value=result):
            assert config.start_refresh() is True
            self._wait_for(lambda: not config.refresh_state()["running"])
        state = config.refresh_state()
        assert state["ok"] is False
        assert state["count"] == 1
        assert state["errors"] == {"AAA": "down"}

    def test_start_refresh_reloads_config_by_default(self, tmp_path):
        # start_refresh(reload=True — дефолт) перед проверкой перечитывает
        # ADAPTER_BACKEND_CONFIG; reload_backend_config мокается — проверяем
        # сам факт вызова и WARN при None (битый файл), когда проверка всё
        # равно идёт по прежним бэкендам.
        config = self._config()
        config.ADAPTER_BACKEND_CONFIG = str(tmp_path / "adapter.yaml")
        config._BACKENDS = [{"name": "AAA", "base": "http://aaa", "key": "k"}]
        result = {"ok": True, "count": 1, "errors": {}}
        with mock.patch.object(config, "refresh_models", return_value=result):
            with mock.patch.object(
                config, "reload_backend_config", return_value=None
            ) as m_reload:
                assert config.start_refresh(timeout=5.0) is True
                self._wait_for(lambda: not config.refresh_state()["running"])
        m_reload.assert_called_once_with()
        state = config.refresh_state()
        assert state["ok"] is True
        assert state["count"] == 1
        # providers в финальном снимке — число бэкендов после перечитывания
        assert state["providers"] == 1

    def test_start_refresh_reload_false_skips_reload(self, tmp_path):
        config = self._config()
        config.ADAPTER_BACKEND_CONFIG = str(tmp_path / "adapter.yaml")
        result = {"ok": False, "count": 0, "errors": {}}
        with mock.patch.object(config, "refresh_models", return_value=result):
            with mock.patch.object(config, "reload_backend_config") as m_reload:
                assert config.start_refresh(reload=False) is True
                self._wait_for(lambda: not config.refresh_state()["running"])
        m_reload.assert_not_called()


class TestReloadBackendConfig:
    """Tests for config.reload_backend_config — перечитывание конфига на лету.

    Contract: reload parses ADAPTER_BACKEND_CONFIG and swaps the backend
    globals (_BACKENDS/_BACKEND_BY_NAME/_DEFAULT_BACKEND), leaves
    _AVAILABLE_MODELS/_MODEL_TO_BACKEND untouched (refresh_models rebuilds
    them), returns the new blocks, or None on broken/unreadable/empty file
    (backend globals untouched)."""

    def _config(self):
        _reload_config()
        from backend_adapter import config

        config._REFRESH_JOB = None
        return config

    @staticmethod
    def _yaml(blocks: str) -> str:
        return f"backend:\n{blocks}"

    def test_reload_swaps_globals(self, tmp_path):
        config = self._config()
        cfg_file = tmp_path / "adapter.yaml"
        cfg_file.write_text(self._yaml(
            "  - name: AAA\n    base: http://aaa\n    key: K1\n"
        ))
        old = {"name": "OLD", "base": "http://old", "key": "K0"}
        config._BACKENDS = [old]
        config._BACKEND_BY_NAME = {"OLD": old}
        config._DEFAULT_BACKEND = old
        config.ADAPTER_BACKEND_CONFIG = str(cfg_file)

        blocks = config.reload_backend_config()
        assert blocks is not None
        assert [b["name"] for b in blocks] == ["AAA"]
        assert [b["name"] for b in config._BACKENDS] == ["AAA"]
        assert set(config._BACKEND_BY_NAME) == {"AAA"}
        assert config._DEFAULT_BACKEND["name"] == "AAA"

    def test_reload_multiple_backends_sets_default_first(self, tmp_path):
        config = self._config()
        cfg_file = tmp_path / "adapter.yaml"
        cfg_file.write_text(self._yaml(
            "  - name: AAA\n    base: http://aaa\n    key: K1\n"
            "  - name: BBB\n    base: http://bbb\n    key: K2\n"
        ))
        config.ADAPTER_BACKEND_CONFIG = str(cfg_file)
        blocks = config.reload_backend_config()
        assert [b["name"] for b in blocks] == ["AAA", "BBB"]
        assert config._DEFAULT_BACKEND["name"] == "AAA"

    def test_reload_keeps_models_index(self, tmp_path):
        # reload НЕ трогает модели/индексы — их пересоберёт refresh_models.
        config = self._config()
        cfg_file = tmp_path / "adapter.yaml"
        cfg_file.write_text(self._yaml(
            "  - name: AAA\n    base: http://aaa\n    key: K1\n"
        ))
        config.ADAPTER_BACKEND_CONFIG = str(cfg_file)
        config._AVAILABLE_MODELS["m1"] = {"id": "m1"}
        config._MODEL_TO_BACKEND["m1"] = ("OLD", {"name": "OLD"})

        config.reload_backend_config()
        assert config._AVAILABLE_MODELS == {"m1": {"id": "m1"}}
        assert config._MODEL_TO_BACKEND["m1"][0] == "OLD"

    def test_reload_broken_file_returns_none_keeps_backends(self, tmp_path):
        # Битый/недоступный YAML → None, прежние глобалы не тронуты.
        config = self._config()
        old = {"name": "AAA", "base": "http://aaa", "key": "k"}
        config._BACKENDS = [old]
        config._BACKEND_BY_NAME = {"AAA": old}
        config._DEFAULT_BACKEND = old
        config.ADAPTER_BACKEND_CONFIG = str(tmp_path / "missing.yaml")

        assert config.reload_backend_config() is None
        assert config._BACKENDS == [old]
        assert config._BACKEND_BY_NAME == {"AAA": old}

    def test_reload_empty_backend_key_returns_none(self, tmp_path):
        # Файл есть, но ключа backend нет / блоков нет — None.
        config = self._config()
        cfg_file = tmp_path / "adapter.yaml"
        cfg_file.write_text("other: 1\n")
        config.ADAPTER_BACKEND_CONFIG = str(cfg_file)
        assert config.reload_backend_config() is None

    def test_start_refresh_reload_swaps_backends(self, tmp_path):
        # Сквозной сценарий: start_refresh(reload=True) с реальным YAML —
        # глобалы подменяются ДО фоновой проверки (refresh_models видит
        # новые бэкенды).
        config = self._config()
        cfg_file = tmp_path / "adapter.yaml"
        cfg_file.write_text(self._yaml(
            "  - name: NEW\n    base: http://new\n    key: K1\n"
        ))
        config.ADAPTER_BACKEND_CONFIG = str(cfg_file)
        config._BACKENDS = [{"name": "OLD", "base": "http://old", "key": "K0"}]
        result = {"ok": True, "count": 1, "errors": {}}

        seen = {}

        def recording_refresh(**kwargs):
            seen["backends"] = [b["name"] for b in config._BACKENDS]
            return result

        with mock.patch.object(config, "refresh_models", side_effect=recording_refresh):
            assert config.start_refresh(timeout=5.0) is True
            deadline = time.monotonic() + 5.0
            while not config.refresh_state().get("done_at"):
                assert time.monotonic() < deadline, "таймаут воркера"
                time.sleep(0.01)
        assert seen["backends"] == ["NEW"]
        state = config.refresh_state()
        assert state["ok"] is True
        assert state["providers"] == 1


class TestModelsJsonWritePoints:
    """Точки записи JSON-файлов опроса моделей в LOGPATH (v0.9.0).

    .models.json пишется при каждой проверке бэкенда на список моделей:
    стартовой (_init_multi_backends) и фоновой (refresh_models) — успех и
    ошибка дают файл.
    """

    def _setup(self, backend=None, models=None):
        """Fresh config + один бэкенд в глобалах (+ модели в _MODEL_TO_BACKEND),
        LOGPATH → tmp_path (иначе файлы писались бы в ./tmp/logs репозитория)."""
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(self._tmp)
        _reload_config()
        from backend_adapter import config
        if backend is None:
            backend = {"name": "AAA", "base": "http://aaa", "key": "k"}
        config._BACKENDS = [backend]
        config._BACKEND_BY_NAME = {backend["name"]: backend}
        config._DEFAULT_BACKEND = backend
        for mid in models or []:
            config._MODEL_TO_BACKEND[mid] = (backend["name"], backend)
        return config

    def test_init_multi_backends_writes_models_json(self, tmp_path):
        self._tmp = tmp_path
        yaml_file = tmp_path / "init.yaml"
        yaml_file.write_text("""backend:
  - name: home
    base: http://home
    key: k
""")
        cfg = self._setup()
        with mock.patch.object(
            cfg, "_fetch_models",
            return_value=[{"id": "m1", "owned_by": "me"}, {"id": "m2"}],
        ):
            cfg._init_multi_backends(str(yaml_file))
        payload = json.loads((tmp_path / "home.models.json").read_text())
        assert payload["backend"] == "home"
        assert payload["ok"] is True
        assert payload["count"] == 2
        assert payload["models"] == [{"id": "m1", "owned_by": "me"}, {"id": "m2"}]
        assert "checked_at" in payload

    def test_init_multi_backends_failure_writes_error_json(self, tmp_path):
        self._tmp = tmp_path
        yaml_file = tmp_path / "init.yaml"
        yaml_file.write_text("""backend:
  - name: home
    base: http://home
    key: k
""")
        cfg = self._setup()
        with mock.patch.object(
            cfg, "_fetch_models", side_effect=OSError("Connection refused by test")
        ):
            with pytest.raises(SystemExit):
                # все бэкенды упали → [FATAL] после записи ошибки в файл
                cfg._init_multi_backends(str(yaml_file))
        payload = json.loads((tmp_path / "home.models.json").read_text())
        assert payload["ok"] is False
        assert "Connection refused by test" in payload["error"]

    def test_refresh_models_writes_models_json_per_backend(self, tmp_path):
        self._tmp = tmp_path
        cfg = self._setup()
        aaa = {"name": "AAA", "base": "http://aaa", "key": "k-aaa"}
        bbb = {"name": "BBB", "base": "http://bbb", "key": "k-bbb"}
        cfg._BACKENDS = [aaa, bbb]
        cfg._BACKEND_BY_NAME = {"AAA": aaa, "BBB": bbb}
        cfg._DEFAULT_BACKEND = aaa

        def fake_fetch(base, key, timeout=None):
            if "bbb" in base:
                raise OSError("Connection refused by test")
            return [{"id": "m-aaa"}]

        with mock.patch.object(cfg, "_fetch_models", side_effect=fake_fetch):
            cfg.refresh_models()
        ok_payload = json.loads((tmp_path / "AAA.models.json").read_text())
        assert ok_payload["ok"] is True
        assert ok_payload["models"] == [{"id": "m-aaa"}]
        err_payload = json.loads((tmp_path / "BBB.models.json").read_text())
        assert err_payload["ok"] is False
        assert "Connection refused by test" in err_payload["error"]

    def test_refresh_models_overwrites_models_json(self, tmp_path):
        """Каждая проверка перезаписывает .models.json целиком (файл один)."""
        self._tmp = tmp_path
        cfg = self._setup()
        with mock.patch.object(cfg, "_fetch_models", return_value=[{"id": "old"}]):
            cfg.refresh_models()
        with mock.patch.object(cfg, "_fetch_models", return_value=[{"id": "new"}]):
            cfg.refresh_models()
        files = [f.name for f in tmp_path.iterdir()]
        assert files == ["AAA.models.json"]
        payload = json.loads((tmp_path / "AAA.models.json").read_text())
        assert payload["models"] == [{"id": "new"}]


class TestRuntimeConfig:
    """Tests for runtime config pool — get/set via /config endpoint."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_valid_keys_applied(self):
        """Valid bool/int values are applied and visible in get_runtime_config().

        v0.9.6: Log и Parts заданы СОГЛАСОВАННО (Log=on, Parts=on) — иначе
        сработал бы каскад Parts→Log (Parts не может быть активен без Log).
        """
        result = self.config.set_runtime_config(
            ADAPTER_DEBUG=True,
            ADAPTER_DEBUG_PARTS=True,
            ADAPTER_TRACE_REASONING_MAX_CHARS=500,
        )
        assert result["ADAPTER_DEBUG"] is True
        assert result["ADAPTER_DEBUG_PARTS"] is True
        assert result["ADAPTER_TRACE_REASONING_MAX_CHARS"] == 500
        # Проверка через get
        current = self.config.get_runtime_config()
        assert current["ADAPTER_DEBUG"] is True
        assert current["ADAPTER_DEBUG_PARTS"] is True
        assert current["ADAPTER_TRACE_REASONING_MAX_CHARS"] == 500

    def test_new_bool_keys_applied(self):
        """Новые bool-переменные пула применяются и видны в get_runtime_config()."""
        result = self.config.set_runtime_config(
            ADAPTER_SENSITIVE_LOGGING_ENABLE=True,
            ADAPTER_STREAMING_ENABLE=False,
            ADAPTER_STREAM_INCLUDE_USAGE=False,
            ADAPTER_STRICT_MODELS=False,
        )
        assert result["ADAPTER_SENSITIVE_LOGGING_ENABLE"] is True
        assert result["ADAPTER_STREAMING_ENABLE"] is False
        assert result["ADAPTER_STREAM_INCLUDE_USAGE"] is False
        assert result["ADAPTER_STRICT_MODELS"] is False
        current = self.config.get_runtime_config()
        assert current["ADAPTER_SENSITIVE_LOGGING_ENABLE"] is True
        assert current["ADAPTER_STREAMING_ENABLE"] is False
        assert current["ADAPTER_STREAM_INCLUDE_USAGE"] is False
        assert current["ADAPTER_STRICT_MODELS"] is False

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
        # LOGPATH вне пула (неизменяемая на лету точка хранения) — игнорируется.
        logpath_before = self.config.ADAPTER_DEBUG_LOGPATH
        result = self.config.set_runtime_config(ADAPTER_DEBUG_LOGPATH="/tmp/other")
        after = self.config.get_runtime_config()
        assert result == before == after
        assert self.config.ADAPTER_DEBUG_LOGPATH == logpath_before  # не изменилось

    def test_wrong_type_not_applied(self):
        """Wrong type for known key is not applied; other keys still apply.

        Второй ключ — ADAPTER_DEBUG_PARTS=False (не True): с True каскад
        v0.9.6 включил бы Log, и проверка «Log остался прежним» не имела бы
        смысла."""
        debug_before = self.config.ADAPTER_DEBUG
        result = self.config.set_runtime_config(
            ADAPTER_DEBUG="not-a-bool",  # неверный тип
            ADAPTER_DEBUG_PARTS=False,  # верный тип
        )
        assert result["ADAPTER_DEBUG"] is debug_before  # осталось прежнее значение
        assert result["ADAPTER_DEBUG_PARTS"] is False  # применилось

    def test_bool_not_passed_as_int(self):
        """Bool is checked BEFORE int — bool doesn't pass as int field."""
        result = self.config.set_runtime_config(
            ADAPTER_TRACE_REASONING_MAX_CHARS=True  # bool, а не int
        )
        # Не применилось (int-поле отклоняет bool)
        assert result["ADAPTER_TRACE_REASONING_MAX_CHARS"] == 0  # дефолт

    def test_parts_bool_applied(self):
        """ADAPTER_DEBUG_PARTS: bool применяется и виден (str-полей в пуле нет)."""
        result = self.config.set_runtime_config(ADAPTER_DEBUG_PARTS=True)
        assert result["ADAPTER_DEBUG_PARTS"] is True
        assert self.config.ADAPTER_DEBUG_PARTS is True
        # Выключение обратно
        result = self.config.set_runtime_config(ADAPTER_DEBUG_PARTS=False)
        assert result["ADAPTER_DEBUG_PARTS"] is False

    def test_parts_rejects_non_bool(self):
        """ADAPTER_DEBUG_PARTS принимает только bool: строка игнорируется."""
        result = self.config.set_runtime_config(ADAPTER_DEBUG_PARTS="yes")
        assert result["ADAPTER_DEBUG_PARTS"] is False  # осталось прежнее значение

    def test_parts_on_enables_log(self):
        """Каскад (v0.9.6): Parts=on при Log=off включает Log."""
        self.config.ADAPTER_DEBUG = False
        self.config.ADAPTER_DEBUG_PARTS = False
        result = self.config.set_runtime_config(ADAPTER_DEBUG_PARTS=True)
        assert result["ADAPTER_DEBUG_PARTS"] is True
        assert result["ADAPTER_DEBUG"] is True
        assert self.config.ADAPTER_DEBUG is True

    def test_log_off_disables_parts(self):
        """Каскад (v0.9.6): выключение Log гасит Parts."""
        self.config.ADAPTER_DEBUG = True
        self.config.ADAPTER_DEBUG_PARTS = True
        result = self.config.set_runtime_config(ADAPTER_DEBUG=False)
        assert result["ADAPTER_DEBUG"] is False
        assert result["ADAPTER_DEBUG_PARTS"] is False
        assert self.config.ADAPTER_DEBUG_PARTS is False

    def test_log_off_and_parts_on_parts_wins(self):
        """В одном вызове Log=off + Parts=on: побеждает явное намерение Parts."""
        self.config.ADAPTER_DEBUG = True
        self.config.ADAPTER_DEBUG_PARTS = False
        result = self.config.set_runtime_config(
            ADAPTER_DEBUG=False, ADAPTER_DEBUG_PARTS=True
        )
        assert result["ADAPTER_DEBUG"] is True
        assert result["ADAPTER_DEBUG_PARTS"] is True

    def test_unrelated_key_does_not_cascade(self):
        """Несогласованность из env (Log=off, Parts=on) не «лечится»
        изменением посторонней настройки: каскад срабатывает только когда
        тронут один из двух тумблеров."""
        self.config.ADAPTER_DEBUG = False
        self.config.ADAPTER_DEBUG_PARTS = True
        result = self.config.set_runtime_config(ADAPTER_DEBUG_TRIM=1234)
        assert result["ADAPTER_DEBUG_PARTS"] is True  # не тронуто
        assert result["ADAPTER_DEBUG"] is False

    def test_trim_int_applied(self):
        """ADAPTER_DEBUG_TRIM: int применяется, 0 допустим (без обрезки)."""
        result = self.config.set_runtime_config(ADAPTER_DEBUG_TRIM=0)
        assert result["ADAPTER_DEBUG_TRIM"] == 0
        assert self.config.trim_limit() == 0
        result = self.config.set_runtime_config(ADAPTER_DEBUG_TRIM=777)
        assert result["ADAPTER_DEBUG_TRIM"] == 777
        assert self.config.trim_limit() == 777

    def test_return_value_matches_sent(self):
        """Return value reflects actual values after application."""
        result = self.config.set_runtime_config(
            ADAPTER_DEBUG=False,
            ADAPTER_DEBUG_TRIM=1000,
        )
        assert result["ADAPTER_DEBUG"] is False
        assert result["ADAPTER_DEBUG_TRIM"] == 1000
        # Возвращает актуальные значения (могли отличаться от посланных, если что-то отклонилось)

    def test_pool_keys_match_pool(self):
        """Return value has exactly the RUNTIME_CONFIG_POOL keys.

        v0.9.1: пул расширен TARGET-переменными маршрутизации входов
        (ADAPTER_*_TARGET) — их значения (строки из фиксированного домена)
        применяются на лету, как и bool/int. v0.9.3: добавлена строка маппинга
        моделей ADAPTER_MODELS_MAPPING (str). Состав пула — единый источник
        RUNTIME_CONFIG_POOL: тест фиксирует, что get/set возвращают ровно его.
        """
        result = self.config.set_runtime_config(ADAPTER_DEBUG=False)
        assert set(result.keys()) == set(self.config.RUNTIME_CONFIG_POOL)
        assert len(result) == len(self.config.RUNTIME_CONFIG_POOL) == 13
        # Три TARGET-переменные входят в пул со значениями-дефолтами env.
        assert result["ADAPTER_MESSAGES_TARGET"] == "completions"
        assert result["ADAPTER_COMPLETIONS_TARGET"] == "none"
        assert result["ADAPTER_RESPONSES_TARGET"] == "none"
        # Строка маппинга — тоже в пуле (дефолт env: пусто).
        assert result["ADAPTER_MODELS_MAPPING"] == ""

    # -- enum-поля пула: TARGET-маршрутизация входов (v0.9.1) -------------

    def test_enum_target_applied(self):
        """TARGET-значение из домена применяется и видно в get_runtime_config()."""
        result = self.config.set_runtime_config(
            ADAPTER_MESSAGES_TARGET="responses",
            ADAPTER_COMPLETIONS_TARGET="passthrough",
        )
        assert result["ADAPTER_MESSAGES_TARGET"] == "responses"
        assert result["ADAPTER_COMPLETIONS_TARGET"] == "passthrough"
        assert self.config.ADAPTER_MESSAGES_TARGET == "responses"
        assert self.config.ADAPTER_COMPLETIONS_TARGET == "passthrough"
        current = self.config.get_runtime_config()
        assert current["ADAPTER_MESSAGES_TARGET"] == "responses"

    def test_enum_target_invalid_ignored(self):
        """Невалидная строка для TARGET игнорируется; соседние ключи применяются."""
        before = self.config.ADAPTER_COMPLETIONS_TARGET
        result = self.config.set_runtime_config(
            ADAPTER_COMPLETIONS_TARGET="bogus",  # не из домена
            ADAPTER_DEBUG=True,  # валидный соседний ключ
        )
        assert result["ADAPTER_COMPLETIONS_TARGET"] == before  # не изменилось
        assert self.config.ADAPTER_COMPLETIONS_TARGET == before
        assert result["ADAPTER_DEBUG"] is True  # применилось

    def test_enum_target_rejects_non_string(self):
        """TARGET принимает только строку из домена: bool/int игнорируются."""
        before = self.config.ADAPTER_RESPONSES_TARGET
        result = self.config.set_runtime_config(
            ADAPTER_RESPONSES_TARGET=True,  # bool
            ADAPTER_MESSAGES_TARGET=1,  # int
        )
        assert result["ADAPTER_RESPONSES_TARGET"] == before == "none"
        assert result["ADAPTER_MESSAGES_TARGET"] == "completions"  # дефолт не тронут

    # -- str-поле пула: маппинг моделей (v0.9.3) --------------------------

    def test_mapping_str_applied_live(self):
        """ADAPTER_MODELS_MAPPING: строка применяется и перестраивает _MAP.

        v0.9.3: строка маппинга входит в runtime-пул. set_runtime_config
        перестраивает словарь config._MAP НА МЕСТЕ (clear+update), а не
        переприсваивает — server.py держит ссылку на словарь. Проверяем, что
        та же ссылка видит новый маппинг.
        """
        from backend_adapter.config import _MAP as ref
        assert ref is self.config._MAP

        result = self.config.set_runtime_config(ADAPTER_MODELS_MAPPING="a:b,c:d")
        assert result["ADAPTER_MODELS_MAPPING"] == "a:b,c:d"
        assert self.config.ADAPTER_MODELS_MAPPING == "a:b,c:d"
        assert self.config.get_runtime_config()["ADAPTER_MODELS_MAPPING"] == "a:b,c:d"
        assert dict(ref) == {"a": "b", "c": "d"}
        assert dict(self.config._MAP) == {"a": "b", "c": "d"}

    def test_mapping_empty_disables(self):
        """Пустая строка маппинга валидна — _MAP очищается (маппинг отключён)."""
        self.config.set_runtime_config(ADAPTER_MODELS_MAPPING="a:b")
        assert dict(self.config._MAP) == {"a": "b"}
        result = self.config.set_runtime_config(ADAPTER_MODELS_MAPPING="")
        assert result["ADAPTER_MODELS_MAPPING"] == ""
        assert dict(self.config._MAP) == {}

    def test_mapping_rejects_non_string(self):
        """Не-строка для маппинга игнорируется; соседний ключ применяется."""
        before = self.config.ADAPTER_MODELS_MAPPING
        result = self.config.set_runtime_config(
            ADAPTER_MODELS_MAPPING=123,  # int
            ADAPTER_DEBUG=True,
        )
        assert result["ADAPTER_MODELS_MAPPING"] == before  # не изменилось
        assert self.config.ADAPTER_MODELS_MAPPING == before
        assert result["ADAPTER_DEBUG"] is True

    def test_target_change_seen_by_routing(self):
        """Смена TARGET через set_runtime_config видна маршрутизатору сразу.

        routing.target_for_input читает config.ADAPTER_*_TARGET на каждый вызов
        (атрибут модуля, не снимок импорта) — live-механизм пула (см.
        routing.py). Прямая проверка: после set_runtime_config маршрутизатор
        решает по НОВОМУ значению.
        """
        from backend_adapter import routing

        # Дефолт: messages → completions (конверсия); вход закрыт = none.
        assert routing.target_for_input("messages") == "completions"
        assert routing.target_for_input("completions") == "none"

        # Меняем на лету: messages → convert messages→messages (сортировка system).
        self.config.set_runtime_config(ADAPTER_MESSAGES_TARGET="messages")
        assert routing.target_for_input("messages") == "messages"

        # Меняем на лету: вход /v1/chat/completions открыт (passthrough E→E).
        self.config.set_runtime_config(ADAPTER_COMPLETIONS_TARGET="passthrough")
        assert routing.target_for_input("completions") == "passthrough"


class TestSessionConfigPool:
    """Домены пер-сессионных переопределений (v0.9.5, session_settings).

    Пул переопределяемого — SESSION_CONFIG_POOL (логирование + TARGET), типы —
    _SESSION_CONFIG_TYPES. v0.9.9: у TARGET-полей нет отдельного домена —
    тот же TARGET_ALLOWED_VALUES, что у глобальной настройки (состояния
    «inherit» нет, «не задано» и есть живое наследование)."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_no_session_target_domain(self):
        # Отдельного домена пер-сессионных TARGET-значений больше нет.
        assert not hasattr(self.config, "SESSION_TARGET_VALUES")

    def test_pool_contents(self):
        # Пул — логирование (2 флага) + три TARGET-переменные входов.
        assert set(self.config.SESSION_CONFIG_POOL) == {
            "ADAPTER_DEBUG",
            "ADAPTER_DEBUG_PARTS",
            "ADAPTER_MESSAGES_TARGET",
            "ADAPTER_COMPLETIONS_TARGET",
            "ADAPTER_RESPONSES_TARGET",
        }

    def test_pool_types_cover_pool(self):
        # У каждого имени пула объявлен тип — иначе set_config молча
        # проигнорировал бы настройку по KeyError-ветке.
        for name in self.config.SESSION_CONFIG_POOL:
            assert name in self.config._SESSION_CONFIG_TYPES

    def test_target_domain_is_shared_with_runtime(self):
        # v0.9.9: TARGET-поля сессии и общий пул валидируются ОДНИМ доменом —
        # TARGET_ALLOWED_VALUES, без "inherit".
        for name in (
            "ADAPTER_MESSAGES_TARGET",
            "ADAPTER_COMPLETIONS_TARGET",
            "ADAPTER_RESPONSES_TARGET",
        ):
            assert self.config._SESSION_CONFIG_TYPES[name] == (
                "enum",
                self.config.TARGET_ALLOWED_VALUES,
            )
            assert self.config._RUNTIME_CONFIG_TYPES[name] == (
                "enum",
                self.config.TARGET_ALLOWED_VALUES,
            )
        assert "inherit" not in self.config.TARGET_ALLOWED_VALUES

    def test_bool_fields_are_bool(self):
        assert self.config._SESSION_CONFIG_TYPES["ADAPTER_DEBUG"] is bool
        assert self.config._SESSION_CONFIG_TYPES["ADAPTER_DEBUG_PARTS"] is bool

    def test_pool_is_subset_of_runtime_pool(self):
        # Имена сессии — из общего пула (сессия переопределяет то, что вообще
        # переключаемо на лету), но БЕЗ строки маппинга моделей: она не «объём
        # на сессию».
        assert set(self.config.SESSION_CONFIG_POOL) <= set(self.config.RUNTIME_CONFIG_POOL)
        assert "ADAPTER_MODELS_MAPPING" not in self.config.SESSION_CONFIG_POOL


class TestAcceptsValue:
    """accepts_value — общий валидатор значений пулов (set_runtime_config и
    session_settings проверяют одним правилом)."""

    def setup_method(self):
        _reload_config()
        from backend_adapter import config
        self.config = config

    def test_bool(self):
        assert self.config.accepts_value(bool, True) is True
        assert self.config.accepts_value(bool, False) is True
        # int/bool не взаимозаменяемы: 1 — не bool.
        assert self.config.accepts_value(bool, 1) is False
        assert self.config.accepts_value(bool, "yes") is False

    def test_int(self):
        assert self.config.accepts_value(int, 500) is True
        assert self.config.accepts_value(int, 0) is True
        # bool — подкласс int, но для int-поля отклоняется ЯВНО (иначе
        # True молча стал бы 1).
        assert self.config.accepts_value(int, True) is False
        assert self.config.accepts_value(int, "500") is False

    def test_str(self):
        assert self.config.accepts_value(str, "a:b") is True
        assert self.config.accepts_value(str, "") is True
        assert self.config.accepts_value(str, 123) is False

    def test_enum(self):
        domain = ("enum", ("messages", "none"))
        assert self.config.accepts_value(domain, "messages") is True
        assert self.config.accepts_value(domain, "none") is True
        assert self.config.accepts_value(domain, "bogus") is False
        # Не-строка (в т.ч. bool) в enum-поле не проходит.
        assert self.config.accepts_value(domain, True) is False
        assert self.config.accepts_value(domain, 1) is False

    def test_unknown_expected_false(self):
        assert self.config.accepts_value(float, 1.5) is False
        assert self.config.accepts_value(None, "x") is False

