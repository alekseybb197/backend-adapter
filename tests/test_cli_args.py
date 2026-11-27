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
import pathlib

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
        (work / "adapter.env").write_text("export ADAPTER_PROXY_PORT=1111\n", encoding="utf-8")
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
        (work / "adapter.env").write_text("export ADAPTER_PROXY_PORT=1111\n", encoding="utf-8")
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
            cli_args.parse_args(["--launchd", "plist-install", "--root", str(root)], "9.9.9")
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
            cli_args.parse_args(["--launchd", "plist-install", "--root", str(root)], "9.9.9")
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
        kickstarted = False

        def fake_run(cmd, *args, **kwargs):
            nonlocal kickstarted
            calls.append(cmd)
            if cmd[:2] == ["launchctl", "print"]:
                if kickstarted:
                    # load опрашивает print до state=running — агент жив.
                    return sp.CompletedProcess(cmd, 0, "state = running\npid = 4242\n", "")
                return sp.CompletedProcess(cmd, 113, "", "Could not find service")
            if cmd[:2] == ["launchctl", "kickstart"]:
                kickstarted = True
                return sp.CompletedProcess(cmd, 0, "4242\n", "")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "load", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        verbs = [c[1] for c in calls]
        # print (регистрация) → bootstrap → kickstart → print (проверка выживания)
        assert verbs[:3] == ["print", "bootstrap", "kickstart"]
        assert calls[1][2].startswith("gui/")
        assert calls[1][3] == str(self._plist(home))
        assert calls[2] == [
            "launchctl",
            "kickstart",
            "-p",
            calls[1][2] + "/com.user.backend-adapter",
        ]
        assert "4242" in capsys.readouterr().out

    def test_load_warns_when_agent_dies(self, home, capsys, monkeypatch):
        """kickstart отдал PID, но агент упал (EADDRINUSE) — [WARN], rc 1.

        ``kickstart -p`` возвращает PID асинхронно: процесс может умереть
        сразу после. Без проверки ``state = running`` команда рапортовала бы
        «запущен» для мёртвого агента.
        """
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        (home / "Library" / "Logs" / "backend-adapter" / "adapter.log").write_text(
            "OSError: [Errno 48] Address already in use\n", encoding="utf-8"
        )
        capsys.readouterr()
        monkeypatch.setattr(cli_args, "_LOAD_SETTLE_TIMEOUT", 0.05)

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["launchctl", "print"]:
                return sp.CompletedProcess(cmd, 0, "state = exited\nlast exit code = 1\n", "")
            return sp.CompletedProcess(cmd, 0, "4242\n", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "load", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[WARN]" in out and "не поднялся" in out
        assert "Address already in use" in out

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
            if cmd[:2] == ["launchctl", "print"]:
                return sp.CompletedProcess(cmd, 0, "state = running\npid = 7\n", "")
            return sp.CompletedProcess(cmd, 0, "7\n", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "load", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        # print (уже зарегистрирован) → kickstart → print (проверка выживания)
        assert [c[1] for c in calls] == ["print", "kickstart", "print"]

    def test_load_requires_plist(self, home, capsys):
        root = home / ".ba"
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "load", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        assert "[FATAL]" in capsys.readouterr().out

    def test_plist_uninstall_removes_plist_and_backup(self, home, capsys, monkeypatch):
        """plist-uninstall: bootout + удаление plist и .bak, дом не тронут."""
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
            cli_args.parse_args(["--launchd", "plist-uninstall", "--root", str(root)], "9.9.9")
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

    def test_plist_uninstall_when_not_installed_is_noop(self, home, capsys, monkeypatch):
        """Повторный plist-uninstall: агент не зарегистрирован, plist нет — no-op."""
        import subprocess as sp

        root = home / ".ba"
        root.mkdir()

        def fake_run(cmd, *args, **kwargs):
            return sp.CompletedProcess(cmd, 113, "", "Could not find service")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "plist-uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "не был зарегистрирован" in out
        assert "удалять нечего" in out

    def test_uninstall_purges_service_home_and_log(self, home, capsys, monkeypatch):
        """uninstall (v1.0.3): plist + дом целиком + лог сервиса, с --yes."""
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        # Положим в дом данные — они должны исчезнуть вместе с каталогом.
        (root / "var").mkdir(exist_ok=True)
        (root / "var" / "state.yaml").write_text("k: v\n", encoding="utf-8")
        log_dir = home / "Library" / "Logs" / "backend-adapter"
        assert (log_dir / "adapter.log").parent.is_dir()
        capsys.readouterr()

        def fake_run(cmd, *args, **kwargs):
            return sp.CompletedProcess(cmd, 113, "", "Could not find service")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--yes", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not root.exists()
        assert not log_dir.exists()
        out = capsys.readouterr().out
        assert "Полное удаление завершено" in out
        assert "--yes" in out

    def test_uninstall_refuses_unmarked_root(self, home, capsys):
        """Защита: каталог без adapter.env/adapter.yaml — не дом, отказ."""
        root = home / "not-a-home"
        root.mkdir()
        (root / "important.txt").write_text("keep me\n", encoding="utf-8")
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--yes", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "не дом адаптера" in out
        assert (root / "important.txt").is_file()

    def test_uninstall_without_tty_requires_yes(self, home, capsys, monkeypatch):
        """Без --yes и без терминала — [FATAL], ничего не удаляется."""
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        capsys.readouterr()
        monkeypatch.setattr(
            cli_args.subprocess, "run", lambda cmd, *a, **k: sp.CompletedProcess(cmd, 113, "", "")
        )
        monkeypatch.setattr(cli_args.sys.stdin, "isatty", lambda: False)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        assert root.is_dir()
        assert "--yes" in capsys.readouterr().out

    def test_uninstall_prompt_yes_proceeds(self, home, capsys, monkeypatch):
        """Интерактивное подтверждение 'y' — удаление выполняется."""
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        capsys.readouterr()
        monkeypatch.setattr(
            cli_args.subprocess, "run", lambda cmd, *a, **k: sp.CompletedProcess(cmd, 113, "", "")
        )
        monkeypatch.setattr(cli_args.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not root.exists()

    def test_uninstall_prompt_no_cancels(self, home, capsys, monkeypatch):
        """Интерактивный отказ 'n' — ничего не удалено."""
        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        capsys.readouterr()
        monkeypatch.setattr(cli_args.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert root.is_dir()
        assert "Отменено" in capsys.readouterr().out

    def test_yes_without_uninstall_is_fatal(self, home, capsys):
        """--yes осмыслен только с uninstall."""
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "status", "--yes"], "9.9.9")
        assert ei.value.code == 2
        assert "--yes" in capsys.readouterr().out


class TestFindBinary:
    """_find_binary (v1.0.3): приоритет собственного бинарника + проба --root.

    Корень регрессии: в PATH лежал standalone-бинарник v0.8.0 без CLI. Он
    молча игнорировал ``--root`` и падал на пустом ``ADAPTER_BACKEND_CONFIG``.
    Теперь каждый кандидат проверяется пробой ``--help``, а несовместимый
    бинарник — ``[FATAL]``.
    """

    @staticmethod
    def _fake_probe(responses):
        """Подменить ``_probe_once``: ответ по последнему аргументу (флагу)."""

        def probe(binary, interpreter, *args):
            return responses.get(args[-1], (1, ""))

        return probe

    def test_prefers_current_script_with_interpreter(self, tmp_path, monkeypatch, capsys):
        """Запуск ``python backend-adapter.py`` → скрипт + интерпретатор."""
        script = tmp_path / "backend-adapter.py"
        script.write_text("# script\n", encoding="utf-8")
        monkeypatch.setattr(cli_args.sys, "argv", [str(script)])
        monkeypatch.setattr(cli_args.sys, "frozen", False, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", "/usr/bin/python3")
        monkeypatch.setattr(cli_args.os.path, "isfile", lambda p: p == str(script))
        monkeypatch.setattr(
            cli_args,
            "_probe_once",
            self._fake_probe({"--help": (0, "Usage: ... --root <путь>"), "--version": (0, "v9\n")}),
        )
        assert cli_args._find_binary("/home/u/.ba") == (str(script), "/usr/bin/python3")
        assert "Бинарник:" in capsys.readouterr().out

    def test_frozen_uses_executable(self, tmp_path, monkeypatch):
        """PyInstaller-сборка: ``sys.executable`` и есть бинарник."""
        monkeypatch.setattr(cli_args.sys, "frozen", True, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", "/opt/ba/backend-adapter")
        monkeypatch.setattr(cli_args.sys, "argv", ["/opt/ba/backend-adapter"])
        monkeypatch.setattr(cli_args.os.path, "isfile", lambda p: p == "/opt/ba/backend-adapter")
        monkeypatch.setattr(cli_args, "_probe_once", self._fake_probe({"--help": (0, " --root ")}))
        assert cli_args._find_binary("/home/u/.ba") == ("/opt/ba/backend-adapter", None)

    def test_rejects_binary_without_root(self, tmp_path, monkeypatch, capsys):
        """Старый бинарник (нет --root в справке) — [FATAL] exit 1."""
        monkeypatch.setattr(cli_args.sys, "frozen", True, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", "/opt/old/backend-adapter")
        monkeypatch.setattr(cli_args.sys, "argv", ["/opt/old/backend-adapter"])
        monkeypatch.setattr(cli_args.os.path, "isfile", lambda p: True)
        monkeypatch.setattr(
            cli_args,
            "_probe_once",
            self._fake_probe(
                {"--help": (0, "Usage: backend-adapter  [FATAL] ADAPTER_BACKEND_CONFIG")}
            ),
        )
        with pytest.raises(SystemExit) as ei:
            cli_args._find_binary("/home/u/.ba")
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "не понимает --root" in out

    def test_timeout_is_rejection(self, monkeypatch, capsys):
        """Старый бинарник без CLI висит сервером — таймаут = несовместим."""
        monkeypatch.setattr(cli_args.sys, "frozen", True, raising=False)
        monkeypatch.setattr(cli_args.sys, "executable", "/opt/old/backend-adapter")
        monkeypatch.setattr(cli_args.sys, "argv", ["/opt/old/backend-adapter"])
        monkeypatch.setattr(cli_args.os.path, "isfile", lambda p: True)
        # None — проба не ответила за таймаут.
        monkeypatch.setattr(cli_args, "_probe_once", lambda *a: None)
        with pytest.raises(SystemExit) as ei:
            cli_args._find_binary("/home/u/.ba")
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[WARN]" in out and "не ответил" in out

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

        def probe(binary, interpreter, *args):
            if binary == str(old):
                return (1, "")  # старый: без CLI
            return (0, " --root ")

        monkeypatch.setattr(cli_args, "_probe_once", probe)
        assert cli_args._find_binary(str(root)) == (str(script), "/usr/bin/python3")

    def test_probe_runs_isolated_from_env_and_cwd(self, tmp_path, monkeypatch):
        """Проба не видит ``ADAPTER_*`` и стартует не в каталоге репозитория.

        Иначе старый бинарник без CLI на ``--help`` поднимал бы настоящий
        сервер на унаследованном конфиге (регрессия: зависшие процессы,
        занимавшие порт).
        """
        import subprocess as sp

        real_popen = sp.Popen  # до подмены — иначе рекурсия в fake_popen
        monkeypatch.setenv("ADAPTER_BACKEND_CONFIG", "/should/not/be/seen")
        captured: dict[str, object] = {}

        def fake_popen(cmd, **kwargs):
            captured.update(kwargs)
            captured["cmd"] = cmd
            return real_popen([cli_args.sys.executable, "-c", "pass"], **kwargs)

        monkeypatch.setattr(cli_args.subprocess, "Popen", fake_popen)
        result = cli_args._probe_once("/opt/x/backend-adapter", None, "--help")
        assert result is not None and result[0] == 0
        env = captured["env"]
        assert isinstance(env, dict)
        assert not any(k.startswith("ADAPTER_") for k in env)
        # cwd и HOME — во временном каталоге, не в репозитории.
        cwd = captured["cwd"]
        assert isinstance(cwd, str) and "backend-adapter-probe-" in cwd
        assert env["HOME"] == cwd
        assert captured["start_new_session"] is True

    def test_probe_timeout_kills_group(self, monkeypatch):
        """Таймаут пробы убивает всю группу (``killpg``), а не только лидера."""
        import subprocess as sp

        killed: list[int] = []

        class FakeProc:
            pid = 4321

            def __init__(self) -> None:
                self.calls = 0

            def communicate(self, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise sp.TimeoutExpired("x", timeout)
                return ("", "")

        proc = FakeProc()
        monkeypatch.setattr(cli_args.subprocess, "Popen", lambda cmd, **kwargs: proc)
        monkeypatch.setattr(cli_args.os, "killpg", lambda pid, sig: killed.append(pid))
        result = cli_args._probe_once("/opt/x/backend-adapter", None, "--help")
        assert result is None
        assert killed == [4321]

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


class TestServiceLogCommands:
    """logs-clear / log-rotate / log-rotate-remove (v1.0.3, ADR-041).

    Лог сервиса живёт вне дома (``~/Library/Logs/backend-adapter``), а конфиг
    ротации — в системном ``/etc/newsyslog.d``. Оба пути подменяются: тест не
    трогает ни настоящий лог, ни ``/etc`` и не зовёт ``sudo``.
    """

    @pytest.fixture
    def home(self, tmp_path, clean_env, fake_binary, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        return tmp_path

    @pytest.fixture
    def env(self, home, monkeypatch):
        """Подменённые пути лога/конфига и запись вызовов subprocess."""
        log_dir = home / "Library" / "Logs" / "backend-adapter"
        log_dir.mkdir(parents=True)
        log = log_dir / "adapter.log"
        log.write_text("old\n", encoding="utf-8")
        etc = home / "etc"
        etc.mkdir()
        monkeypatch.setattr(cli_args, "_NEWSYSLOG_DIR", str(etc))
        calls: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            import subprocess as sp

            calls.append(list(cmd))
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        return {"log": log, "conf": etc / "backend-adapter.conf", "calls": calls}

    def _marked_home(self, home, *, data_root=None):
        """Размеченный дом с plist; PID-файл по умолчанию указывает на us."""
        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        var = (data_root or root) / "var"
        var.mkdir(parents=True, exist_ok=True)
        (var / "adapter.pid").write_text(str(os.getpid()), encoding="utf-8")
        return root

    # ── logs-clear ──────────────────────────────────────────────────────
    def test_logs_clear_removes_file_when_agent_stopped(self, home, env, capsys, monkeypatch):
        monkeypatch.setattr(cli_args, "_launchd_fields", lambda: {"state": "not running"})
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "logs-clear"], "9.9.9")
        assert ei.value.code == 0
        assert not env["log"].exists()
        out = capsys.readouterr().out
        assert "[OK]" in out and "перезапуск не нужен" in out
        # Остановленный агент не перезапускается.
        assert not any("kickstart" in c for c in env["calls"])

    def test_logs_clear_restarts_running_agent(self, home, env, capsys, monkeypatch):
        monkeypatch.setattr(cli_args, "_launchd_fields", lambda: {"state": "running", "pid": "1"})
        monkeypatch.setattr(cli_args, "_wait_launchd_running", lambda t, s: "4242")
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "logs-clear"], "9.9.9")
        assert ei.value.code == 0
        assert not env["log"].exists()
        kick = [c for c in env["calls"] if "kickstart" in c][0]
        assert "-k" in kick
        assert "PID 4242" in capsys.readouterr().out

    def test_logs_clear_unregistered_agent(self, home, env, capsys, monkeypatch):
        monkeypatch.setattr(cli_args, "_launchd_fields", lambda: None)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "logs-clear"], "9.9.9")
        assert ei.value.code == 0
        assert not env["log"].exists()

    def test_logs_clear_missing_file_warns(self, home, env, capsys, monkeypatch):
        env["log"].unlink()
        monkeypatch.setattr(cli_args, "_launchd_fields", lambda: {"state": "not running"})
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "logs-clear"], "9.9.9")
        assert ei.value.code == 0
        assert "[WARN]" in capsys.readouterr().out

    def test_logs_clear_never_truncates(self, home, env, monkeypatch):
        """Файл удаляется, а не обнуляется: truncate под launchd даёт NUL.

        Признак удаления — открытый до вызова дескриптор после команды
        указывает на unlinked inode (``st_nlink == 0``). Сравнение номеров
        inode ненадёжно: tmpfs переиспользует их сразу (падало в CI).
        """
        fd = os.open(env["log"], os.O_RDONLY)
        try:
            monkeypatch.setattr(cli_args, "_launchd_fields", lambda: None)
            with pytest.raises(SystemExit):
                cli_args.parse_args(["--launchd", "logs-clear"], "9.9.9")
            assert not env["log"].exists()
            assert os.fstat(fd).st_nlink == 0
        finally:
            os.close(fd)

    # ── log-rotate ──────────────────────────────────────────────────────
    def test_log_rotate_installs_conf(self, home, env, capsys, monkeypatch):
        root = self._marked_home(home)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: "/usr/sbin/newsyslog")
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        # Сначала dry-run без root, затем sudo install.
        dry = [c for c in env["calls"] if "newsyslog" in c][0]
        assert "-r" in dry and "-n" in dry
        install = [c for c in env["calls"] if c[:3] == ["sudo", "install", "-m"]][0]
        assert install[-1] == str(env["conf"])
        out = capsys.readouterr().out
        assert "[OK]" in out and "Ротация лога настроена" in out

    def test_log_rotate_conf_points_to_pidfile(self, home, env, monkeypatch):
        root = self._marked_home(home)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: "/usr/sbin/newsyslog")
        captured = {}

        def fake_run(cmd, *args, **kwargs):
            import subprocess as sp

            if cmd[:3] == ["sudo", "install", "-m"]:
                captured["content"] = pathlib.Path(cmd[-2]).read_text(encoding="utf-8")
            return sp.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert str(root / "var" / "adapter.pid") in captured["content"]
        assert str(env["log"]) in captured["content"]

    def test_log_rotate_refuses_without_pidfile(self, home, env, capsys):
        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--launchd", "install", "--root", str(root)], "9.9.9")
        (root / "var").mkdir(exist_ok=True)
        capsys.readouterr()
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "[FATAL]" in out and "PID" in out
        assert not env["conf"].exists()

    def test_log_rotate_refuses_unmarked_home(self, home, env, capsys):
        root = home / "plain"
        root.mkdir()
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        assert "не размечен" in capsys.readouterr().out

    def test_log_rotate_refuses_without_log_file(self, home, env, capsys, monkeypatch):
        root = self._marked_home(home)
        env["log"].unlink()
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        assert "Лог сервиса не найден" in capsys.readouterr().out

    def test_log_rotate_aborts_on_failed_dry_run(self, home, env, capsys, monkeypatch):
        root = self._marked_home(home)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: "/usr/sbin/newsyslog")

        def fake_run(cmd, *args, **kwargs):
            import subprocess as sp

            rc = 1 if cmd[0] == "newsyslog" else 0
            return sp.CompletedProcess(cmd, rc, "", "bad config")

        monkeypatch.setattr(cli_args.subprocess, "run", fake_run)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate", "--root", str(root)], "9.9.9")
        assert ei.value.code == 1
        out = capsys.readouterr().out
        assert "отверг конфиг" in out
        # В /etc ничего не попало: sudo install не вызывался.
        assert not env["conf"].exists()

    def test_log_rotate_without_newsyslog_warns_but_installs(self, home, env, capsys, monkeypatch):
        root = self._marked_home(home)
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        out = capsys.readouterr().out
        assert "newsyslog не найден" in out
        assert any(c[:3] == ["sudo", "install", "-m"] for c in env["calls"])

    # ── log-rotate-remove ───────────────────────────────────────────────
    def test_log_rotate_remove_calls_sudo_rm(self, home, env, capsys):
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate-remove"], "9.9.9")
        assert ei.value.code == 0
        assert ["sudo", "rm", "-f", str(env["conf"])] in env["calls"]
        assert "снят" in capsys.readouterr().out

    def test_log_rotate_remove_failure_is_fatal(self, home, env, capsys, monkeypatch):
        def fail(cmd, *a, **k):
            import subprocess as sp

            return sp.CompletedProcess(cmd, 1, "", "denied")

        monkeypatch.setattr(cli_args.subprocess, "run", fail)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "log-rotate-remove"], "9.9.9")
        assert ei.value.code == 1
        assert "[FATAL]" in capsys.readouterr().out

    # ── uninstall ───────────────────────────────────────────────────────
    def test_uninstall_removes_rotate_conf(self, home, env, capsys, monkeypatch):
        root = self._marked_home(home)
        env["conf"].write_text("# rotation\n", encoding="utf-8")
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--yes", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert ["sudo", "rm", "-f", str(env["conf"])] in env["calls"]
        assert not root.exists()

    def test_uninstall_without_rotate_conf_needs_no_sudo(self, home, env, capsys, monkeypatch):
        root = self._marked_home(home)
        monkeypatch.setattr(cli_args.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--launchd", "uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not any(c[0] == "sudo" for c in env["calls"])


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
            cli_args.parse_args(["--systemd", "unit-install", "--root", str(root)], "9.9.9")
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
            cli_args.parse_args(["--systemd", "unit-install", "--root", str(root)], "9.9.9")
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

    def test_unit_uninstall_disables_and_removes_unit(self, home, capsys, monkeypatch):
        """unit-uninstall: disable --now, удаление юнита и .bak, дом не тронут."""
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
            cli_args.parse_args(["--systemd", "unit-uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not unit.exists() and not backup.exists()
        flat = [" ".join(c) for c in calls]
        assert any("disable --now backend-adapter.service" in c for c in flat)
        assert any("daemon-reload" in c for c in flat)
        assert (root / "adapter.env").read_text(encoding="utf-8") == env_before
        assert "Дом не изменён" in capsys.readouterr().out

    def test_unit_uninstall_without_systemctl_still_removes_files(self, home, capsys, monkeypatch):
        """Без systemctl файлы юнита всё равно убираются, остановка — WARN."""
        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        unit = self._unit(home)
        assert unit.is_file()
        capsys.readouterr()

        monkeypatch.setattr(cli_args.shutil, "which", lambda name: None)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "unit-uninstall", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not unit.exists()
        out = capsys.readouterr().out
        assert "[WARN]" in out and "systemctl" in out

    def test_uninstall_purges_unit_and_home(self, home, capsys, monkeypatch):
        """uninstall (v1.0.3): юнит + дом целиком, с --yes."""
        import subprocess as sp

        root = home / ".ba"
        with pytest.raises(SystemExit):
            cli_args.parse_args(["--systemd", "install", "--root", str(root)], "9.9.9")
        assert root.is_dir()
        capsys.readouterr()

        monkeypatch.setattr(
            cli_args.subprocess, "run", lambda cmd, *a, **k: sp.CompletedProcess(cmd, 0, "", "")
        )
        monkeypatch.setattr(cli_args.shutil, "which", lambda name: "/usr/bin/" + name)
        with pytest.raises(SystemExit) as ei:
            cli_args.parse_args(["--systemd", "uninstall", "--yes", "--root", str(root)], "9.9.9")
        assert ei.value.code == 0
        assert not root.exists()
        assert not self._unit(home).exists()
        assert "Полное удаление завершено" in capsys.readouterr().out
