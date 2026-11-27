#!/usr/bin/env python3
"""Tests for backend_adapter.templates — встроенные шаблоны настроек (v0.9.12).

Шаблоны ``adapter.yaml``/``tariffs.yaml`` лежат и в коде (``templates.py``, из
него их берёт ``--install`` — так они доступны и из бинарника), и файлами-
справочниками в ``docs/samples/``. Тест сторожит расхождение: любая правка
одного места обязана попасть и во второе. Там, где ``docs/samples/`` нет
(запуск из бинарника/wheel), сверка пропускается — это не ошибка.
"""

import os
from pathlib import Path

import pytest

from backend_adapter import templates

# Эталон — репозиторные образцы. Путь от этого файла: tests/ → корень → docs/samples.
_SAMPLES = Path(__file__).resolve().parent.parent / "docs" / "samples"

_REFERENCE = {
    "sample.adapter.yaml": "SAMPLE_ADAPTER_YAML",
    "sample.tariffs.yaml": "SAMPLE_TARIFFS_YAML",
}


@pytest.mark.parametrize("filename,attr", sorted(_REFERENCE.items()))
def test_embedded_matches_reference(filename, attr):
    """Встроенный шаблон совпадает с docs/samples байт-в-байт."""
    reference = _SAMPLES / filename
    if not reference.is_file():
        pytest.skip(f"docs/samples/{filename} недоступен (запуск не из репозитория)")
    assert getattr(templates, attr) == reference.read_text(encoding="utf-8")


def test_embedded_yaml_ends_with_newline():
    """Отсутствие хвостового \\n — типичная ошибка при переносе в строку."""
    assert templates.SAMPLE_ADAPTER_YAML.endswith("\n")
    assert templates.SAMPLE_TARIFFS_YAML.endswith("\n")


def test_sample_plist_is_valid():
    """Образец plist (docs/samples) должен парситься plistlib и содержать
    абсолютный путь к бинарнику в ProgramArguments.

    Сторож от двух ошибок: (1) ``--`` внутри XML-комментария делает файл
    невалидным; (2) launchd НЕ раскрывает ``~`` — путь с ``~`` не запустится.
    """
    import plistlib

    path = _SAMPLES / "com.user.backend-adapter.plist"
    if not path.is_file():
        pytest.skip("docs/samples/com.user.backend-adapter.plist недоступен")
    data = plistlib.loads(path.read_bytes())
    prog = data["ProgramArguments"]
    assert prog, "ProgramArguments пуст"
    assert not prog[0].startswith("~"), "launchd не раскрывает ~ в путях"
    assert prog[0].startswith("/"), "нужен абсолютный путь к бинарнику"


class TestRenderEnv:
    def test_contains_absolute_paths_from_root(self, tmp_path):
        root = str(tmp_path / "home")
        text = templates.render_env(root)
        # os.path.join — идиома кроссплатформенная (разделитель зависит от ОС).
        assert f"ADAPTER_BACKEND_CONFIG='{os.path.join(root, 'adapter.yaml')}'" in text
        assert f"ADAPTER_MODELS_TARIFFS='{os.path.join(root, 'tariffs.yaml')}'" in text
        assert f"ADAPTER_DATA_ROOT='{root}'" in text

    def test_token_placeholder_and_key_defaults(self, tmp_path):
        text = templates.render_env(str(tmp_path / "home"))
        # Токен — заглушка, не пустое значение; без --key имя образцовое.
        assert "ADAPTER_BACKEND_KEY_LLM_SERVICE='*****'" in text
        # Несколько дефолтов, сверяемых с config.py (регрессия при рассинхроне).
        assert "export ADAPTER_PROXY_PORT=9999" in text
        assert 'export ADAPTER_ENDPOINT_HOST="127.0.0.1"' in text
        assert "export ADAPTER_LOG_TRIM=1000" in text
        assert "export ADAPTER_WEBUI_PORT=8765" in text

    def test_custom_key_env(self, tmp_path):
        """--key подставляет имя переменной токена (v0.9.13)."""
        text = templates.render_env(str(tmp_path / "home"), key_env="MY_ADAPTER_KEY")
        assert "export MY_ADAPTER_KEY='*****'" in text
        assert "ADAPTER_BACKEND_KEY_LLM_SERVICE" not in text

    def test_paths_follow_root(self, tmp_path):
        """Разные root дают разные абсолютные пути (файл не захардкожен)."""
        a = templates.render_env(str(tmp_path / "a"))
        b = templates.render_env(str(tmp_path / "b"))
        assert a != b
        assert str(tmp_path / "a") in a
        assert str(tmp_path / "b") in b


