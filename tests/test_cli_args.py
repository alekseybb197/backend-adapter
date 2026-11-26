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


@pytest.fixture
def fake_binary(monkeypatch, tmp_path):
    """Подменить поиск бинарника: тесты не зависят от PATH машины.

    ``_find_binary`` выбирает бинарник и пробует его ``--help`` — в процессе
    pytest ``sys.argv[0]`` не адаптер, а в PATH разработчика может лежать
    устаревшая версия. Возвращаем фиксированный путь без пробы; сам
    ``_find_binary`` проверяется отдельным классом ``TestFindBinary``.
    """
    binary = str(tmp_path / "bin" / "backend-adapter")

    def fake_find(root: str) -> tuple[str, str | None]:
        # Реальный _find_binary сообщает выбранный путь — сохраняем контракт.
        print(f"[OK]   Бинарник: {binary}")
        return binary, None

    monkeypatch.setattr(cli_args, "_find_binary", fake_find)
    return binary


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


class TestLaunchd:
    """--launchd (v1.0.2): управление launchd-агентом macOS.

    plist пишется в ``~/Library/LaunchAgents`` — тесты подменяют HOME на
    ``tmp_path``. Подкоманды ``load``/``unload``/``status`` зовут ``launchctl``
    и здесь не проверяются (на CI его нет); проверяем разбор argv и
    генерацию/установку plist, дом не трогается у ``plist-install``.
    """

    @pytest.fixture
    def home(self, tmp_path, clean_env, fake_binary, monkeypatch):
        """HOME → tmp_path: plist ляжет в tmp_path/Library/LaunchAgents."""
        monkeypatch.setenv("HOME", str(tmp_path))
        return tmp_path

    @staticmethod
    def _plist(home):
        return home / "Library" / "LaunchAgents" / "com.user.backend-adapter.plist"

    def test_unknown_action_is_fatal(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "bogus"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_incompatible_with_install(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "install", "--install"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_incompatible_with_check(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "install", "--check"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_bare_flag_defaults_to_install(self, home, capsys):
        """--launchd без подкоманды (так зовёт install.sh) = install."""
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        # install размечает дом и ставит plist.
        assert (root / "adapter.env").is_file()
        assert self._plist(home).is_file()

    def test_install_writes_plist_with_root(self, home, capsys):
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        text = self._plist(home).read_text(encoding="utf-8")
        assert "--root" in text and str(root) in text
        # Токенов в plist нет — только путь к дому (--root подход).
        assert "ADAPTER_BACKEND_KEY" not in text

    def test_install_writes_service_env_to_plist(self, home, capsys):
        """plist сам задаёт CONFIG и DETACH: агент не зависит от adapter.env.

        Регрессия v1.0.3: без ``ADAPTER_BACKEND_CONFIG`` агент падал на
        ``[FATAL] ... is not set``, а при ``ADAPTER_DETACH_ENABLE=1`` в
        ``adapter.env`` делал двойной форк и выпадал из-под надзора launchd.
        """
        import plistlib

        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        data = plistlib.loads(self._plist(home).read_bytes())
        env = data["EnvironmentVariables"]
        assert env["ADAPTER_BACKEND_CONFIG"] == str(root / "adapter.yaml")
        assert env["ADAPTER_DETACH_ENABLE"] == "0"
        # Токенов по-прежнему нет.
        assert not any("KEY" in name for name in env)

    def test_install_reports_chosen_binary(self, home, capsys, fake_binary):
        """install печатает, какой бинарник попал в plist (диагностика)."""
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert fake_binary in capsys.readouterr().out

    def test_plist_install_requires_marked_home(self, home, capsys):
        root = home / ".ba"
        root.mkdir()
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(
                ["--launchd", "plist-install", "--root", str(root)], "9.9.9"
            )
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "--install" in out
        assert not self._plist(home).exists()

    def test_plist_install_does_not_touch_home(self, home, capsys):
        """Главная гарантия: готовый дом не переразмечается."""
        root = home / ".ba"
        root.mkdir()
        env_path = root / "adapter.env"
        env_path.write_text("export ADAPTER_PROXY_PORT=4242\n", encoding="utf-8")

        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(
                ["--launchd", "plist-install", "--root", str(root)], "9.9.9"
            )
        assert ei.value.code == 0
        # adapter.env не тронут, .bak не появился, шаблоны не досозданы.
        assert env_path.read_text(encoding="utf-8") == "export ADAPTER_PROXY_PORT=4242\n"
        assert not (root / "adapter.env.bak").exists()
        assert not (root / "adapter.yaml").exists()
        # plist при этом установлен.
        assert self._plist(home).is_file()

    def test_existing_plist_backed_up(self, home, capsys):
        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        plist = self._plist(home)
        plist.write_text("old plist\n", encoding="utf-8")
        capsys.readouterr()

        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert (home / "Library" / "LaunchAgents" / "com.user.backend-adapter.plist.bak").read_text(
            encoding="utf-8"
        ) == "old plist\n"
        assert "[WARN]" in capsys.readouterr().out

    def test_install_writes_log_path_to_plist(self, home, capsys):
        """launchd без StandardOutPath теряет вывод — plist задаёт файл лога.

        Лог сервиса живёт **вне** дома (``~/Library/Logs/backend-adapter``):
        ``<root>/log`` — каталог данных работающего адаптера, смешивать туда
        вывод launchd нельзя.
        """
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        text = self._plist(home).read_text(encoding="utf-8")
        log_path = str(home / "Library" / "Logs" / "backend-adapter" / "adapter.log")
        assert "<key>StandardOutPath</key>" in text
        assert "<key>StandardErrorPath</key>" in text
        assert text.count(log_path) == 2
        # Каталог лога создан заранее — launchd не создаёт его сам.
        assert (home / "Library" / "Logs" / "backend-adapter").is_dir()
        # Домашний log/ сервисным логом не засоряется.
        assert not (root / "log" / "adapter.log").exists()

    def test_logs_tail_default_depth(self, home, capsys, monkeypatch):
        """logs по умолчанию — tail последних 20 строк, без -F."""
        root = home / ".ba"
        log_dir = home / "Library" / "Logs" / "backend-adapter"
        log_dir.mkdir(parents=True)
        log = log_dir / "adapter.log"
        log.write_text("line\n", encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            import subprocess as sp

            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "logs", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert calls == [["tail", "-n20", str(log)]]

    def test_logs_custom_depth_and_follow(self, home, capsys, monkeypatch):
        root = home / ".ba"
        log_dir = home / "Library" / "Logs" / "backend-adapter"
        log_dir.mkdir(parents=True)
        log = log_dir / "adapter.log"
        log.write_text("line\n", encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            import subprocess as sp

            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(
                ["--launchd", "logs", "-n", "5", "-f", "--root", str(root)], "9.9.9"
            )
        assert ei.value.code == 0
        assert calls == [["tail", "-n5", "-F", str(log)]]

    def test_logs_missing_file_warns(self, home, capsys, monkeypatch):
        root = home / ".ba"
        root.mkdir()
        called = []
        monkeypatch.setattr(cli_args.subprocess, "run", lambda *a, **k: called.append(a))
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "logs", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "[WARN]" in out and "adapter.log" in out
        assert not called

    def test_load_bootstraps_and_kickstarts(self, home, capsys, monkeypatch):
        """load = bootstrap (регистрация) + kickstart -p (реальный запуск).

        ``RunAtLoad=false`` в plist означает, что одна регистрация процесса не
        поднимает — без ``kickstart`` агент остался бы ``not running``.
        """
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        capsys.readouterr()

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            if cmd[:2] == ["launchctl", "print"]:
                return sp.CompletedProcess(cmd, 113, "", "Could not find service")
            if cmd[:2] == ["launchctl", "kickstart"]:
                return sp.CompletedProcess(cmd, 0, "4242\n", "")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "load", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        verbs = [c[1] for c in calls]
        assert verbs == ["print", "bootstrap", "kickstart"]
        assert calls[1][2].startswith("gui/")
        assert calls[1][3] == str(self._plist(home))
        assert calls[2] == ["launchctl", "kickstart", "-p", calls[1][2] + "/com.user.backend-adapter"]
        assert "4242" in capsys.readouterr().out

    def test_load_skips_bootstrap_when_registered(self, home, capsys, monkeypatch):
        """Уже зарегистрированный агент: bootstrap не повторяется (rc 5), стартует kickstart."""
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        capsys.readouterr()

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return sp.CompletedProcess(cmd, 0, "7\n", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "load", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert [c[1] for c in calls] == ["print", "kickstart"]

    def test_load_requires_plist(self, home, capsys):
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "load", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        assert "[FATAL]" in capsys.readouterr().out

    def test_uninstall_removes_plist_and_backup(self, home, capsys, monkeypatch):
        """uninstall: bootout + удаление plist и .bak, дом не тронут."""
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        plist = self._plist(home)
        plist.write_text("old plist\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        backup = plist.with_name(plist.name + ".bak")
        assert plist.is_file() and backup.is_file()
        env_before = (root / "adapter.env").read_text(encoding="utf-8")
        capsys.readouterr()

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not plist.exists() and not backup.exists()
        assert [c[1] for c in calls] == ["print", "bootout"]
        # Дом (токены, конфиги) не тронут.
        assert (root / "adapter.env").read_text(encoding="utf-8") == env_before
        assert "Дом не изменён" in capsys.readouterr().out

    def test_status_prints_log_tail_when_not_running(self, home, capsys, monkeypatch):
        """Не работающий агент: status печатает хвост лога сразу (v1.0.3).

        Причина падения агента — в логе; заставлять пользователя звать
        ``logs`` отдельно значит терять шаг диагностики.
        """
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        log_dir = home / "Library" / "Logs" / "backend-adapter"
        (log_dir / "adapter.log").write_text(
            "[FATAL] ADAPTER_BACKEND_CONFIG is not set\n", encoding="utf-8"
        )
        capsys.readouterr()

        def fake_run(cmd, *args, **kwargs):
            # Агент зарегистрирован, но упал.
            return sp.CompletedProcess(cmd, 0, "state = exited\nlast exit code = 1\n", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "status", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "[FATAL] ADAPTER_BACKEND_CONFIG is not set" in out
        assert "Последние строки лога" in out

    def test_uninstall_when_not_installed_is_noop(self, home, capsys, monkeypatch):
        """Повторный uninstall: агент не зарегистрирован, plist нет — чистый no-op."""
        import subprocess as sp

        root = home / ".ba"
        root.mkdir()

        def fake_run(cmd, *args, **kwargs):
            return sp.CompletedProcess(cmd, 113, "", "Could not find service")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "не был зарегистрирован" in out
        assert "удалять нечего" in out


class TestFindBinary:
    """_find_binary (v1.0.3): приоритет собственного бинарника + проба --root.

    Корень регрессии: в PATH лежал standalone-бинарник v0.8.0 без CLI. Он
    молча игнорировал ``--root`` и падал на пустом ``ADAPTER_BACKEND_CONFIG``.
    Теперь каждый кандидат проверяется пробой ``--help``, а несовместимый
    бинарник — ``[FATAL]``.
    """

    @staticmethod
    def _fake_run(responses):
        """Подменить subprocess.run: ответ по первому аргументу после бинарника."""

        def run(cmd, *args, **kwargs):
            import subprocess as sp

            flag = cmd[-1]
            rc, out = responses.get(flag, (1, ""))
            return sp.CompletedProcess(cmd, rc, out, "")

        return run

    def test_prefers_current_script_with_interpreter(self, tmp_path, monkeypatch, capsys):
        """Запуск ``python backend-adapter.py`` → скрипт + интерпретатор."""
        script = tmp_path / "backend-adapter.py"
        script.write_text("# script\n", encoding="utf-8")
        monkeypatch.setattr(cli_args.sys, "argv", [str(script)])
        monkeypatch.setattr(cli_args.sys, "frozen", False, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", "/usr/bin/python3")
        monkeypatch.setattr(cli_args.os.path, "isfile", lambda p: p == str(script))
        monkeypatch.setattr(
            cli_args.subprocess,
            "run",
            self._fake_run({"--help": (0, "Usage: ... --root <путь>"), "--version": (0, "v9\n")}),
        )
        assert cli_args._find_binary("/home/u/.ba") == (str(script), "/usr/bin/python3")
        assert "Бинарник:" in capsys.readouterr().out

    def test_frozen_uses_executable(self, tmp_path, monkeypatch):
        """PyInstaller-сборка: ``sys.executable`` и есть бинарник."""
        monkeypatch.setattr(cli_args.sys, "frozen", True, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", "/opt/ba/backend-adapter")
        monkeypatch.setattr(cli_args.sys, "argv", ["/opt/ba/backend-adapter"])
        monkeypatch.setattr(
            cli_args.os.path, "isfile", lambda p: p == "/opt/ba/backend-adapter"
        )
        monkeypatch.setattr(
            cli_args.subprocess, "run", self._fake_run({"--help": (0, " --root ")})
        )
        assert cli_args._find_binary("/home/u/.ba") == ("/opt/ba/backend-adapter", None)

    def test_rejects_binary_without_root(self, tmp_path, monkeypatch, capsys):
        """Старый бинарник (нет --root в справке) — [FATAL] exit 1."""
        monkeypatch.setattr(cli_args.sys, "frozen", True, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", "/opt/old/backend-adapter")
        monkeypatch.setattr(cli_args.sys, "argv", ["/opt/old/backend-adapter"])
        monkeypatch.setattr(cli_args.os.path, "isfile", lambda p: True)
        monkeypatch.setattr(
            cli_args.subprocess,
            "run",
            self._fake_run({"--help": (0, "Usage: backend-adapter  [FATAL] ADAPTER_BACKEND_CONFIG")}),
        )
        with pytest.raises(SystemExit) as ei:
            cli_args._find_binary("/home/u/.ba")
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "не понимает --root" in out

    def test_timeout_is_rejection(self, monkeypatch, capsys):
        """Старый бинарник без CLI висит сервером — таймаут = несовместим."""
        import subprocess as sp

        monkeypatch.setattr(cli_args.sys, "frozen", True, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", "/opt/old/backend-adapter")
        monkeypatch.setattr(cli_args.sys, "argv", ["/opt/old/backend-adapter"])
        monkeypatch.setattr(cli_args.os.path, "isfile", lambda p: True)

        def boom(cmd, *args, **kwargs):
            raise sp.TimeoutExpired(cmd, 1)

        monkeypatch.setattr(cli_args.subprocess, "run", boom)
        with pytest.raises(SystemExit) as ei:
            cli_args._find_binary("/home/u/.ba")
        assert ei.value.code == 1
        assert "[WARN]" in capsys.readouterr().out

    def test_falls_back_to_script_in_home(self, tmp_path, monkeypatch, capsys):
        """Несовместимый self-бинарник, но в доме есть скрипт — берём скрипт."""
        root = tmp_path / ".ba"
        root.mkdir()
        script = root / "backend-adapter.py"
        script.write_text("# script\n", encoding="utf-8")
        old = tmp_path / "old-backend-adapter"
        old.write_text("# old\n", encoding="utf-8")
        monkeypatch.setattr(cli_args.sys, "frozen", True, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", str(old))
        monkeypatch.setattr(cli_args.sys, "argv", [str(old)])
        # which("python3") нужен для интерпретатора скрипта; остальное — None.
        monkeypatch.setattr(
            cli_args.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None
        )

        def run(cmd, *args, **kwargs):
            import subprocess as sp

            if cmd[0] == str(old):
                return sp.CompletedProcess(cmd, 1, "", "")  # старый: без CLI
            return sp.CompletedProcess(cmd, 0, " --root ", "")

        monkeypatch.setattr(cli_args.subprocess, "run", run)
        assert cli_args._find_binary(str(root)) == (str(script), "/usr/bin/python3")

    def test_not_found_warns(self, tmp_path, monkeypatch, capsys):
        """Ничего не найдено — [WARN], путь по умолчанию (как раньше)."""
        monkeypatch.setattr(cli_args.sys, "frozen", False, raising=False)
        monkeypatch.setattr(cli_args.sys, "argv", ["pytest"])
        monkeypatch.setattr(cli_args.os.path, "isfile", lambda p: False)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: None)
        binary, interpreter = cli_args._find_binary(str(tmp_path))
        assert binary == str(tmp_path / "backend-adapter.py")
        assert interpreter == cli_args.sys.executable
        assert "[WARN]" in capsys.readouterr().out


class TestLogsFlags:
    """-n/--lines и -f/--follow (v1.0.2) — только вместе с подкомандой logs."""

    @pytest.fixture
    def home(self, tmp_path, clean_env, monkeypatch):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        return tmp_path

    def test_lines_without_logs_is_fatal(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "status", "-n", "5"], "9.9.9")
        assert ei.value.code == 2
        assert "logs" in capsys.readouterr().out

    def test_follow_without_service_flag_is_fatal(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["-f"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_lines_non_integer_is_fatal(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "logs", "-n", "abc"], "9.9.9")
        assert ei.value.code == 2
        assert "целое" in capsys.readouterr().out

    def test_lines_non_positive_is_fatal(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "logs", "-n", "0"], "9.9.9")
        assert ei.value.code == 2
        assert "> 0" in capsys.readouterr().out

    def test_lines_requires_value(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "logs", "-n"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out


class TestSystemd:
    """--systemd (v1.0.2): user-level systemd-юнит Linux (аналог --launchd).

    Юнит пишется в ``$XDG_CONFIG_HOME/systemd/user`` — тесты подменяют
    ``XDG_CONFIG_HOME`` на ``tmp_path``. Подкоманды ``start``/``stop``/``status``
    зовут ``systemctl --user`` и здесь не проверяются (на CI/macOS его нет);
    проверяем разбор argv и генерацию/установку юнита, дом не трогается у
    ``unit-install``.
    """

    @pytest.fixture
    def home(self, tmp_path, clean_env, fake_binary, monkeypatch):
        """XDG_CONFIG_HOME → tmp_path: юнит ляжет в tmp_path/systemd/user."""
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        return tmp_path

    @staticmethod
    def _unit(home):
        return home / "systemd" / "user" / "backend-adapter.service"

    def test_unknown_action_is_fatal(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "bogus"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_incompatible_with_install(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "install", "--install"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_incompatible_with_check(self, home, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "install", "--check"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_incompatible_with_launchd(self, home, capsys):
        """Две системы инициализации в одном вызове — ошибка."""
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "install", "--launchd", "install"], "9.9.9")
        assert ei.value.code == 2
        assert "[FATAL]" in capsys.readouterr().out

    def test_bare_flag_defaults_to_install(self, home, capsys):
        """--systemd без подкоманды (так зовёт обёртка) = install."""
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert (root / "adapter.env").is_file()
        assert self._unit(home).is_file()

    def test_install_writes_unit_with_root(self, home, capsys):
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        text = self._unit(home).read_text(encoding="utf-8")
        assert "--root" in text and str(root) in text
        # Токенов в юните нет — только путь к дому (--root подход).
        assert "ADAPTER_BACKEND_KEY" not in text
        # Инвариант: detach выключен при надзоре systemd.
        assert "ADAPTER_DETACH_ENABLE=0" in text
        # Юнит сам задаёт путь к adapter.yaml — не зависит от adapter.env.
        assert f'Environment="ADAPTER_BACKEND_CONFIG={root / "adapter.yaml"}"' in text

    def test_install_does_not_start_or_enable(self, home, capsys, monkeypatch):
        """install только ставит юнит: start/enable не вызываются."""
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            import subprocess as sp

            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: "/usr/bin/" + name)
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        # Разрешён только daemon-reload; start/enable — нет.
        flat = [" ".join(c) for c in calls]
        assert any("daemon-reload" in c for c in flat)
        assert not any(" start" in c for c in flat)
        assert not any("enable" in c for c in flat)

    def test_unit_install_requires_marked_home(self, home, capsys):
        root = home / ".ba"
        root.mkdir()
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(
                ["--systemd", "unit-install", "--root", str(root)], "9.9.9"
            )
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "--install" in out
        assert not self._unit(home).exists()

    def test_unit_install_does_not_touch_home(self, home, capsys):
        """Главная гарантия: готовый дом не переразмечается."""
        root = home / ".ba"
        root.mkdir()
        env_path = root / "adapter.env"
        env_path.write_text("export ADAPTER_PROXY_PORT=4242\n", encoding="utf-8")

        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(
                ["--systemd", "unit-install", "--root", str(root)], "9.9.9"
            )
        assert ei.value.code == 0
        assert env_path.read_text(encoding="utf-8") == "export ADAPTER_PROXY_PORT=4242\n"
        assert not (root / "adapter.env.bak").exists()
        assert not (root / "adapter.yaml").exists()
        assert self._unit(home).is_file()

    def test_existing_unit_backed_up(self, home, capsys):
        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        unit = self._unit(home)
        unit.write_text("old unit\n", encoding="utf-8")
        capsys.readouterr()

        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert (home / "systemd" / "user" / "backend-adapter.service.bak").read_text(
            encoding="utf-8"
        ) == "old unit\n"
        assert "[WARN]" in capsys.readouterr().out

    def test_logs_journalctl_default_depth(self, home, capsys, monkeypatch):
        """logs читает journald: journalctl --user -u <unit> -n20."""
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            import subprocess as sp

            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: "/usr/bin/" + name)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "logs"], "9.9.9")
        assert ei.value.code == 0
        assert calls == [
            [
                "journalctl",
                "--user",
                "-u",
                "backend-adapter.service",
                "--no-pager",
                "-n20",
            ]
        ]

    def test_logs_journalctl_custom_depth_and_follow(self, home, capsys, monkeypatch):
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            import subprocess as sp

            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: "/usr/bin/" + name)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "logs", "--lines", "200", "--follow"], "9.9.9")
        assert ei.value.code == 0
        assert calls[0][-2:] == ["-n200", "-f"]

    def test_logs_requires_journalctl(self, home, capsys, monkeypatch):
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "logs"], "9.9.9")
        assert ei.value.code == 1
        assert "journalctl" in capsys.readouterr().out

    def test_uninstall_disables_and_removes_unit(self, home, capsys, monkeypatch):
        """uninstall: disable --now, удаление юнита и .bak, дом не тронут."""
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        unit = self._unit(home)
        unit.write_text("old unit\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        backup = unit.with_name(unit.name + ".bak")
        assert unit.is_file() and backup.is_file()
        env_before = (root / "adapter.env").read_text(encoding="utf-8")
        capsys.readouterr()

        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: "/usr/bin/" + name)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not unit.exists() and not backup.exists()
        flat = [" ".join(c) for c in calls]
        assert any("disable --now backend-adapter.service" in c for c in flat)
        assert any("daemon-reload" in c for c in flat)
        assert (root / "adapter.env").read_text(encoding="utf-8") == env_before
        assert "Дом не изменён" in capsys.readouterr().out

    def test_uninstall_without_systemctl_still_removes_files(self, home, capsys, monkeypatch):
        """Без systemctl файлы юнита всё равно убираются, остановка — WARN."""
        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        unit = self._unit(home)
        assert unit.is_file()
        capsys.readouterr()

        monkeypatch.setattr(cli_args.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not unit.exists()
        out = capsys.readouterr().out
        assert "[WARN]" in out and "systemctl" in out
