#!/usr/bin/env python3
"""Tests for backend_adapter.cli_args — CLI-ключи адаптера (v0.9.11).

Модуль — лист DAG (stdlib + ``templates``): импортируется на уровне файла,
свежесть через fresh_env не нужна. Проверяем разбор argv: информационные ключи
завершают процесс кодом 0, неизвестный — кодом 2, пустой argv — обычный старт.
Ключи ``--root``/``--install`` трогают ``os.environ`` и файловую систему,
поэтому каждый тест изолирован: fixture подменяет ``os.environ`` приватной
копией (cli_args пишет в неё через ``setdefault`` — реальное окружение не
задето), каталог — ``tmp_path``.
"""

import os

import pytest

from backend_adapter import cli_args, templates

# Переменные, которые тесты выставляют сами: снимаем их из окружения, чтобы
# реальный adapter.env разработчика не влиял на результат.
_CLEAN_ENV = (
    "ADAPTER_DATA_ROOT",
    "ADAPTER_BACKEND_CONFIG",
    "ADAPTER_MODELS_TARIFFS",
    "ADAPTER_PROXY_PORT",
    "ADAPTER_ENDPOINT_HOST",
)


@pytest.fixture
def clean_env(monkeypatch):
    """Приватная копия окружения без переменных дома: правки не утекают."""
    monkeypatch.setattr(os, "environ", dict(os.environ))
    for name in _CLEAN_ENV:
        os.environ.pop(name, None)