class TestRenderAdapterYaml:
    """render_adapter_yaml — один бэкенд из --install --name/--base/--key."""

    def test_single_backend_structure(self):
        text = templates.render_adapter_yaml("demo", "https://x.example", "ADAPTER_DEMO_KEY")
        assert text.startswith("# backend-adapter config")
        assert "backend:\n" in text
        assert "  - name: demo\n" in text
        assert "    base: https://x.example\n" in text
        assert "    key: ADAPTER_DEMO_KEY\n" in text

    def test_ends_with_newline(self):
        assert templates.render_adapter_yaml("n", "http://b", "K").endswith("\n")

    def test_key_is_env_var_name_not_token(self):
        """Поле key — имя переменной, а не сам токен (ср. SAMPLE_ADAPTER_YAML)."""
        text = templates.render_adapter_yaml("n", "http://b", "ADAPTER_ENV_NAME")
        assert "key: ADAPTER_ENV_NAME" in text


class TestRenderLaunchdPlist:
    """render_launchd_plist — минимальный plist с --root подходом."""

    def test_contains_label(self):
        text = templates.render_launchd_plist("/usr/local/bin/backend-adapter", "/Users/test/.ba")
        assert f"<string>{templates.LAUNCHD_LABEL}</string>" in text

    def test_contains_root_argument(self):
        text = templates.render_launchd_plist("/usr/local/bin/adapter", "/Users/test/.ba")
        assert "<string>--root</string>" in text
        assert "<string>/Users/test/.ba</string>" in text

    def test_program_arguments_is_valid_plist(self):
        """Сторож: ProgramArguments должен парситься plistlib, первый элемент —
        ровно путь к бинарнику. Регрессия ``[4:]`` калечила ``<string>`` →
        ``ring>`` (терялось начало пути)."""
        import plistlib

        text = templates.render_launchd_plist("/usr/local/bin/backend-adapter", "/Users/test/.ba")
        data = plistlib.loads(text.encode("utf-8"))
        assert data["ProgramArguments"] == [
            "/usr/local/bin/backend-adapter",
            "--root",
            "/Users/test/.ba",
        ]

    def test_program_arguments_without_root(self):
        """Без root в ProgramArguments ровно один элемент — путь к бинарнику."""
        import plistlib

        text = templates.render_launchd_plist("/bin/adapter")
        data = plistlib.loads(text.encode("utf-8"))
        assert data["ProgramArguments"] == ["/bin/adapter"]

    def test_contains_working_directory(self):
        text = templates.render_launchd_plist("/usr/bin/python3", "/home/user/.ba")
        assert "<key>WorkingDirectory</key>" in text
        assert "<string>/home/user/.ba</string>" in text

    def test_no_root_without_root_param(self):
        """Без root — нет --root и WorkingDirectory в plist."""
        text = templates.render_launchd_plist("/usr/local/bin/backend-adapter")
        assert "--root" not in text
        assert "WorkingDirectory" not in text

    def test_log_paths(self, tmp_path):
        """StandardOutPath / StandardErrorPath появляются при задании путей."""
        out = str(tmp_path / "log" / "stdout.log")
        err = str(tmp_path / "log" / "stderr.log")
        text = templates.render_launchd_plist(
            "/usr/local/bin/adapter",
            "/Users/test/.ba",
            standard_out_path=out,
            standard_error_path=err,
        )
        assert f"<string>{out}</string>" in text
        assert "<key>StandardOutPath</key>" in text
        assert "<key>StandardErrorPath</key>" in text

    def test_default_flags(self):
        """По умолчанию KeepAlive=false, RunAtLoad=false."""
        text = templates.render_launchd_plist("/bin/adapter", "/root")
        assert "<false/>" in text

    def test_keep_alive_true(self):
        assert "<true/>" in templates.render_launchd_plist("/bin/adapter", "/root", keep_alive=True)

    def test_run_at_load_true(self):
        text = templates.render_launchd_plist("/bin/adapter", "/root", run_at_load=True)
        # два true: RunAtLoad и KeepAlive (по умолчанию False — false)
        assert "<true/>" in text

    def test_process_type_background(self):
        text = templates.render_launchd_plist("/bin/adapter", "/root")
        assert "<string>Background</string>" in text

    def test_xml_escape_special_chars(self):
        """Путь с & должен быть экранирован в XML."""
        text = templates.render_launchd_plist("/usr/bin/adapter&test", "/root")
        assert "&amp;" in text

    def test_interpreter_prefixes_program_arguments(self):
        """``interpreter`` — запуск backend-adapter.py интерпретатором (v1.0.3)."""
        import plistlib

        text = templates.render_launchd_plist(
            "/repo/backend-adapter.py",
            "/Users/test/.ba",
            interpreter="/usr/bin/python3",
        )
        data = plistlib.loads(text.encode("utf-8"))
        assert data["ProgramArguments"] == [
            "/usr/bin/python3",
            "/repo/backend-adapter.py",
            "--root",
            "/Users/test/.ba",
        ]

    def test_env_vars_block_is_valid_plist(self):
        """EnvironmentVariables — валидный plist-словарь (v1.0.3).

        Без ``ADAPTER_BACKEND_CONFIG`` агент падал на ``[FATAL] ... is not
        set``; значения plist побеждают ``adapter.env`` (``setdefault``).
        """
        import plistlib

        env = {
            "ADAPTER_BACKEND_CONFIG": "/Users/test/.ba/adapter.yaml",
            "ADAPTER_DETACH_ENABLE": "0",
        }
        text = templates.render_launchd_plist("/bin/adapter", "/Users/test/.ba", env_vars=env)
        data = plistlib.loads(text.encode("utf-8"))
        assert data["EnvironmentVariables"] == env

    def test_no_env_vars_block_by_default(self):
        import plistlib

        text = templates.render_launchd_plist("/bin/adapter", "/root")
        assert "EnvironmentVariables" not in plistlib.loads(text.encode("utf-8"))

    def test_env_vars_escaped_and_sorted(self):
        """XML-спецсимволы в значениях экранируются, ключи отсортированы."""
        import plistlib

        env = {"ZZ": "/path/with&special", "AA": "1"}
        text = templates.render_launchd_plist("/bin/adapter", "/root", env_vars=env)
        assert "&amp;" in text
        assert text.find("<key>AA</key>") < text.find("<key>ZZ</key>")
        assert plistlib.loads(text.encode("utf-8"))["EnvironmentVariables"] == env


