#!/usr/bin/env python3
"""Unit tests for backend_adapter.probe_json — JSON files of check results
(models and endpoint probes) written to ADAPTER_DEBUG_LOGPATH (v0.9.0).

Tests cover:
  - name conversion for filenames: `/` and `:` → `_`; names without them
    stay unchanged (no collisions for dots/dashes)
  - write_models_json writes <backend>.models.json into the LOGPATH root;
    write_endpoint_json — <backend>.<converted-model>.<pname>.json
  - overwrite: a second call replaces the whole file (one file, new content)
  - atomicity: no `.tmp` leftovers after a successful write
  - unconditional channel: files are written regardless of ADAPTER_DEBUG_ENABLE
  - sanitizer: secrets redacted by default; full data when
    ADAPTER_SENSITIVE_LOGGING_ENABLE=1
  - a write failure never raises (observational channel)
"""
import json
import os
import sys


def _reload_probe_json():
    """Reload probe_json so it re-reads LOGPATH from env at call time."""
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]
    from backend_adapter import probe_json

    return probe_json


class TestConvertName:
    def test_slash_and_colon_to_underscore(self):
        assert _reload_probe_json()._convert_name("org/model:v1") == "org_model_v1"

    def test_plain_name_unchanged(self):
        assert _reload_probe_json()._convert_name("qwen3.6-35b-a3b") == "qwen3.6-35b-a3b"

    def test_multiple_replacements(self):
        assert _reload_probe_json()._convert_name("a/b:c/d") == "a_b_c_d"


class TestWriteFiles:
    def _logpath(self, tmp_path):
        # ADAPTER_DEBUG_LOGPATH → tmp_path: формула модуля читает env живым
        # чтением на каждом вызове, reload не нужен (но безвреден).
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        return _reload_probe_json()

    def test_writes_models_json(self, tmp_path):
        pj = self._logpath(tmp_path)
        payload = {"backend": "llm", "checked_at": "2026-09-08T10:00:00", "ok": True, "count": 2}
        pj.write_models_json("llm", payload)
        written = json.loads((tmp_path / "llm.models.json").read_text())
        assert written == payload

    def test_writes_endpoint_json_with_converted_model(self, tmp_path):
        pj = self._logpath(tmp_path)
        payload = {"backend": "llm", "model": "org/model:v1", "endpoint": "completions"}
        pj.write_endpoint_json("llm", "org/model:v1", "completions", payload)
        assert (tmp_path / "llm.org_model_v1.completions.json").exists()
        assert json.loads((tmp_path / "llm.org_model_v1.completions.json").read_text()) == payload

    def test_overwrite_replaces_whole_file(self, tmp_path):
        pj = self._logpath(tmp_path)
        pj.write_models_json("llm", {"backend": "llm", "ok": True, "count": 1, "models": [{"id": "a"}]})
        pj.write_models_json("llm", {"backend": "llm", "ok": True, "count": 1, "models": [{"id": "b"}]})
        files = [f.name for f in tmp_path.iterdir()]
        assert files == ["llm.models.json"]
        assert json.loads((tmp_path / "llm.models.json").read_text())["models"] == [{"id": "b"}]

    def test_no_tmp_leftovers(self, tmp_path):
        pj = self._logpath(tmp_path)
        pj.write_models_json("llm", {"ok": True})
        pj.write_endpoint_json("llm", "m:1", "completions", {"ok": True})
        assert [f.name for f in tmp_path.iterdir()] == [
            "llm.m_1.completions.json",
            "llm.models.json",
        ]

    def test_logpath_created_if_missing(self, tmp_path):
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path / "nested" / "logs")
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"ok": True})
        assert (tmp_path / "nested" / "logs" / "llm.models.json").exists()


class TestUnconditionalChannel:
    def test_written_when_debug_enable_off(self, tmp_path):
        """Файлы пишутся при ADAPTER_DEBUG_ENABLE=0 (канал .err: гейт — только
        наличие LOGPATH; вне ENABLE/PARTS/TRIM)."""
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        os.environ["ADAPTER_DEBUG_ENABLE"] = "0"
        os.environ["ADAPTER_DEBUG_PARTS"] = "0"
        os.environ["ADAPTER_DEBUG_TRIM"] = "0"
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"ok": True})
        pj.write_endpoint_json("llm", "m", "completions", {"ok": True})
        assert (tmp_path / "llm.models.json").exists()
        assert (tmp_path / "llm.m.completions.json").exists()

    def test_default_logpath_used_when_env_empty(self, monkeypatch, tmp_path, capsys):
        """LOGPATH пуст/не задан → дефолт ./tmp/logs (относительно папки
        запуска). Проверяем на подменённом cwd, чтобы не писать в репозиторий."""
        monkeypatch.chdir(tmp_path)
        os.environ.pop("ADAPTER_DEBUG_LOGPATH", None)
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"ok": True})
        assert (tmp_path / "tmp" / "logs" / "llm.models.json").exists()
        assert capsys.readouterr().out == ""  # тихий канал


class TestSanitizer:
    def test_secrets_redacted_by_default(self, tmp_path):
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        os.environ.pop("ADAPTER_SENSITIVE_LOGGING_ENABLE", None)
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"backend": "llm", "token": "sk-abcdefghij1234567890"})
        text = (tmp_path / "llm.models.json").read_text()
        assert "sk-abcdefghij1234567890" not in text
        assert "REDACTED" in text

    def test_nested_secret_key_masked_recursively(self, tmp_path):
        """Секретный ключ во вложенной структуре (models-список ответа
        /v1/models с полем key) маскируется рекурсивным обходом."""
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        os.environ.pop("ADAPTER_SENSITIVE_LOGGING_ENABLE", None)
        pj = _reload_probe_json()
        token = "sk-nested-abcdefghij1234567890"
        pj.write_models_json(
            "llm",
            {"backend": "llm", "models": [{"id": "m1", "key": token, "ok": True}]},
        )
        text = (tmp_path / "llm.models.json").read_text()
        assert token not in text
        assert "REDACTED" in text
        assert "m1" in text  # несекретное содержимое не тронуто

    def test_short_secret_fully_masked(self, tmp_path):
        """Короткое значение (≤8 символов) маскируется целиком."""
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        os.environ.pop("ADAPTER_SENSITIVE_LOGGING_ENABLE", None)
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"backend": "llm", "password": "short"})
        text = (tmp_path / "llm.models.json").read_text()
        assert "short" not in text

    def test_full_data_when_sensitive_enabled(self, tmp_path):
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        os.environ["ADAPTER_SENSITIVE_LOGGING_ENABLE"] = "1"
        pj = _reload_probe_json()
        token = "sk-abcdefghij1234567890"
        pj.write_models_json("llm", {"backend": "llm", "token": token})
        assert token in (tmp_path / "llm.models.json").read_text()


class TestWriteFailureSilent:
    def test_write_error_never_raises(self, tmp_path, monkeypatch):
        """Канал наблюдательный: ошибка записи (тут — непустой LOGPATH-файл)
        молча глотается, проверка не роняется."""
        target = tmp_path / "logs"
        target.write_text("I am a file, not a directory")
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(target)
        pj = _reload_probe_json()
        pj.write_models_json("llm", {"ok": True})  # не должно бросить
        assert target.read_text() == "I am a file, not a directory"
