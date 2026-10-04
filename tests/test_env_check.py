#!/usr/bin/env python3
"""Tests for backend_adapter.env_check — режим ``--check`` (v0.9.13).

Проверяет офлайн-диагностику конфигов дома: типы значений по ``_ENV_SPECS``
(int/bool), устаревшие имена (``_LEGACY``), структуру adapter.yaml/tariffs.yaml.
Итог: exit 1 при ``[ERROR]``, иначе 0 (``[WARN]`` не роняет).

``run_check`` читает **живое** окружение, поэтому тесты изолированы: fixture
``clean_adapter_env`` вычищает все ``ADAPTER_*`` из ``os.environ`` на время
теста (иначе legacy-переменные из шелла разработчика дали бы лишние WARN).
"""

import os

import pytest

from backend_adapter import env_check
from backend_adapter.env_validate import _BOOL_FALSE, _BOOL_TRUE, _ENV_SPECS


@pytest.fixture
def clean_adapter_env(monkeypatch):
    """Убрать все ADAPTER_* из окружения на время теста."""
    for name in [k for k in os.environ if k.startswith("ADAPTER_")]:
        monkeypatch.delenv(name, raising=False)


def _valid_yaml(tmp_path, *, body: str | None = None) -> None:
    (tmp_path / "adapter.yaml").write_text(
        body
        or (
            "backend:\n  - name: llm\n    base: https://llm.example.com\n    key: ADAPTER_LLM_KEY\n"
        ),
        encoding="utf-8",
    )


class TestParseEnvText:
    def test_exports_quotes_and_comments(self):
        text = "# комментарий\nexport A=1\nB='two words'\nC=\"three\"\n\nD=no-space\n"
        assert env_check.parse_env_text(text) == {
            "A": "1",
            "B": "two words",
            "C": "three",
            "D": "no-space",
        }

    def test_lines_without_equals_ignored(self):
        assert env_check.parse_env_text("just a line\nX=1\n") == {"X": "1"}