class TestRenderLaunchdPlistFull:
    """render_launchd_plist_full — полный plist с инлайн env-переменными."""

    def test_contains_env_vars(self):
        env = {"ADAPTER_BACKEND_CONFIG": "/custom/adapter.yaml", "ADAPTER_DETACH_ENABLE": "1"}
        text = templates.render_launchd_plist_full(
            "/usr/local/bin/adapter",
            "/Users/test/.ba",
            env_vars=env,
        )
        assert "<key>ADAPTER_BACKEND_CONFIG</key>" in text
        assert "<string>/custom/adapter.yaml</string>" in text
        assert "<key>ADAPTER_DETACH_ENABLE</key>" in text
        assert "<string>1</string>" in text

    def test_default_env_vars(self):
        text = templates.render_launchd_plist_full("/bin/adapter", "/root")
        assert "ADAPTER_BACKEND_CONFIG" in text
        assert "ADAPTER_DETACH_ENABLE" in text

    def test_contains_root_argument(self):
        text = templates.render_launchd_plist_full("/usr/local/bin/adapter", "/Users/test/.ba")
        assert "<string>--root</string>" in text
        assert "<string>/Users/test/.ba</string>" in text

    def test_contains_working_directory(self):
        text = templates.render_launchd_plist_full("/bin/adapter", "/custom/root")
        assert "<key>WorkingDirectory</key>" in text
        assert "<string>/custom/root</string>" in text

    def test_log_paths(self, tmp_path):
        out = str(tmp_path / "log" / "out.log")
        err = str(tmp_path / "log" / "err.log")
        text = templates.render_launchd_plist_full(
            "/bin/adapter",
            "/root",
            standard_out_path=out,
            standard_error_path=err,
        )
        assert f"<string>{out}</string>" in text
        assert "<key>StandardOutPath</key>" in text

    def test_keep_alive_true_by_default(self):
        text = templates.render_launchd_plist_full("/bin/adapter", "/root")
        # keep_alive по умолчанию True — должен быть true
        assert "<true/>" in text

    def test_env_vars_sorted(self):
        env = {"ZZ_LAST": "z", "AA_FIRST": "a", "MM_MID": "m"}
        text = templates.render_launchd_plist_full("/bin/adapter", "/root", env_vars=env)
        aa_pos = text.find("AA_FIRST")
        mm_pos = text.find("MM_MID")
        zz_pos = text.find("ZZ_LAST")
        assert aa_pos < mm_pos < zz_pos

    def test_xml_escape_in_env_values(self):
        """Value с & должен быть экранирован."""
        env = {"ADAPTER_BACKEND_CONFIG": "/path/with&special.yaml"}
        text = templates.render_launchd_plist_full("/bin/adapter", "/root", env_vars=env)
        assert "&amp;" in text


