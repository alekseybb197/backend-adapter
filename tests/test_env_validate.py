#!/usr/bin/env python3
"""Tests for backend_adapter.env_validate — строгая проверка env (v0.9.6).

Модуль — лист DAG (только stdlib): можно импортировать на уровне файла,
свежесть после fresh_env не нужна. Проверяем парсеры (мягкие: невалидное →
дефолт) и validate_env (строгая: невалидное → [FATAL] + sys.exit(1)).
"""

import pytest

from backend_adapter import env_validate


class TestParseBool:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "On", "YES"])
    def test_true_domain(self, raw):
        assert env_validate.parse_bool(raw) is True

    @pytest.mark.parametrize("raw", ["0", "false", "FALSE", " no ", "off", "", "   "])
    def test_false_domain(self, raw):
        assert env_validate.parse_bool(raw) is False

    def test_invalid_returns_default(self):
        # "enabled" не входит в домен → дефолт (мягкий парсер; строгость — за
        # validate_env).
        assert env_validate.parse_bool("enabled") is False
        assert env_validate.parse_bool("enabled", True) is True

    def test_on_is_true_off_is_false(self):
        # Раньше "off" молча трактовалось как ВКЛЮЧЕНО (не входило в набор
        # «выключено») — теперь off/on в домене и работают как ожидается.
        assert env_validate.parse_bool("off") is False
        assert env_validate.parse_bool("on") is True


class TestParseInt:
    def test_plain_int(self):
        assert env_validate.parse_int("9999", 0) == 9999

    def test_whitespace(self):
        assert env_validate.parse_int("  42  ", 0) == 42

    def test_negative(self):
        assert env_validate.parse_int("-5", 0) == -5

    @pytest.mark.parametrize("raw", ["abc", "1.5", "", "1e3", "0x10"])
    def test_invalid_returns_default(self, raw):
        assert env_validate.parse_int(raw, 300) == 300


class TestValidateEnv:
    """validate_env: невалидное → FATAL + SystemExit(1); валидное/незаданное — ок."""

    def test_valid_env_passes(self, monkeypatch):
        monkeypatch.setenv("ADAPTER_PROXY_PORT", "1234")
        monkeypatch.setenv("ADAPTER_DEBUG_ENABLE", "1")
        env_validate.validate_env()  # не бросает

    def test_unset_passes(self, monkeypatch):
        monkeypatch.delenv("ADAPTER_PROXY_PORT", raising=False)
        monkeypatch.delenv("ADAPTER_DEBUG_ENABLE", raising=False)
        env_validate.validate_env()

    def test_invalid_int_fatal(self, monkeypatch, capsys):
        monkeypatch.setenv("ADAPTER_PROXY_PORT", "abc")
        with pytest.raises(SystemExit) as ei:
            env_validate.validate_env()
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[FATAL]" in out
        assert "ADAPTER_PROXY_PORT" in out
        assert "abc" in out

    def test_invalid_bool_fatal(self, monkeypatch, capsys):
        # "enabled" не входит в домен bool → FATAL (раньше молча False).
        monkeypatch.setenv("ADAPTER_DEBUG_ENABLE", "enabled")
        with pytest.raises(SystemExit) as ei:
            env_validate.validate_env()
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "ADAPTER_DEBUG_ENABLE" in out

    @pytest.mark.parametrize("raw", ["1", "0", "true", "false", "yes", "no", "on", "off", ""])
    def test_all_bool_words_accepted(self, monkeypatch, raw):
        monkeypatch.setenv("ADAPTER_DEBUG_ENABLE", raw)
        env_validate.validate_env()

    def test_all_bool_vars_checked(self, monkeypatch):
        # Каждая bool-переменная из таблицы ловит мусор (не только DEBUG).
        for name, kind in env_validate._ENV_SPECS.items():
            if kind != "bool":
                continue
            monkeypatch.setenv(name, "maybe")
            with pytest.raises(SystemExit):
                env_validate.validate_env()
            monkeypatch.delenv(name)

    def test_all_int_vars_checked(self, monkeypatch):
        for name, kind in env_validate._ENV_SPECS.items():
            if kind != "int":
                continue
            monkeypatch.setenv(name, "not-a-number")
            with pytest.raises(SystemExit):
                env_validate.validate_env()
            monkeypatch.delenv(name)

    def test_str_vars_not_checked(self, monkeypatch):
        # str-переменные — свободный текст: любое значение валидно.
        monkeypatch.setenv("ADAPTER_DATA_ROOT", "что угодно / с пробелами")
        monkeypatch.setenv("ADAPTER_MODELS_MAPPING", "??:!!")
        env_validate.validate_env()

    def test_specs_cover_int_and_bool_env_read_by_config(self, monkeypatch):
        # Таблица должна покрывать все int/bool-переменные, которые config.py
        # парсит как int(...)/bool — иначе валидатор пропустит мусор.
        for name in (
            "ADAPTER_PROXY_PORT",
            "ADAPTER_TIMEOUT",
            "ADAPTER_RETRY_COUNT",
            "ADAPTER_DEBUG_TRIM",
            "ADAPTER_WEBUI_PORT",
            "ADAPTER_EXPORTER_PORT",
            "ADAPTER_SESSIONS_TABLE",
            "ADAPTER_DEBUG_ENABLE",
            "ADAPTER_STRICT_MODELS",
            "ADAPTER_STREAMING_ENABLE",
        ):
            assert name in env_validate._ENV_SPECS, name


class TestConfigUsesValidators:
    """config.py парсит bool/int через env_validate — единый домен значений."""

    def test_config_bool_uses_parse_bool(self, monkeypatch):
        # "off" теперь ВЫКЛЮЧЕНО (раньше молча трактовалось как включено:
        # "off" не входило в набор «выключено»).
        monkeypatch.setenv("ADAPTER_DEBUG_ENABLE", "off")
        monkeypatch.setenv("ADAPTER_DATA_ROOT", "")
        monkeypatch.setenv("ADAPTER_BACKEND_CONFIG", "")
        import sys

        for n in [m for m in list(sys.modules) if m.startswith("backend_adapter")]:
            del sys.modules[n]
        from backend_adapter import config

        assert config.ADAPTER_DEBUG is False

    def test_config_int_invalid_falls_back_to_default(self, monkeypatch):
        # Мягкий парсер config: мусор → дефолт, без ValueError на импорте.
        # (Строгий FATAL даёт env_validate.validate_env на старте скрипта.)
        monkeypatch.setenv("ADAPTER_PROXY_PORT", "abc")
        import sys

        for n in [m for m in list(sys.modules) if m.startswith("backend_adapter")]:
            del sys.modules[n]
        from backend_adapter import config

        assert config.PROXY_PORT == 9999