class TestVersion:
    @pytest.mark.parametrize("flag", ["-v", "--version"])
    def test_prints_version_and_exits_zero(self, flag, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args([flag], "9.9.9")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "9.9.9" in out
        assert "backend-adapter" in out


class TestHelp:
    @pytest.mark.parametrize("flag", ["-h", "--help", "-?"])
    def test_prints_usage_and_exits_zero(self, flag, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args([flag], "9.9.9")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        # Справка перечисляет все информационные ключи с пояснениями.
        for key in (
            "--version",
            "--help",
            "--root",
            "--install",
            "--name",
            "--base",
            "--key",
            "--check",
        ):
            assert key in out


class TestUnknownOption:
    @pytest.mark.parametrize("flag", ["--nope", "-x", "positional"])
    def test_unknown_gets_fatal_and_exit_two(self, flag, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args([flag], "9.9.9")
        assert ei.value.code == 2
        out = capsys.readouterr().out
        assert "[FATAL]" in out and flag in out
        assert "--help" in out  # подсказка, куда смотреть


class TestNoArgs:
    def test_empty_argv_returns_without_exit(self, clean_env):
        # Обычный запуск адаптера: ключей нет — parse_args просто возвращается
        # (обратная совместимость, tests/test_manual_check.py запускает скрипт
        # без аргументов) и дом не активирует.
        assert cli_args.parse_args([], "9.9.9") is None
        assert "ADAPTER_DATA_ROOT" not in os.environ


class TestRoot:
    def test_missing_argument_gets_fatal_and_exit_two(self, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--root"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_sets_data_root_and_backend_config(self, tmp_path, clean_env):
        root = str(tmp_path / "home")
        assert cli_args.parse_args(["--root", root], "9.9.9") is None
        assert os.environ["ADAPTER_DATA_ROOT"] == root
        assert os.environ["ADAPTER_BACKEND_CONFIG"] == os.path.join(root, "adapter.yaml")

    def test_explicit_env_wins(self, tmp_path, clean_env):
        os.environ["ADAPTER_DATA_ROOT"] = "/explicit/data"
        os.environ["ADAPTER_BACKEND_CONFIG"] = "/explicit/adapter.yaml"
        cli_args.parse_args(["--root", str(tmp_path / "home")], "9.9.9")
        assert os.environ["ADAPTER_DATA_ROOT"] == "/explicit/data"
        assert os.environ["ADAPTER_BACKEND_CONFIG"] == "/explicit/adapter.yaml"

    def test_env_file_fills_gaps_only(self, tmp_path, clean_env):
        os.environ["ADAPTER_PROXY_PORT"] = "2222"  # env побеждает файл
        root = tmp_path / "home"
        root.mkdir()
        (root / "adapter.env").write_text(
            "# комментарий\n"
            "export ADAPTER_PROXY_PORT=1111\n"
            "ADAPTER_ENDPOINT_HOST='from-file'\n"
            "ADAPTER_DATA_ROOT=/from/env/file\n"
            "\n",
            encoding="utf-8",
        )
        cli_args.parse_args(["--root", str(root)], "9.9.9")
        assert os.environ["ADAPTER_PROXY_PORT"] == "2222"  # env сильнее
        assert os.environ["ADAPTER_ENDPOINT_HOST"] == "from-file"  # кавычки сняты
        # Файл задал ADAPTER_DATA_ROOT — он побеждает подставленный по умолчанию.
        assert os.environ["ADAPTER_DATA_ROOT"] == "/from/env/file"

    def test_tariffs_only_when_file_exists(self, tmp_path, clean_env):
        root = tmp_path / "home"
        root.mkdir()
        cli_args.parse_args(["--root", str(root)], "9.9.9")
        assert "ADAPTER_MODELS_TARIFFS" not in os.environ  # файла нет — не подставляем
        (root / "tariffs.yaml").write_text("tariffs: []\n", encoding="utf-8")
        cli_args.parse_args(["--root", str(root)], "9.9.9")
        assert os.environ["ADAPTER_MODELS_TARIFFS"] == str(root / "tariffs.yaml")


class TestInstall:
    def test_lays_out_home(self, tmp_path, clean_env, capsys):
        root = tmp_path / "home"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0

        assert (root / "adapter.env").is_file()
        assert (root / "adapter.yaml").is_file()
        assert (root / "tariffs.yaml").is_file()
        assert (root / "log").is_dir()
        assert (root / "var").is_dir()

        env_text = (root / "adapter.env").read_text(encoding="utf-8")
        assert str(root / "adapter.yaml") in env_text
        assert str(root / "tariffs.yaml") in env_text
        assert str(root) in env_text
        # Токен — заглушка, не пустое значение.
        assert "ADAPTER_BACKEND_KEY_LLM_SERVICE='*****'" in env_text

        # v0.9.12: файлы кладутся из встроенных шаблонов — ровно те же байты,
        # что и в docs/samples (иначе из бинарника лёг бы иной текст).
        assert (root / "adapter.yaml").read_text(encoding="utf-8") == templates.SAMPLE_ADAPTER_YAML
        assert (root / "tariffs.yaml").read_text(encoding="utf-8") == templates.SAMPLE_TARIFFS_YAML

        # В adapter.env попадает токен — файл закрыт для остальных.
        assert (root / "adapter.env").stat().st_mode & 0o777 == 0o600

        assert "[OK]" in capsys.readouterr().out

    def test_backs_up_edited_file(self, tmp_path, clean_env, capsys):
        root = tmp_path / "home"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--install", "--root", str(root)], "9.9.9")
        (root / "adapter.env").write_text("edited: keep me\n", encoding="utf-8")
        capsys.readouterr()

        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        # Правка пользователя сохранена в .bak, файл перезаписан шаблоном.
        assert (root / "adapter.env.bak").read_text(encoding="utf-8") == "edited: keep me\n"
        assert (root / "adapter.env").read_text(encoding="utf-8") != "edited: keep me\n"
        assert "[WARN]" in out and ".bak" in out

    def test_backup_keeps_mode(self, tmp_path, clean_env):
        # adapter.env несёт токен: копия .bak должна остаться 0600 (copy2).
        root = tmp_path / "home"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--install", "--root", str(root)], "9.9.9")
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--install", "--root", str(root)], "9.9.9")
        assert (root / "adapter.env.bak").stat().st_mode & 0o777 == 0o600

    def test_fresh_install_has_no_backups(self, tmp_path, clean_env):
        # Первый --install ничего не бэкапит: копировать было нечего.
        root = tmp_path / "home"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--install", "--root", str(root)], "9.9.9")
        assert not (root / "adapter.env.bak").exists()
        assert not (root / "adapter.yaml.bak").exists()
        assert not (root / "tariffs.yaml.bak").exists()

    def test_default_root_is_home_ba(self, tmp_path, clean_env, monkeypatch, capsys):
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--install"], "9.9.9")
        assert ei.value.code == 0
        assert (tmp_path / ".ba" / "adapter.env").is_file()

    def test_install_works_without_docs_samples(self, tmp_path, clean_env, monkeypatch, capsys):
        # v0.9.12: шаблоны встроены в код (templates.py), docs/samples/ в
        # рантайме не читается. Подменяем __file__ в несуществующий каталог
        # (аналог standalone-бинарника/wheel) — --install всё равно размечает
        # дом и завершается кодом 0, без [FATAL].
        monkeypatch.setattr(cli_args, "__file__", str(tmp_path / "pkg" / "cli_args.py"))
        root = tmp_path / "home"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert (root / "adapter.yaml").is_file()
        assert (root / "tariffs.yaml").is_file()
        assert (root / "adapter.env").is_file()
        assert "[FATAL]" not in capsys.readouterr().out


class TestDiscoverHome:
    """Без --root дом ищется: текущая папка → ~/.ba (v0.9.13).

    Признак дома — adapter.env или adapter.yaml. Явный --root важнее поиска;
    явный env важнее дома (setdefault). Ничего не найдено — дом не
    подключается, работает нулевой запуск.
    """

    def test_current_dir_env_file_wins(self, tmp_path, clean_env, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        (work / "adapter.env").write_text(
            "export ADAPTER_PROXY_PORT=1111\n", encoding="utf-8"
        )
        monkeypatch.chdir(work)
        cli_args.parse_args([], "9.9.9")
        assert os.environ["ADAPTER_PROXY_PORT"] == "1111"
        assert os.environ["ADAPTER_DATA_ROOT"] == str(work)
        assert os.environ["ADAPTER_BACKEND_CONFIG"] == str(work / "adapter.yaml")

    def test_current_dir_yaml_is_a_marker(self, tmp_path, clean_env, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        (work / "adapter.yaml").write_text("backend: []\n", encoding="utf-8")
        monkeypatch.chdir(work)
        cli_args.parse_args([], "9.9.9")
        assert os.environ["ADAPTER_DATA_ROOT"] == str(work)

    def test_falls_back_to_home_ba(self, tmp_path, clean_env, monkeypatch):
        # CWD без маркеров, в ~/.ba — маркер: дом найден в ~/.ba.
        work = tmp_path / "work"
        work.mkdir()
        home = tmp_path / "home"
        (home / ".ba").mkdir(parents=True)
        (home / ".ba" / "adapter.env").write_text("", encoding="utf-8")
        monkeypatch.chdir(work)
        monkeypatch.setenv("HOME", str(home))
        cli_args.parse_args([], "9.9.9")
        assert os.environ["ADAPTER_DATA_ROOT"] == str(home / ".ba")

    def test_no_home_anywhere_stays_unset(self, tmp_path, clean_env, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.chdir(work)
        monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
        cli_args.parse_args([], "9.9.9")
        # Дом не подключён — ADAPTER_DATA_ROOT не выставлен (дефолт даст config).
        assert "ADAPTER_DATA_ROOT" not in os.environ
        assert "ADAPTER_BACKEND_CONFIG" not in os.environ

    def test_explicit_root_beats_discovery(self, tmp_path, clean_env, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        (work / "adapter.env").write_text(
            "export ADAPTER_PROXY_PORT=1111\n", encoding="utf-8"
        )
        monkeypatch.chdir(work)
        explicit = tmp_path / "explicit"
        cli_args.parse_args(["--root", str(explicit)], "9.9.9")
        assert os.environ["ADAPTER_DATA_ROOT"] == str(explicit)
        assert "ADAPTER_PROXY_PORT" not in os.environ  # файл CWD не прочитан

    def test_explicit_env_beats_discovered_home(self, tmp_path, clean_env, monkeypatch):
        work = tmp_path / "work"
        work.mkdir()
        (work / "adapter.env").write_text(
            "export ADAPTER_BACKEND_CONFIG=/from/file\n", encoding="utf-8"
        )
        monkeypatch.chdir(work)
        os.environ["ADAPTER_BACKEND_CONFIG"] = "/from/env"
        cli_args.parse_args([], "9.9.9")
        assert os.environ["ADAPTER_BACKEND_CONFIG"] == "/from/env"  # env сильнее


class TestInstallBackend:
    """--install --name/--base/--key (v0.9.13): генерация adapter.yaml.

    Ключи осмысленны только вместе с --install и только все три сразу;
    без них --install кладёт образцовый SAMPLE_ADAPTER_YAML (см. TestInstall).
    """

    def test_all_three_generate_backend_yaml(self, tmp_path, clean_env, capsys):
        root = tmp_path / "home"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(
                [
                    "--install",
                    "--root",
                    str(root),
                    "--name",
                    "demo",
                    "--base",
                    "https://x.example",
                    "--key",
                    "ADAPTER_DEMO_KEY",
                ],
                "9.9.9",
            )
        assert ei.value.code == 0
        yaml_text = (root / "adapter.yaml").read_text(encoding="utf-8")
        assert "name: demo" in yaml_text
        assert "base: https://x.example" in yaml_text
        assert "key: ADAPTER_DEMO_KEY" in yaml_text
        # Имя токена из --key подставлено и в adapter.env.
        env_text = (root / "adapter.env").read_text(encoding="utf-8")
        assert "ADAPTER_DEMO_KEY='*****'" in env_text
        assert str(root) in env_text  # ADAPTER_DATA_ROOT = <root>

    def test_custom_key_replaces_default_env_name(self, tmp_path, clean_env, capsys):
        root = tmp_path / "home"
        with pytest.raises(SystemExit):
            cli_args.parse_args(
                [
                    "--install",
                    "--root",
                    str(root),
                    "--name",
                    "n",
                    "--base",
                    "http://b",
                    "--key",
                    "MY_KEY",
                ],
                "9.9.9",
            )
        env_text = (root / "adapter.env").read_text(encoding="utf-8")
        assert "export MY_KEY='*****'" in env_text
        # Образцовая переменная токена не остаётся.
        assert "ADAPTER_BACKEND_KEY_LLM_SERVICE" not in env_text

    @pytest.mark.parametrize(
        "partial",
        [
            ["--name", "demo"],
            ["--base", "https://x.example"],
            ["--key", "ADAPTER_DEMO_KEY"],
            ["--name", "demo", "--base", "https://x.example"],
        ],
    )
    def test_partial_set_is_fatal(self, partial, tmp_path, clean_env, capsys):
        root = tmp_path / "home"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--install", "--root", str(root), *partial], "9.9.9")
        assert ei.value.code == 2
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "--name/--base/--key" in out
        assert not root.exists()  # дом не тронут

    @pytest.mark.parametrize("flag", ["--name", "--base", "--key"])
    def test_without_install_is_fatal(self, flag, tmp_path, clean_env, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--root", str(tmp_path / "home"), flag, "x"], "9.9.9")
        assert ei.value.code == 2
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "--install" in out

    def test_missing_value_is_fatal(self, clean_env, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--install", "--name"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out


class TestCheck:
    """--check (v0.9.13): офлайн-диагностика дома.

    Код выхода — ровно код ``env_check.run_check`` (1 при [ERROR], иначе 0);
    ключ несовместим с --install и --name/--base/--key. Запускается до
    validate_env/импорта config, поэтому обязан пережить битый env.
    """

    @staticmethod
    def _clear_adapter_vars():
        """Убрать живые ADAPTER_* (dev-шелл дал бы недетерминированные WARN)."""
        for name in [k for k in os.environ if k.startswith("ADAPTER_")]:
            os.environ.pop(name, None)

    def test_dispatches_with_resolved_root(self, tmp_path, clean_env, monkeypatch):
        seen: dict[str, str] = {}

        def fake_run_check(path: str) -> int:
            seen["path"] = path
            return 0

        monkeypatch.setattr(cli_args.env_check, "run_check", fake_run_check)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--check", "--root", str(tmp_path)], "9.9.9")
        assert ei.value.code == 0
        assert seen["path"] == str(tmp_path)

    def test_exit_code_propagated(self, tmp_path, clean_env, monkeypatch):
        monkeypatch.setattr(cli_args.env_check, "run_check", lambda path: 1)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--check", "--root", str(tmp_path)], "9.9.9")
        assert ei.value.code == 1

    def test_defaults_to_home_ba(self, tmp_path, clean_env, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        seen: dict[str, str] = {}

        def fake_run_check(path: str) -> int:
            seen["path"] = path
            return 0

        monkeypatch.setattr(cli_args.env_check, "run_check", fake_run_check)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--check"], "9.9.9")
        assert ei.value.code == 0
        assert seen["path"] == str(tmp_path / ".ba")

    def test_empty_root_warns_but_zero(self, tmp_path, clean_env, capsys):
        self._clear_adapter_vars()
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--check", "--root", str(tmp_path)], "9.9.9")
        assert ei.value.code == 0
        assert "[WARN]" in capsys.readouterr().out

    def test_broken_env_reports_error_not_crash(self, tmp_path, clean_env, monkeypatch, capsys):
        """--check идёт ДО validate_env: битый int — строка отчёта, не ValueError."""
        self._clear_adapter_vars()
        monkeypatch.setenv("ADAPTER_PROXY_PORT", "abc")
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--check", "--root", str(tmp_path)], "9.9.9")
        assert ei.value.code == 1
        assert "[ERROR]" in capsys.readouterr().out

    def test_incompatible_with_install(self, clean_env, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--check", "--install"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_incompatible_with_backend_keys(self, clean_env, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(
                ["--check", "--name", "n", "--base", "http://b", "--key", "K"], "9.9.9"
            )
        assert ei.value.code == 2
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "--check" in out