class TestRenderSystemdUnit:
    """render_systemd_unit — user-level юнит с --root подходом (Linux)."""

    def test_sections(self):
        text = templates.render_systemd_unit("/usr/local/bin/backend-adapter", "/home/u/.ba")
        assert text.startswith("[Unit]\n")
        assert "[Service]\n" in text
        assert "[Install]\n" in text
        assert text.endswith("\n")

    def test_exec_start_has_root(self):
        text = templates.render_systemd_unit("/usr/local/bin/adapter", "/home/u/.ba")
        assert "ExecStart=/usr/local/bin/adapter --root /home/u/.ba\n" in text

    def test_working_directory_is_root(self):
        text = templates.render_systemd_unit("/bin/adapter", "/custom/root")
        assert "WorkingDirectory=/custom/root\n" in text

    def test_detach_disabled(self):
        """Environment=ADAPTER_DETACH_ENABLE=0 — обязательный инвариант."""
        text = templates.render_systemd_unit("/bin/adapter", "/root")
        assert "Environment=ADAPTER_DETACH_ENABLE=0\n" in text

    def test_restart_on_failure(self):
        text = templates.render_systemd_unit("/bin/adapter", "/root")
        assert "Restart=on-failure\n" in text
        assert "RestartSec=5\n" in text

    def test_restart_can_be_disabled(self):
        text = templates.render_systemd_unit("/bin/adapter", "/root", restart=False)
        assert "Restart=on-failure" not in text
        assert "RestartSec=" not in text

    def test_wanted_by_default_target(self):
        """User-level юнит: default.target (не multi-user.target системного)."""
        text = templates.render_systemd_unit("/bin/adapter", "/root")
        assert "WantedBy=default.target\n" in text

    def test_stdout_to_journal(self):
        text = templates.render_systemd_unit("/bin/adapter", "/root")
        assert "StandardOutput=journal\n" in text
        assert "StandardError=journal\n" in text

    def test_no_token_in_unit(self):
        """Токенов в юните нет — только путь к дому (--root подход)."""
        text = templates.render_systemd_unit("/bin/adapter", "/root")
        assert "ADAPTER_BACKEND_KEY" not in text

    def test_percent_escaped(self):
        """``%`` — спецификатор systemd: удваивается, чтобы не раскрывался."""
        text = templates.render_systemd_unit("/opt/100%/adapter", "/root")
        assert "/opt/100%%/adapter" in text
        assert "/opt/100%/adapter" not in text

    def test_spaces_quoted(self):
        """Путь с пробелом берётся в кавычки."""
        text = templates.render_systemd_unit("/opt/my app/adapter", "/home/u/.ba")
        assert 'ExecStart="/opt/my app/adapter" --root /home/u/.ba' in text

    def test_config_env_set_by_unit(self):
        """Юнит сам задаёт ADAPTER_BACKEND_CONFIG — не зависит от adapter.env."""
        text = templates.render_systemd_unit("/bin/adapter", "/home/u/.ba")
        assert 'Environment="ADAPTER_BACKEND_CONFIG=/home/u/.ba/adapter.yaml"\n' in text

    def test_interpreter_in_exec_start(self):
        """``interpreter`` — запуск backend-adapter.py интерпретатором (v1.0.3)."""
        text = templates.render_systemd_unit(
            "/repo/backend-adapter.py", "/home/u/.ba", interpreter="/usr/bin/python3"
        )
        assert "ExecStart=/usr/bin/python3 /repo/backend-adapter.py --root /home/u/.ba\n" in text

    def test_config_env_escaped(self):
        """``%`` и кавычка в пути дома экранируются в Environment=."""
        text = templates.render_systemd_unit("/bin/adapter", "/opt/100%/my ba")
        assert "Environment=\"ADAPTER_BACKEND_CONFIG=/opt/100%%/my ba/adapter.yaml\"\n" in text


