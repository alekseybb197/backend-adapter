#!/usr/bin/env python3
"""Unit tests for backend_adapter.probe_json — JSON file of the backend
model-list poll written to ADAPTER_DATA_ROOT/var (v0.9.9).

Tests cover:
  - write_models_json writes <backend>.models.json into the var/ state dir
  - overwrite: a second call replaces the whole file (one file, new content)
  - atomicity: no `.tmp` leftovers after a successful write
  - unconditional channel: files are written regardless of ADAPTER_DEBUG_ENABLE
  - sanitizer: secrets redacted by default; full data when
    ADAPTER_SENSITIVE_LOGGING_ENABLE=1
  - a write failure never raises (observational channel)

v0.9.9: endpoint probes removed — write_endpoint_json and its filename
conversion (`/`/`:` → `_`) are gone, only the models poll remains. Файл
опроса моделей переехал в подпапку var/ корня данных.
"""
import json
import os
import sys


def _reload_probe_json():
    """Reload probe_json so it re-reads DATA_ROOT from env at call time."""
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]
    from backend_adapter import probe_json

    return probe_json


class TestNoEndpointApi:
    """v0.9.9: дампы проб эндпойнтов удалены вместе с пробами."""

    def test_endpoint_writer_removed(self):
        pj = _reload_probe_json()
        assert not hasattr(pj, "write_endpoint_json")
        assert not hasattr(pj, "_convert_name")
        assert not hasattr(pj, "_SAFE_NAME")


class TestWriteFiles:
    def _logpath(self, tmp_path):
        # ADAPTER_DATA_ROOT → tmp_path: формула модуля читает env живым
        # чтением на каждом вызове, reload не нужен (но безвреден). Файлы
        # ложатся в подпапку var/ (папка состояния).
        os.environ["ADAPTER_DATA_ROOT"] = str(tmp_path)
        return _reload_probe_json()

    def test_writes_models_json(self, tmp_path):
        pj = self._logpath(tmp_path)
        payload = {"backend": "llm", "checked_at": "2026-09-08T10:00:00", "ok": True, "count": 2}
        pj.write_models_json("llm", payload)
        written = json.loads((tmp_path / "var" / "llm.models.json").read_text())
        assert written == payload

    def test_overwrite_replaces_whole_file(self, tmp_path):
        pj = self._logpath(tmp_path)
        pj.write_models_json("llm", {"backend": "llm", "ok": True, "count": 1, "models": [{"id": "a"}]})
        pj.write_models_json("llm", {"backend": "llm", "ok": True, "count": 1, "models": [{"id": "b"}]})
        files = [f.name for f in (tmp_path / "var").iterdir()]
        assert files == ["llm.models.json"]
        assert json.loads((tmp_path / "var" / "llm.models.json").read_text())["models"] == [{"id": "b"}]

    def test_no_tmp_leftovers(self, tmp_path):
        pj = self._logpath(tmp_path)
        pj.write_models_json("llm", {"ok": True})
        assert sorted(f.name for f in (tmp_path / "var").iterdir()) == ["llm.models.json"]

    def test_var_dir_created_if_missing(self, tmp_path):
        os.environ["ADAPTER_DATA_ROOT"] = str(tmp_path / "nested" / "data")
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"ok": True})
        assert (tmp_path / "nested" / "data" / "var" / "llm.models.json").exists()


class TestUnconditionalChannel:
    def test_written_when_debug_enable_off(self, tmp_path):
        """Файлы пишутся при ADAPTER_DEBUG_ENABLE=0 (канал .err: гейт — только
        наличие корня данных; вне ENABLE/TRIM)."""
        os.environ["ADAPTER_DATA_ROOT"] = str(tmp_path)
        os.environ["ADAPTER_DEBUG_ENABLE"] = "0"
        os.environ["ADAPTER_DEBUG_TRIM"] = "0"
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"ok": True})
        assert (tmp_path / "var" / "llm.models.json").exists()

    def test_default_data_root_used_when_env_empty(self, monkeypatch, tmp_path, capsys):
        """DATA_ROOT пуст/не задан → дефолт ./tmp/adapter (относительно папки
        запуска). Проверяем на подменённом cwd, чтобы не писать в репозиторий."""
        monkeypatch.chdir(tmp_path)
        os.environ.pop("ADAPTER_DATA_ROOT", None)
        # Старые имена (если экспортированы в окружении) дали бы [WARN] на
        # импорте — канал должен быть тихим, поэтому убираем и их.
        os.environ.pop("ADAPTER_DEBUG_LOGPATH", None)
        os.environ.pop("ADAPTER_DEBUG_PARTS", None)
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"ok": True})
        assert (tmp_path / "tmp" / "adapter" / "var" / "llm.models.json").exists()
        assert capsys.readouterr().out == ""  # тихий канал


class TestSanitizer:
    def test_secrets_redacted_by_default(self, tmp_path):
        os.environ["ADAPTER_DATA_ROOT"] = str(tmp_path)
        os.environ.pop("ADAPTER_SENSITIVE_LOGGING_ENABLE", None)
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"backend": "llm", "token": "sk-abcdefghij1234567890"})
        text = (tmp_path / "var" / "llm.models.json").read_text()
        assert "sk-abcdefghij1234567890" not in text
        assert "REDACTED" in text

    def test_nested_secret_key_masked_recursively(self, tmp_path):
        """Секретный ключ во вложенной структуре (models-список ответа
        /v1/models с полем key) маскируется рекурсивным обходом."""
        os.environ["ADAPTER_DATA_ROOT"] = str(tmp_path)
        os.environ.pop("ADAPTER_SENSITIVE_LOGGING_ENABLE", None)
        pj = _reload_probe_json()
        token = "sk-nested-abcdefghij1234567890"
        pj.write_models_json(
            "llm",
            {"backend": "llm", "models": [{"id": "m1", "key": token, "ok": True}]},
        )
        text = (tmp_path / "var" / "llm.models.json").read_text()
        assert token not in text
        assert "REDACTED" in text
        assert "m1" in text  # несекретное содержимое не тронуто

    def test_short_secret_fully_masked(self, tmp_path):
        """Короткое значение (≤8 символов) маскируется целиком."""
        os.environ["ADAPTER_DATA_ROOT"] = str(tmp_path)
        os.environ.pop("ADAPTER_SENSITIVE_LOGGING_ENABLE", None)
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"backend": "llm", "password": "short"})
        text = (tmp_path / "var" / "llm.models.json").read_text()
        assert "short" not in text

    def test_full_data_when_sensitive_enabled(self, tmp_path):
        os.environ["ADAPTER_DATA_ROOT"] = str(tmp_path)
        os.environ["ADAPTER_SENSITIVE_LOGGING_ENABLE"] = "1"
        pj = _reload_probe_json()
        token = "sk-abcdefghij1234567890"
        pj.write_models_json("llm", {"backend": "llm", "token": token})
        assert token in (tmp_path / "var" / "llm.models.json").read_text()


class TestWriteFailureSilent:
    def test_write_error_never_raises(self, tmp_path, monkeypatch):
        """Канал наблюдательный: ошибка записи (тут — файл вместо папки var/)
        молча глотается, проверка не роняется."""
        target = tmp_path / "data"
        target.mkdir()
        blocker = target / "var"
        blocker.write_text("I am a file, not a directory")
        os.environ["ADAPTER_DATA_ROOT"] = str(target)
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"ok": True})  # не должно бросить
        assert blocker.read_text() == "I am a file, not a directory"