class TestCleanHome:
    def test_valid_set_exit_zero(self, tmp_path, clean_adapter_env, capsys):
        _valid_yaml(tmp_path)
        (tmp_path / "tariffs.yaml").write_text("tariffs: []\n", encoding="utf-8")
        (tmp_path / "adapter.env").write_text(
            "export ADAPTER_PROXY_PORT=9999\nexport ADAPTER_DEBUG_ENABLE=0\n",
            encoding="utf-8",
        )
        assert env_check.run_check(str(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "[ERROR]" not in out
        assert "Итог: ошибок нет." in out

    def test_empty_root_warns_but_zero(self, tmp_path, clean_adapter_env, capsys):
        """Пустой root: файлов нет — WARN, но не ошибка (exit 0)."""
        assert env_check.run_check(str(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "[WARN]" in out
        assert "[ERROR]" not in out


class TestTypeErrors:
    @pytest.mark.parametrize("value", ["abc", "1.5", ""])
    def test_int_field_from_file(self, value, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "adapter.env").write_text(
            f"export ADAPTER_PROXY_PORT={value}\n", encoding="utf-8"
        )
        assert env_check.run_check(str(tmp_path)) == 1
        out = capsys.readouterr().out
        assert "ADAPTER_PROXY_PORT" in out and "[ERROR]" in out

    def test_bad_bool_from_file(self, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "adapter.env").write_text(
            "export ADAPTER_DEBUG_ENABLE=maybe\n", encoding="utf-8"
        )
        assert env_check.run_check(str(tmp_path)) == 1
        assert "ADAPTER_DEBUG_ENABLE" in capsys.readouterr().out

    def test_all_bool_domain_values_pass(self, tmp_path, clean_adapter_env):
        """Весь домен bool (оба набора + пусто) — без ошибок."""
        for i, v in enumerate(_BOOL_TRUE + _BOOL_FALSE):
            (tmp_path / "adapter.env").write_text(
                f"export ADAPTER_DEBUG_ENABLE={v}\n", encoding="utf-8"
            )
            assert env_check.run_check(str(tmp_path)) == 0, (i, v)

    def test_bad_int_in_live_env(self, tmp_path, monkeypatch, clean_adapter_env, capsys):
        monkeypatch.setenv("ADAPTER_WEBUI_PORT", "not-a-port")
        assert env_check.run_check(str(tmp_path)) == 1
        out = capsys.readouterr().out
        assert "ADAPTER_WEBUI_PORT" in out and "окружение" in out

    def test_every_int_spec_reported(self, tmp_path, clean_adapter_env, capsys):
        """Каждая int-переменная из _ENV_SPECS диагностируется по имени."""
        int_specs = [n for n, k in _ENV_SPECS.items() if k == "int"]
        for name in int_specs:
            (tmp_path / "adapter.env").write_text(f"export {name}=xyz\n", encoding="utf-8")
            assert env_check.run_check(str(tmp_path)) == 1
            assert name in capsys.readouterr().out


class TestLegacy:
    def test_old_trim_warns_not_error(self, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "adapter.env").write_text("export ADAPTER_DEBUG_TRIM=3000\n", encoding="utf-8")
        assert env_check.run_check(str(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "ADAPTER_DEBUG_TRIM" in out and "(v0.9.13)" in out
        assert "[ERROR]" not in out

    @pytest.mark.parametrize("name", sorted(env_check._LEGACY))
    def test_each_legacy_warns(self, name, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "adapter.env").write_text(f"export {name}=x\n", encoding="utf-8")
        assert env_check.run_check(str(tmp_path)) == 0
        assert name in capsys.readouterr().out

    def test_empty_legacy_value_is_silent(self, tmp_path, clean_adapter_env, capsys):
        """Пустое значение старого имени — не задано, WARN не печатается."""
        (tmp_path / "adapter.env").write_text("export ADAPTER_DEBUG_TRIM=\n", encoding="utf-8")
        assert env_check.run_check(str(tmp_path)) == 0
        assert "ADAPTER_DEBUG_TRIM" not in capsys.readouterr().out


class TestUnknownAndTokenVars:
    def test_unknown_debug_var_warns(self, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "adapter.env").write_text("export ADAPTER_DEBUG_ENBLE=1\n", encoding="utf-8")
        assert env_check.run_check(str(tmp_path)) == 0
        assert "ADAPTER_DEBUG_ENBLE" in capsys.readouterr().out

    def test_token_and_target_vars_are_silent(self, tmp_path, clean_adapter_env, capsys):
        """Токены (по key из adapter.yaml) и TARGET-поля — не ложная тревога."""
        _valid_yaml(tmp_path)
        (tmp_path / "adapter.env").write_text(
            "export ADAPTER_LLM_KEY=secret\nexport ADAPTER_MESSAGES_TARGET=completions\n",
            encoding="utf-8",
        )
        assert env_check.run_check(str(tmp_path)) == 0
        out = capsys.readouterr().out
        assert "ADAPTER_LLM_KEY" not in out
        assert "ADAPTER_MESSAGES_TARGET" not in out


class TestAdapterYaml:
    def test_missing_yaml_warns_zero(self, tmp_path, clean_adapter_env, capsys):
        assert env_check.run_check(str(tmp_path)) == 0
        assert "adapter.yaml" in capsys.readouterr().out

    def test_broken_yaml_error(self, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "adapter.yaml").write_text("backend: [unclosed\n", encoding="utf-8")
        assert env_check.run_check(str(tmp_path)) == 1
        assert "[ERROR]" in capsys.readouterr().out

    def test_backend_not_list_error(self, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "adapter.yaml").write_text("backend: not-a-list\n", encoding="utf-8")
        assert env_check.run_check(str(tmp_path)) == 1
        assert "[ERROR]" in capsys.readouterr().out

    def test_entry_missing_key_error(self, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "adapter.yaml").write_text(
            "backend:\n  - name: llm\n    base: https://llm.example.com\n",
            encoding="utf-8",
        )
        assert env_check.run_check(str(tmp_path)) == 1
        out = capsys.readouterr().out
        assert "[ERROR]" in out and "key" in out

    def test_valid_yaml_ok(self, tmp_path, clean_adapter_env, capsys):
        _valid_yaml(tmp_path)
        assert env_check.run_check(str(tmp_path)) == 0
        assert "поля name/base/key заполнены" in capsys.readouterr().out


class TestTariffsYaml:
    def test_absent_is_ok(self, tmp_path, clean_adapter_env, capsys):
        assert env_check.run_check(str(tmp_path)) == 0
        assert "необязательн" in capsys.readouterr().out

    def test_wrong_type_error(self, tmp_path, clean_adapter_env, capsys):
        (tmp_path / "tariffs.yaml").write_text("tariffs: not-a-list\n", encoding="utf-8")
        assert env_check.run_check(str(tmp_path)) == 1
        assert "[ERROR]" in capsys.readouterr().out


class TestNeverRaises:
    def test_broken_env_does_not_raise(self, tmp_path, monkeypatch, clean_adapter_env):
        """--check обязан пережить заведомо битое окружение (ради чего и
        запускается ДО validate_env): невалидные int/bool — отчёт, не крах."""
        monkeypatch.setenv("ADAPTER_PROXY_PORT", "abc")
        monkeypatch.setenv("ADAPTER_DEBUG_ENABLE", "offf")
        assert env_check.run_check(str(tmp_path)) == 1  # без исключения