class TestEscapePlistString:
    """_escape_plist_string — XML-экранирование специальных символов."""

    def test_normal_string(self):
        assert templates._escape_plist_string("hello") == "hello"

    def test_ampersand(self):
        assert templates._escape_plist_string("a&b") == "a&amp;b"

    def test_lt(self):
        assert templates._escape_plist_string("a<b") == "a&lt;b"

    def test_gt(self):
        assert templates._escape_plist_string("a>b") == "a&gt;b"

    def test_apostrophe(self):
        assert templates._escape_plist_string("it's") == "it&apos;s"

    def test_quote(self):
        assert templates._escape_plist_string('say "hi"') == "say &quot;hi&quot;"

    def test_all_special_chars(self):
        # Тестовая строка: < > & ' "
        s = '<>&\'"'
        result = templates._escape_plist_string(s)
        assert result == "&lt;&gt;&amp;&apos;&quot;"


class TestRenderNewsyslogConf:
    """render_newsyslog_conf — строка ротации лога сервиса (ADR-041)."""

    def _fields(self, text):
        """Поля последней (некомментарной) строки конфига."""
        line = [ln for ln in text.splitlines() if ln and not ln.startswith("#")][-1]
        return line.split()

    def test_full_line_with_pidfile(self):
        text = templates.render_newsyslog_conf(
            "/Users/u/Library/Logs/backend-adapter/adapter.log",
            owner_group="501:20",
            pid_file="/Users/u/.ba/var/adapter.pid",
        )
        assert self._fields(text) == [
            "/Users/u/Library/Logs/backend-adapter/adapter.log",
            "501:20",
            "600",
            "5",
            "1024",
            "*",
            "BZ",
            "/Users/u/.ba/var/adapter.pid",
            "1",
        ]

    def test_header_documents_commands(self):
        text = templates.render_newsyslog_conf("/tmp/a.log", owner_group="0:0")
        assert "--launchd log-rotate" in text
        assert "--launchd log-rotate-remove" in text
        assert text.endswith("\n")

    def test_without_pidfile_adds_N_and_drops_signal(self):
        """Без PID newsyslog не смог бы уведомить процесс — только флаг N."""
        fields = self._fields(templates.render_newsyslog_conf("/tmp/a.log", owner_group="0:0"))
        assert "N" in fields[6]
        assert fields[-1] == fields[6]  # номера сигнала нет

    def test_never_uses_signal_zero(self):
        """`0` newsyslog отвергает: «illegal signal number»."""
        for pid in (None, "/tmp/a.pid"):
            text = templates.render_newsyslog_conf("/tmp/a.log", owner_group="0:0", pid_file=pid)
            assert self._fields(text)[-1] != "0"

    def test_custom_policy(self):
        fields = self._fields(
            templates.render_newsyslog_conf(
                "/tmp/a.log", owner_group="0:0", count=3, size_kb=50, flags="Z"
            )
        )
        assert fields[3] == "3" and fields[4] == "50"
        assert "N" in fields[6] and "Z" in fields[6]

    def test_mode_is_octal(self):
        fields = self._fields(
            templates.render_newsyslog_conf("/tmp/a.log", owner_group="0:0", mode=0o640)
        )
        assert fields[2] == "640"

    def test_space_in_path_is_rejected(self):
        with pytest.raises(ValueError):
            templates.render_newsyslog_conf("/tmp/my log.log", owner_group="0:0")

    def test_hash_in_path_escaped(self):
        fields = self._fields(templates.render_newsyslog_conf("/tmp/a#b.log", owner_group="0:0"))
        assert fields[0] == "/tmp/a\\#b.log"
