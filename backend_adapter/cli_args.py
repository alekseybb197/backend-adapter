"""CLI-ключи адаптера: ``--version``, ``--help``, ``--root``, ``--install``,
``--name``/``--base``/``--key``, ``--check``, ``--launchd``, ``--systemd``.

До v0.9.11 у адаптера не было CLI вообще: ``backend-adapter.py`` не читал
``sys.argv``, а версия печаталась безусловно стартовым баннером. Из-за этого
внешние потребители (``install.sh``, ``scripts/build-binaries.sh``, CI)
определяли версию и работоспособность бинарника, ЗАПУСКАЯ его целиком и
разбирая ``[FATAL]`` пустого ``ADAPTER_BACKEND_CONFIG``. Теперь версия и
справка отдаются напрямую.

**Момент разбора критичен.** ``backend-adapter.py`` имеет два import-time
побочных эффекта, зависящих от env: ``env_validate.validate_env()`` (невалидный
env → ``[FATAL]`` + ``sys.exit(1)``) и импорт ``config`` (парсит ``int(...)``
из env на уровне модуля). ``parse_args`` вызывается ДО них, поэтому
``--version``/``--help`` обязаны работать при любом, даже заведомо битом
окружении (``ADAPTER_PROXY_PORT=abc``).

**Домашняя папка (``--root``).** По умолчанию ``~/.ba``:

    <root>/
      adapter.env     — переменные окружения (рендерит --install из templates)
      adapter.yaml    — конфигурация бэкендов (встроенный sample.adapter.yaml)
      tariffs.yaml    — тарифы моделей (встроенный sample.tariffs.yaml)
      log/            — файловые логи сессий (*.parts, .err, ...)
      var/            — состояние (PID, model-usage.yaml, state.yaml)

С v0.9.13 дом и корень данных совпадают: ``ADAPTER_DATA_ROOT`` == ``<root>``
(подпапка ``tmp/adapter`` убрана). Запуск с ``--root`` подставляет из дома
ровно то, чего нет в окружении (``os.environ.setdefault`` — **явный env всегда
побеждает**): путь данных, переменные из ``adapter.env``,
``adapter.yaml``/``tariffs.yaml``.

**Автопоиск дома (v0.9.13).** Без ``--root`` дом находится сам, в порядке:
текущая папка, затем ``~/.ba`` (признак дома — ``adapter.env`` или
``adapter.yaml``). Найденный дом подключается целиком; не найдено нигде — дом
не читается, но дефолт ``ADAPTER_DATA_ROOT`` всё равно ``~/.ba``: «нулевой
запуск» требует лишь ``ADAPTER_BACKEND_CONFIG`` в окружении. ``--install`` и
``--check`` без ``--root`` автопоиск не используют — у них цель всегда ``~/.ba``.

Модуль — лист DAG: stdlib + ``templates`` (тоже stdlib-only), откуда берутся
встроенные шаблоны ``--install``. Версию принимает аргументом: импортировать
``backend-adapter.py`` нельзя (имя с дефисом — не валидный идентификатор), а
держать её здесь значило бы раздвоить единственный источник версии
(см. докстринг ``cli.py``).

Разбор — ручной, а не ``argparse``: нужен ключ ``-?`` (argparse трактует
``-h``/``--help`` специально), а для горстки плоских ключей таблица проще
настройки ``add_help=False``.
"""

import collections
import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import env_check, service_log, templates

# Ключи версии и справки (синонимы: короткий/длинный; -? — исторический
# «вопросительный» вариант, привычный по Windows-утилитам).
_VERSION_FLAGS = ("-v", "--version")
_HELP_FLAGS = ("-h", "--help", "-?")
_LAUNCHD_FLAG = "--launchd"
# install — разметить дом + plist; plist-install — только plist для уже
# готового дома (дом не трогает); остальные — управление установленным plist.
_LAUNCHD_ACTIONS = frozenset(
    {
        "install",
        "plist-install",
        "load",
        "unload",
        "status",
        "plist",
        "logs",
        "logs-clear",
        "log-rotate",
        "log-rotate-remove",
        "plist-uninstall",
        "uninstall",
    }
)
# Действие по умолчанию, если подкоманда опущена (--launchd --root ...):
# так вызывают install.sh и scripts/install-launchd.sh.
_LAUNCHD_DEFAULT_ACTION = "install"

# Linux-аналог launchd: user-level systemd-юнит (~/.config/systemd/user).
# install — разметить дом + юнит (запуск отдельно, через start);
# unit-install — только юнит для уже готового дома; остальные — управление.
_SYSTEMD_FLAG = "--systemd"
_SYSTEMD_ACTIONS = frozenset(
    {
        "install",
        "unit-install",
        "start",
        "stop",
        "status",
        "unit",
        "logs",
        "unit-uninstall",
        "uninstall",
    }
)
_SYSTEMD_DEFAULT_ACTION = "install"

# Просмотр логов сервиса (v1.0.2): ``--launchd logs`` / ``--systemd logs``.
# ``-n`` — глубина разового вывода (по умолчанию последние 20 строк),
# ``-f`` — не завершаться, а следить за логом до Ctrl-C.
_LOGS_DEFAULT_LINES = 20

# Ротация лога сервиса (v1.0.3, ADR-041): конфиг newsyslog кладётся в
# /etc/newsyslog.d — это системный каталог, запись требует sudo.
_NEWSYSLOG_DIR = "/etc/newsyslog.d"
_NEWSYSLOG_CONF_NAME = "backend-adapter.conf"
_LOGS_LINES_FLAGS = ("-n", "--lines")
_LOGS_FOLLOW_FLAGS = ("-f", "--follow")

# Подтверждение полного удаления (``--launchd uninstall``/``--systemd
# uninstall``) без интерактивного вопроса — для скриптов.
_YES_FLAGS = ("-y", "--yes")

# Домашняя папка адаптера по умолчанию: используется для --install без
# --root (--root без аргумента — ошибка, см. parse_args).
_DEFAULT_ROOT = "~/.ba"

# Имена файлов дома — в templates (там же, где рендерится adapter.env); берём
# их оттуда, чтобы не держать два списка имён.
_ENV_NAME = templates.ENV_NAME
_YAML_NAME = templates.YAML_NAME
_TARIFFS_NAME = templates.TARIFFS_NAME

# Метка launchd-агента — в templates (там же, где рендерится plist); берём
# оттуда, чтобы Label в коде и в шаблоне не разъехались.
_LAUNCHD_LABEL = templates.LAUNCHD_LABEL
# Имя user-level systemd-юнита — тоже в templates (там же, где рендерится).
_SYSTEMD_UNIT_NAME = templates.SYSTEMD_UNIT_NAME


def _plist_path() -> str:
    """Путь к plist-файлу launchd-агента."""
    return os.path.expanduser(os.path.join("~/Library/LaunchAgents", f"{_LAUNCHD_LABEL}.plist"))


def _unit_path() -> str:
    """Путь к user-level systemd-юниту: ``$XDG_CONFIG_HOME`` или ``~/.config``."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "systemd", "user", _SYSTEMD_UNIT_NAME)


_HELP_TEXT = """\
backend-adapter — [AN] Messages <-> [OI]-compatible backends for AI agents

Usage:
  backend-adapter [OPTIONS]

Options:
  -v, --version      Показать версию и завершить работу.
  -h, --help, -?     Показать эту справку и завершить работу.
  --root <путь>      Домашняя папка адаптера. Одновременно корень данных
                     (ADAPTER_DATA_ROOT). Из неё подставляются недостающие
                     настройки: переменные <путь>/adapter.env, конфиги
                     <путь>/adapter.yaml и <путь>/tariffs.yaml, данные
                     <путь>/log и <путь>/var.
                     Без --root дом ищется сам: сначала текущая папка, затем
                     ~/.ba (признак — adapter.env или adapter.yaml).
                     Явно заданные переменные окружения всегда побеждают.
  --install          Разметить домашнюю папку для работы адаптера: создать
                     каталоги данных и положить adapter.env (все переменные
                     со значениями по умолчанию), adapter.yaml и tariffs.yaml.
                     Существующий файл не теряется: прежняя копия ложится
                     рядом как <файл>.bak, затем файл перезаписывается.
                     Завершает работу, адаптер не запускает.
  --name <имя>       Имя бэкенда для генерируемого adapter.yaml.
                     Только вместе с --install.
  --base <url>       Базовый URL [OI]-совместимого эндпойнта бэкенда.
                     Только вместе с --install.
  --key <имя>        Имя переменной окружения с токеном бэкенда (поле key
                     в adapter.yaml). Только вместе с --install.
                     --name/--base/--key задаются все три вместе: они
                     заменяют образцовый adapter.yaml на один бэкенд.
  --check            Проверить конфигурацию дома (--root, по умолчанию ~/.ba)
                     и живое окружение: типы значений (int/bool), устаревшие
                     переменные, структуру adapter.yaml/tariffs.yaml.
                     Завершает работу: код 1 при ошибках, иначе 0.
  --launchd <подкоманда>
                     Управление launchd-агентом для macOS. Подкоманды:
                     install — разметить дом и сгенерировать plist (по
                     умолчанию, если подкоманда опущена);
                     plist-install — только сгенерировать и установить plist
                     для УЖЕ размеченного дома (дом не трогает, не
                     переразмечает adapter.env/adapter.yaml);
                     load — зарегистрировать агент и запустить его
                     (launchctl bootstrap + kickstart);
                     unload — остановить агент и снять регистрацию
                     (launchctl bootout);
                     status — проверить статус агента;
                     plist — вывести содержимое plist на stdout;
                     logs — показать последние строки лога агента
                     (~/Library/Logs/backend-adapter/adapter.log);
                     см. -n/-f ниже;
                     logs-clear — очистить лог агента (файл удаляется;
                     работающий агент ПЕРЕЗАПУСКАЕТСЯ, иначе место не
                     освободится — см. ADR-041);
                     log-rotate — поставить ротацию лога через newsyslog
                     (/etc/newsyslog.d, требует sudo); по умолчанию 5
                     архивов по 1 МБ, сжатие gzip, по факту размера;
                     log-rotate-remove — снять конфиг ротации (требует sudo);
                     plist-uninstall — остановить агент, снять регистрацию и
                     удалить plist (дом НЕ трогает);
                     uninstall — ПОЛНОЕ удаление: plist + дом целиком
                     (adapter.env с токенами, конфиги) + лог сервиса +
                     конфиг ротации, если он был установлен.
                     Спрашивает подтверждение; для скриптов добавьте --yes.
                     Все подкоманды принимают --root (по умолчанию ~/.ba).
  --systemd <подкоманда>
                     Управление user-level systemd-юнитом для Linux (аналог
                     --launchd; без root). Подкоманды:
                     install — разметить дом и установить юнит в
                     ~/.config/systemd/user (по умолчанию, если подкоманда
                     опущена); автозапуск НЕ включается и не запускается;
                     unit-install — только установить юнит для УЖЕ
                     размеченного дома (дом не трогает);
                     start — запустить сервис (systemctl --user start);
                     stop — остановить сервис (systemctl --user stop);
                     status — проверить статус сервиса;
                     unit — вывести содержимое юнита на stdout;
                     logs — показать логи сервиса из journald
                     (journalctl --user -u backend-adapter.service); см. -n/-f;
                     unit-uninstall — остановить и отключить сервис, удалить
                     юнит (дом НЕ трогает);
                     uninstall — ПОЛНОЕ удаление: юнит + дом целиком
                     (adapter.env с токенами, конфиги). Спрашивает
                     подтверждение; для скриптов добавьте --yes.
                     Все подкоманды принимают --root (по умолчанию ~/.ba).
  -n, --lines <N>    Сколько последних строк лога вывести (только с logs;
                     по умолчанию 20).
  -f, --follow       Не завершаться после вывода, а следить за логом сервиса
                     в консоли до Ctrl-C (только с logs).
  -y, --yes          Подтвердить полное удаление без вопроса (только с
                     --launchd uninstall / --systemd uninstall).

Без ключей адаптер стартует как обычно (нужен ADAPTER_BACKEND_CONFIG).
"""


def parse_args(argv: list[str], version: str) -> None:
    """Разобрать argv адаптера; информационные ключи завершают процесс.

    ``argv`` — ``sys.argv[1:]``; ``version`` — ``__version__`` скрипта.
    Возврата нет у «пустого» вызова (ноль ключей — обычный запуск) и у
    ``--root`` (подготовленный дом: дальше адаптер стартует как обычно).
    ``--version``/``--help``/``--install`` печатают ответ и вызывают
    ``sys.exit(0)``.

    Неизвестный ключ или пропущенный аргумент ``--root`` — ``[FATAL]`` и
    ``sys.exit(2)``: молчаливое проглатывание опасно (адаптер поднимается
    сервисом, и незамеченная опечатка означала бы запуск не с теми
    параметрами, ср. ``install.sh``, где незнакомый ключ лишь предупреждает).

    ``--name``/``--base``/``--key`` (v0.9.13) задают единственный бэкенд
    генерируемого ``adapter.yaml`` и допустимы только вместе с ``--install``
    и все три сразу: частичный набор или набор без ``--install`` — ``[FATAL]``
    и ``sys.exit(2)``. ``--check`` (v0.9.13) — офлайн-диагностика конфигов
    дома: завершает работу кодом ``env_check.run_check`` (1 при ошибках,
    иначе 0); несовместим с ``--install`` и ``--name/--base/--key``.

    ``--launchd [подкоманда]`` (v1.0.2) — управление launchd-агентом.
    Несовместим с ``--check``, ``--install``, ``--name``/``--base``/``--key``.
    Подкоманда опциональна: без неё (``--launchd --root ...``) — ``install``.
    ``install`` размечает дом и ставит plist; ``plist-install`` — только plist
    для уже размеченного дома (дом не трогает).

    ``--systemd [подкоманда]`` (v1.0.2) — Linux-аналог: user-level systemd-юнит
    (``~/.config/systemd/user``). Те же правила совместимости и опциональная
    подкоманда (дефолт ``install``). ``install`` размечает дом и ставит юнит,
    но **не** включает автозапуск и не стартует сервис; ``unit-install`` —
    только юнит для уже размеченного дома; ``start``/``stop``/``status``/``unit``
    — управление.

    Подкоманда ``logs`` (v1.0.2) у обеих систем печатает хвост лога сервиса:
    ``--launchd`` читает ``~/Library/Logs/backend-adapter/adapter.log``,
    ``--systemd`` — ``journalctl --user -u backend-adapter.service``. Глубина —
    ``-n/--lines`` (по умолчанию ``_LOGS_DEFAULT_LINES``), режим наблюдения —
    ``-f/--follow`` (процесс живёт до Ctrl-C). ``-n``/``-f`` без ``logs`` —
    ``[FATAL]`` exit 2.

    Подкоманды лога сервиса (v1.0.3, ADR-041). ``logs-clear`` удаляет файл
    лога (``truncate`` под launchd оставляет NUL-дырку, см.
    ``service_log``), а работающий агент перезапускает: без этого fd живого
    процесса держал бы удалённый inode. ``log-rotate`` ставит конфиг
    newsyslog в ``/etc/newsyslog.d`` (запись через ``sudo``, предварительно
    проверенный dry-run'ом без root), ``log-rotate-remove`` его снимает.
    Обе требуют размеченного дома с живым PID-файлом: без PID newsyslog не
    смог бы уведомить агент о ротации.

    Подкоманды удаления (v1.0.3). ``plist-uninstall``/``unit-uninstall``
    удаляют только установочный артефакт сервиса (plist/юнит и его ``.bak``),
    предварительно остановив и сняв регистрацию; дом не трогается.
    ``uninstall`` — **полное** удаление: тот же сервисный артефакт, затем дом
    ``<root>`` целиком (в нём ``adapter.env`` с токенами) и, на macOS, лог
    сервиса вне дома. Операция разрушительная: спрашивает подтверждение, а
    без терминала требует ``--yes`` (иначе ``[FATAL]``); отказывается, если
    ``<root>`` не похож на дом адаптера или это ``/``/``~``.
    """
    root: str | None = None
    install = False
    check = False
    launchd_action: str | None = None
    systemd_action: str | None = None
    logs_lines = _LOGS_DEFAULT_LINES
    logs_follow = False
    assume_yes = False
    name: str | None = None
    base: str | None = None
    key: str | None = None
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in _VERSION_FLAGS:
            print(f"backend-adapter {version}")
            sys.exit(0)
        if arg in _HELP_FLAGS:
            print(_HELP_TEXT, end="")
            sys.exit(0)
        if arg == "--install":
            install = True
        elif arg == "--check":
            check = True
        elif arg == _LAUNCHD_FLAG:
            # Подкоманда опциональна: ``--launchd`` без неё (или с другим
            # ключом следом — ``--launchd --root ...``) = install. Так
            # вызывают install.sh и scripts/install-launchd.sh.
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                i += 1
                launchd_action = argv[i]
                if launchd_action not in _LAUNCHD_ACTIONS:
                    print(
                        f"[FATAL] --launchd: неизвестная подкоманда {launchd_action!r}. "
                        f"Допустимые: {', '.join(sorted(_LAUNCHD_ACTIONS))}."
                    )
                    sys.exit(2)
            else:
                launchd_action = _LAUNCHD_DEFAULT_ACTION
        elif arg == _SYSTEMD_FLAG:
            # Подкоманда опциональна, как у --launchd: без неё (или с ключом
            # следом — --systemd --root ...) = install.
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                i += 1
                systemd_action = argv[i]
                if systemd_action not in _SYSTEMD_ACTIONS:
                    print(
                        f"[FATAL] --systemd: неизвестная подкоманда {systemd_action!r}. "
                        f"Допустимые: {', '.join(sorted(_SYSTEMD_ACTIONS))}."
                    )
                    sys.exit(2)
            else:
                systemd_action = _SYSTEMD_DEFAULT_ACTION
        elif arg in _LOGS_LINES_FLAGS:
            i += 1
            if i >= len(argv):
                print(f"[FATAL] {arg} требует число строк лога.")
                sys.exit(2)
            raw = argv[i]
            try:
                logs_lines = int(raw)
            except ValueError:
                print(f"[FATAL] {arg}: ожидается целое число, получено {raw!r}.")
                sys.exit(2)
            if logs_lines <= 0:
                print(f"[FATAL] {arg}: глубина лога должна быть > 0, получено {logs_lines}.")
                sys.exit(2)
        elif arg in _LOGS_FOLLOW_FLAGS:
            logs_follow = True
        elif arg in _YES_FLAGS:
            assume_yes = True
        elif arg == "--root":
            i += 1
            if i >= len(argv):
                print("[FATAL] --root требует путь к домашней папке.")
                sys.exit(2)
            root = argv[i]
        elif arg in ("--name", "--base", "--key"):
            i += 1
            if i >= len(argv):
                print(f"[FATAL] {arg} требует значение.")
                sys.exit(2)
            if arg == "--name":
                name = argv[i]
            elif arg == "--base":
                base = argv[i]
            else:
                key = argv[i]
        else:
            print(f"[FATAL] Unknown option: {arg!r}. См. backend-adapter --help.")
            sys.exit(2)
        i += 1

    # --check — офлайн-диагностика конфигов: режим только читает и завершает
    # работу, поэтому несовместим с --install (--root у них общий, а --name/
    # --base/--key задают генерируемый adapter.yaml, которого --check не пишет).
    if check and install:
        print("[FATAL] --check и --install несовместимы.")
        sys.exit(2)

    # --name/--base/--key задают бэкенд генерируемого adapter.yaml: они
    # осмысленны только вместе с --install и только все три сразу (иначе
    # получился бы недонастроенный бэкенд). Отказ — [FATAL] + exit 2.
    backend_keys = {"--name": name, "--base": base, "--key": key}
    given = [flag for flag, value in backend_keys.items() if value is not None]
    if given:
        if check:
            print(f"[FATAL] {'/'.join(given)} несовместимы с --check.")
            sys.exit(2)
        if not install:
            print(
                f"[FATAL] {'/'.join(given)} задаются только вместе с --install "
                "(они генерируют adapter.yaml)."
            )
            sys.exit(2)
        missing = [flag for flag, value in backend_keys.items() if value is None]
        if missing:
            print(
                f"[FATAL] --name/--base/--key задаются все три вместе; "
                f"не хватает: {', '.join(missing)}."
            )
            sys.exit(2)

    # --launchd несовместим с --check, --install и --name/--base/--key.
    if launchd_action and check:
        print("[FATAL] --launchd и --check несовместимы.")
        sys.exit(2)
    if launchd_action and install:
        print("[FATAL] --launchd и --install несовместимы.")
        sys.exit(2)
    if launchd_action and given:
        print(f"[FATAL] --launchd несовместим с {'/'.join(given)}.")
        sys.exit(2)

    # --systemd — те же правила совместимости, что и у --launchd, плюс
    # взаимоисключение с самим --launchd (две разные системы инициализации).
    if systemd_action and check:
        print("[FATAL] --systemd и --check несовместимы.")
        sys.exit(2)
    if systemd_action and install:
        print("[FATAL] --systemd и --install несовместимы.")
        sys.exit(2)
    if systemd_action and given:
        print(f"[FATAL] --systemd несовместим с {'/'.join(given)}.")
        sys.exit(2)
    if systemd_action and launchd_action:
        print("[FATAL] --systemd и --launchd несовместимы.")
        sys.exit(2)

    # -n/-f осмысленны только с подкомандой logs (v1.0.2): глубина и слежение
    # относятся к просмотру лога, у остальных подкоманд смысла не имеют.
    viewing_logs = launchd_action == "logs" or systemd_action == "logs"
    if not viewing_logs and (logs_lines != _LOGS_DEFAULT_LINES or logs_follow):
        owner = "launchd" if launchd_action else ("systemd" if systemd_action else None)
        if owner is not None:
            print(f"[FATAL] -n/-f допустимы только с --{owner} logs.")
        else:
            print("[FATAL] -n/-f допустимы только с --launchd logs или --systemd logs.")
        sys.exit(2)

    # --yes подтверждает полное удаление — осмыслен только с ``uninstall``.
    if assume_yes and launchd_action != "uninstall" and systemd_action != "uninstall":
        print("[FATAL] --yes допустим только с --launchd uninstall или --systemd uninstall.")
        sys.exit(2)

    if check:
        sys.exit(env_check.run_check(_resolve_root(root)))
    if launchd_action:
        _dispatch_launchd(
            launchd_action,
            _resolve_root(root),
            lines=logs_lines,
            follow=logs_follow,
            assume_yes=assume_yes,
        )
        sys.exit(0)
    if systemd_action:
        _dispatch_systemd(
            systemd_action,
            _resolve_root(root),
            lines=logs_lines,
            follow=logs_follow,
            assume_yes=assume_yes,
        )
        sys.exit(0)
    if install:
        _install_home(_resolve_root(root), name=name, base=base, key_env=key)
        sys.exit(0)
    if root is not None:
        _apply_home(_resolve_root(root))
    else:
        # Без --root дом ищется сам (v0.9.13): текущая папка, затем ~/.ba.
        found = _discover_home()
        if found is not None:
            _apply_home(found)


def _resolve_root(root: str | None) -> str:
    """Абсолютный путь домашней папки; ``None`` → дефолт ``~/.ba``."""
    return os.path.abspath(os.path.expanduser(root or _DEFAULT_ROOT))


def _discover_home() -> str | None:
    """Найти дом без ``--root``: текущая папка, затем ``~/.ba``; иначе None.

    Порядок (v0.9.13): **сначала текущая папка**, затем домашняя ``~/.ba``.
    Признак дома — файл ``adapter.env`` или ``adapter.yaml`` (по ним дом и
    опознаётся: пустая папка домом не считается). Явный ``--root`` важнее
    поиска (обрабатывается вызывающим до этого), а переменные окружения —
    важнее самого дома (``_apply_home`` кладёт всё через ``setdefault``).
    Каталог не найден нигде — ``None``: дом не подключается, работает
    «нулевой запуск» на ``ADAPTER_BACKEND_CONFIG`` из окружения.
    """
    for candidate in (os.getcwd(), os.path.expanduser(_DEFAULT_ROOT)):
        if os.path.isfile(os.path.join(candidate, _ENV_NAME)) or os.path.isfile(
            os.path.join(candidate, _YAML_NAME)
        ):
            return candidate
    return None


def _apply_home(root: str) -> None:
    """Подставить из дома недостающие настройки (env побеждает).

    Порядок важен: **сначала** ``adapter.env`` (файл дома — это тоже
    пользовательская настройка, и он вправе задать ``ADAPTER_DATA_ROOT``
    сам), и лишь затем — подстановка путей по умолчанию от ``root``. Каждое
    присваивание через ``setdefault``: уже заданное в окружении значение
    сильнее и файла, и подстановки.
    """
    _load_env_file(os.path.join(root, _ENV_NAME))
    os.environ.setdefault("ADAPTER_DATA_ROOT", root)
    os.environ.setdefault("ADAPTER_BACKEND_CONFIG", os.path.join(root, _YAML_NAME))
    tariffs = os.path.join(root, _TARIFFS_NAME)
    if os.path.isfile(tariffs):
        os.environ.setdefault("ADAPTER_MODELS_TARIFFS", tariffs)


def _load_env_file(path: str) -> None:
    """Прочитать env-файл и подставить НЕзаданные переменные.

    Формат — построчный ``export K=V`` / ``K=V`` (как ``source`` в sh, но без
    исполнения shell). Разбор — общим парсером ``env_check.parse_env_text``
    (тот же, что у ``--check``). Уже заданное в окружении значение сохраняется
    (``setdefault``) — файл дома лишь заполняет пробелы. Отсутствие файла — не
    ошибка (дом может обходиться без adapter.env).
    """
    try:
        with open(path, encoding="utf-8") as f:
            values = env_check.parse_env_text(f.read())
    except OSError:
        return
    for key, value in values.items():
        os.environ.setdefault(key, value)


def _install_home(
    root: str,
    *,
    name: str | None = None,
    base: str | None = None,
    key_env: str | None = None,
) -> None:
    """Разметить домашнюю папку из встроенных шаблонов; существующее не трогать.

    Создаёт ``<root>`` с подпапками данных (``log/`` + ``var/`` — те же, что
    создаёт старт адаптера; дом и корень данных совпадают с v0.9.13) и кладёт
    ``adapter.yaml``, ``tariffs.yaml`` и ``adapter.env`` из модуля ``templates``
    — содержимое встроено в код (v0.9.12), поэтому ``--install`` работает и из
    standalone-бинарника/wheel, где ``docs/samples/`` недоступен. Каждый уже
    существующий файл — пропуск с ``[WARN]`` (идемпотентность: повторный запуск
    не перезаписывает правки пользователя).

    ``name``/``base``/``key_env`` (все три, v0.9.13 — проверено ``parse_args``)
    заменяют образцовый ``adapter.yaml`` на один бэкенд с заданным именем,
    URL и **именем** переменной токена; то же имя токена подставляется в
    ``adapter.env``. Без них — байт-в-байт ``SAMPLE_ADAPTER_YAML``.
    """
    os.makedirs(os.path.join(root, "log"), exist_ok=True)
    os.makedirs(os.path.join(root, "var"), exist_ok=True)

    if name is not None:
        assert base is not None and key_env is not None  # гарантировано parse_args
        adapter_yaml = templates.render_adapter_yaml(name, base, key_env)
    else:
        adapter_yaml = templates.SAMPLE_ADAPTER_YAML
    _write_template(os.path.join(root, _YAML_NAME), adapter_yaml)
    _write_template(os.path.join(root, _TARIFFS_NAME), templates.SAMPLE_TARIFFS_YAML)
    # В adapter.env попадает токен бэкенда — файл только для владельца (0600).
    _write_template(
        os.path.join(root, _ENV_NAME),
        templates.render_env(root, key_env=key_env or templates.DEFAULT_KEY_ENV),
        mode=0o600,
        hint=" (заполните токен)",
    )

    print(f"[OK]   Домашняя папка адаптера готова: {root}")
    print(f"       Запуск: backend-adapter --root {root}")


def _write_template(path: str, content: str, *, mode: int | None = None, hint: str = "") -> None:
    """Записать файл из встроенного шаблона, сохранив прежнюю копию в ``.bak``.

    Файл есть → сперва копируется в ``<path>.bak`` (``shutil.copy2`` — права
    сохраняются, важно для ``adapter.env`` с токеном: копия тоже ``0600``),
    затем ``<path>`` перезаписывается свежим содержимым. Прежняя версия не
    теряется, а пользователь видит ``[WARN]`` с именем бэкапа. Сбой копирования
    — ``[WARN]`` и **без** перезаписи: оригинал важнее свежего шаблона.
    Файла нет → обычное создание.

    ``newline=""`` отключает трансляцию ``\\n`` → ``os.linesep``: файлы ложатся
    ровно теми байтами, что и ``docs/samples/*.yaml`` (важно для Windows, где
    иначе получился бы CRLF). ``mode`` — права создаваемого файла (``None`` —
    по umask).
    """
    if os.path.exists(path):
        backup = path + ".bak"
        try:
            shutil.copy2(path, backup)
        except OSError as e:
            print(
                f"[WARN] {path} существует, но копия в {backup} не удалась: {e} — оставлен без изменений."
            )
            return
        print(f"[WARN] {path} уже существует — прежняя копия в {backup}, файл перезаписан.")
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)
    print(f"[OK]   {path} — создан{hint}.")


def _dispatch_launchd(
    action: str, root: str, *, lines: int, follow: bool, assume_yes: bool
) -> None:
    """Перенаправить подкоманду ``--launchd`` к нужной функции."""
    if action == "logs":
        _launchd_logs(root, lines=lines, follow=follow)
        return
    if action == "logs-clear":
        _launchd_logs_clear(root)
        return
    if action == "uninstall":
        _launchd_uninstall(root, assume_yes=assume_yes)
        return
    dispatch = {
        "install": _launchd_install,
        "plist-install": _launchd_plist_install,
        "load": _launchd_load,
        "unload": _launchd_unload,
        "status": _launchd_status,
        "plist": _launchd_plist,
        "log-rotate": _launchd_log_rotate,
        "log-rotate-remove": _launchd_log_rotate_remove,
        "plist-uninstall": _launchd_plist_uninstall,
    }
    func = dispatch.get(action)
    if func is None:
        print(f"[FATAL] --launchd: неизвестная подкоманда {action!r}.")
        sys.exit(2)
    func(root)


def _home_is_marked(root: str) -> bool:
    """Дом размечен? Признак — ``adapter.env`` (его кладёт ``--install``)."""
    return os.path.isfile(os.path.join(root, _ENV_NAME))


# Сколько ждать ответа на пробный ``--help``. Новый standalone-бинарник
# (PyInstaller ``--onefile``) распаковывается при старте — пара секунд; старый
# бинарник без CLI вместо ответа поднимает сервер, поэтому таймаут обязателен.
_BINARY_PROBE_TIMEOUT = 10

# Окно проверки после ``kickstart -p``: launchd отдаёт PID раньше, чем видно,
# выжил ли процесс. Опрашиваем ``state = running`` недолго — упавший от
# ``EADDRINUSE`` агент умирает за доли секунды.
_LOAD_SETTLE_TIMEOUT = 5.0
_LOAD_SETTLE_STEP = 0.2


def _binary_argv(binary: str, interpreter: str | None, *args: str) -> list[str]:
    """Командная строка запуска бинарника: ``[interpreter, binary, *args]``."""
    return ([interpreter, binary] if interpreter else [binary]) + list(args)


def _script_interpreter() -> str | None:
    """Интерпретатор для запуска ``backend-adapter.py`` сервисом.

    Из исходников это текущий ``sys.executable``. Из frozen-сборки
    ``sys.executable`` — сам бинарник (не Python), поэтому ищем ``python3``
    в ``PATH``; не нашли — ``None`` (скрипт запустится напрямую: сработает
    только исполняемый файл с shebang).
    """
    if not getattr(sys, "frozen", False):
        return sys.executable
    return shutil.which("python3") or shutil.which("python")


def _probe_env() -> dict[str, str]:
    """Окружение для пробы бинарника: всё, кроме ``ADAPTER_*``.

    Проба не должна видеть настройки живого адаптера. Иначе старый бинарник
    без CLI на ``--help`` стартует **по-настоящему**: унаследованные
    ``ADAPTER_*`` (обычный экспорт ``set -a; source adapter.env``) дают ему
    конфиг, он находит дом, занимает порт и не выходит — проба висит до
    таймаута, а её потомок остаётся сиротой. Без ``ADAPTER_*`` он падает на
    пустом ``ADAPTER_BACKEND_CONFIG`` за секунды, так и не подняв сервер.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("ADAPTER_")}


def _kill_group(proc: subprocess.Popen[str]) -> None:
    """Убить процесс вместе с группой (``start_new_session`` — своя группа).

    ``Popen.kill()`` убивает только прямой дочерний процесс. У onefile-бинарника
    PyInstaller это bootloader, а python-ребёнок переживает его и остаётся
    сиротой с открытым портом. ``killpg`` добивает всю группу.
    """
    with contextlib.suppress(OSError):
        os.killpg(proc.pid, signal.SIGKILL)


def _probe_once(binary: str, interpreter: str | None, *args: str) -> tuple[int, str] | None:
    """Запустить пробу изолированно; ``(rc, stdout)`` или ``None`` при таймауте.

    Окружение — без ``ADAPTER_*`` (``_probe_env``), ``HOME`` и ``cwd`` — во
    временном каталоге: даже старый бинарник не найдёт реальный дом
    (автопоиск идёт от ``cwd`` и ``$HOME``) и не тронет его состояние.
    Своя сессия (``start_new_session``) позволяет при таймауте убить всю
    группу — иначе python-ребёнок onefile-бутлоадера остаётся сиротой.
    """
    check_dir = tempfile.mkdtemp(prefix="backend-adapter-probe-")
    env = _probe_env()
    env["HOME"] = check_dir
    proc = subprocess.Popen(
        _binary_argv(binary, interpreter, *args),
        cwd=check_dir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, _ = proc.communicate(timeout=_BINARY_PROBE_TIMEOUT)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=1)
        return None
    finally:
        shutil.rmtree(check_dir, ignore_errors=True)
    return proc.returncode, stdout or ""


def _probe_binary(binary: str, interpreter: str | None) -> str | None:
    """Проверить, что бинарник понимает ``--root``; вернуть его версию.

    Проба — единственный надёжный способ: standalone-бинарник PyInstaller
    сжат, искать в нём литерал ``--root`` бесполезно. Новый бинарник на
    ``--help`` сразу печатает справку с ``--root`` и выходит с кодом 0;
    старый (без CLI) справки не знает — запускает сервер и падает на
    ``[FATAL]`` (или висит, если окружение настроено), поэтому проба идёт в
    изоляции и с таймаутом (``_probe_once``).

    Возвращает строку версии (``backend-adapter X.Y.Z``) при успехе и
    ``None`` — если бинарник несовместим или не отвечает.
    """
    try:
        help_result = _probe_once(binary, interpreter, "--help")
    except OSError as e:
        print(f"[WARN] Не удалось проверить {binary}: {e}")
        return None
    if help_result is None:
        print(f"[WARN] {binary} не ответил на --help за {_BINARY_PROBE_TIMEOUT}с.")
        return None
    rc, stdout = help_result
    if rc != 0 or "--root" not in stdout:
        return None
    try:
        version_result = _probe_once(binary, interpreter, "--version")
    except OSError:
        return ""
    if version_result is None:
        return ""
    rc, stdout = version_result
    return stdout.strip() if rc == 0 else ""


def _find_binary(root: str) -> tuple[str, str | None]:
    """Найти бинарник backend-adapter для plist/юнита: ``(путь, интерпретатор)``.

    Приоритет — **тот бинарник, которым выполнена команда**: именно его
    пользователь обновил, и только он гарантированно понимает ``--root``.
    Порядок:

    1. frozen-сборка (PyInstaller) — ``sys.executable``;
    2. запуск ``python backend-adapter.py`` — ``sys.argv[0]`` + ``sys.executable``
       как интерпретатор (launchd/systemd не умеют shebang-запуск);
    3. установленная консольная команда ``backend-adapter`` — ``sys.argv[0]``;
    4. ``<root>/backend-adapter.py`` (интерпретатор — ``_script_interpreter``);
    5. ``backend-adapter`` в ``PATH``.

    Каждый существующий кандидат проверяется пробой ``--help`` (см.
    ``_probe_binary``): бинарник без ``--root`` для сервиса бесполезен — он
    проигнорирует ``--root`` и упадёт на пустом ``ADAPTER_BACKEND_CONFIG``.
    Поэтому найденный, но несовместимый бинарник — ``[FATAL]``: plist с ним
    заведомо не запустится. Совсем не найден — ``[WARN]`` и путь по умолчанию
    (как раньше): пользователь мог поставить бинарник позже.
    """
    candidates: list[tuple[str, str | None]] = []
    argv0 = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else ""
    argv0_name = os.path.basename(argv0)
    if getattr(sys, "frozen", False):
        candidates.append((sys.executable, None))
    elif argv0_name == "backend-adapter.py":
        candidates.append((argv0, sys.executable))
    elif argv0_name == "backend-adapter":
        candidates.append((argv0, None))

    script_in_home = os.path.join(root, "backend-adapter.py")
    script_interpreter = _script_interpreter()
    candidates.append((script_in_home, script_interpreter))
    found = shutil.which("backend-adapter")
    if found:
        candidates.append((found, None))

    rejected: list[str] = []
    for binary, interpreter in candidates:
        if not os.path.isfile(binary):
            continue
        version = _probe_binary(binary, interpreter)
        if version is not None:
            print(f"[OK]   Бинарник: {binary}" + (f" ({version})" if version else ""))
            if interpreter:
                print(f"       Интерпретатор: {interpreter}")
            return binary, interpreter
        rejected.append(binary)

    if rejected:
        print(f"[FATAL] Бинарник не понимает --root: {', '.join(rejected)}")
        print("       Это версия без CLI (--root появился в v0.9.13): сервис с ней")
        print("       проигнорирует --root и упадёт на пустом ADAPTER_BACKEND_CONFIG.")
        print("       Обновите backend-adapter (install.sh) или положите")
        print(f"       backend-adapter.py в дом: {script_in_home}")
        sys.exit(1)

    print(f"[WARN] Бинарник не найден: {script_in_home}")
    print("       Убедитесь, что backend-adapter в PATH, или укажите --root.")
    return script_in_home, script_interpreter


def _service_log_path() -> str:
    """Файл, куда launchd пишет stdout/stderr агента.

    Путь живёт в ``service_log`` (модуль поднимает его и выше — он нужен и
    запущенному адаптеру для SIGHUP-переоткрытия, ADR-041). Здесь — только
    делегат, чтобы у CLI и адаптера не разъехались два пути.

    launchd без ``StandardOutPath``/``StandardErrorPath`` отправляет вывод
    сервиса в ``/dev/null`` — логов не остаётся вовсе. Поэтому генерируемый
    plist всегда задаёт этот файл, а ``--launchd logs`` читает его (``tail``).
    """
    return service_log.service_log_path()


def _write_launchd_plist(root: str) -> str:
    """Сгенерировать plist под ``root`` и записать в ``~/Library/LaunchAgents``.

    ``--root`` подход: токены в plist не попадают, настройки берутся из
    ``<root>/adapter.env``. Но два значения plist задаёт **сам** — они
    фундаментальны для сервиса и не должны зависеть от содержимого файла:

    * ``ADAPTER_BACKEND_CONFIG`` — путь к ``<root>/adapter.yaml``: без него
      агент упадёт на ``[FATAL] ADAPTER_BACKEND_CONFIG is not set``;
    * ``ADAPTER_DETACH_ENABLE=0`` — двойной форк несовместим с надзором
      launchd (как и у systemd-юнита).

    Значения ``EnvironmentVariables`` попадают в окружение процесса **до**
    чтения ``adapter.env``, где всё ставится через ``setdefault``, — потому
    plist побеждает файл. Вывод агента направляется в
    ``~/Library/Logs/backend-adapter/adapter.log`` (иначе launchd его теряет)
    — этот же файл читает ``--launchd logs``. Прежний plist, если был,
    сохраняется рядом как ``<plist>.bak``. Возвращает путь plist.
    """
    binary_path, interpreter = _find_binary(root)
    log_path = _service_log_path()
    # launchd не создаёт каталог лога сам — иначе агент молча не стартует.
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    plist_content = templates.render_launchd_plist(
        binary_path,
        root,
        interpreter=interpreter,
        env_vars={
            "ADAPTER_BACKEND_CONFIG": os.path.join(root, _YAML_NAME),
            "ADAPTER_DETACH_ENABLE": "0",
        },
        keep_alive=False,
        run_at_load=False,
        standard_out_path=log_path,
        standard_error_path=log_path,
    )

    plist_path = _plist_path()
    os.makedirs(os.path.dirname(plist_path), exist_ok=True)
    if os.path.exists(plist_path):
        backup = plist_path + ".bak"
        try:
            shutil.copy2(plist_path, backup)
            print(f"[WARN] {plist_path} уже существует — прежняя копия в {backup}.")
        except OSError as e:
            print(f"[WARN] Прежний plist не скопирован в {backup}: {e} — перезаписан.")

    with open(plist_path, "w", encoding="utf-8", newline="") as f:
        f.write(plist_content)
    os.chmod(plist_path, 0o644)
    return plist_path


def _launchd_install(root: str) -> None:
    """Разметить дом (если нужно) и установить launchd-агент.

    Если дом не размечен — вызывает ``_install_home`` автоматически. Затем
    генерирует и ставит минимальный plist (``--root <root>``) в
    ``~/Library/LaunchAgents``. Выводит инструкции для ``launchctl``.
    """
    if not _home_is_marked(root):
        _install_home(root)

    plist_path = _write_launchd_plist(root)

    print(f"[OK]   launchd-агент установлен: {plist_path}")
    print("       Запустить: backend-adapter --launchd load")
    print("       Остановить: backend-adapter --launchd unload")


def _launchd_plist_install(root: str) -> None:
    """Установить plist для УЖЕ размеченного дома, не трогая сам дом.

    В отличие от ``install`` не размечает дом и не перезаписывает
    ``adapter.env``/``adapter.yaml`` (их правки остаются как есть). Если дом
    не размечен (нет ``adapter.env``) — ``[FATAL]`` с подсказкой сначала
    выполнить ``--install``: без ``adapter.env`` агенту нечего загружать.
    """
    if not _home_is_marked(root):
        print(f"[FATAL] Дом не размечен: {root} (нет {_ENV_NAME}).")
        print(f"       Сначала разметьте дом: backend-adapter --install --root {root}")
        print("       Либо используйте --launchd install (разметит дом и поставит plist).")
        sys.exit(1)

    plist_path = _write_launchd_plist(root)

    print(f"[OK]   launchd-агент установлен: {plist_path}")
    print(f"       Дом не изменён: {root}")
    print("       Запустить: backend-adapter --launchd load")


def _launchd_target() -> str:
    """Адрес агента в launchd: ``gui/<uid>/<label>`` (сессия текущего юзера)."""
    return f"gui/{os.getuid()}/{_LAUNCHD_LABEL}"


def _launchd_loaded() -> bool:
    """Агент зарегистрирован в launchd? ``launchctl print`` → rc 0 (иначе 113)."""
    result = subprocess.run(
        ["launchctl", "print", _launchd_target()],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _launchd_fields() -> dict[str, str] | None:
    """Поля ``launchctl print`` агента; ``None`` — агент не зарегистрирован."""
    result = subprocess.run(
        ["launchctl", "print", _launchd_target()],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep:
            fields[key] = value
    return fields


def _wait_launchd_running(timeout: float, step: float) -> str | None:
    """Дождаться ``state = running``; вернуть PID или ``None`` по таймауту.

    ``kickstart -p`` возвращает PID асинхронно: агент уже запущен, но процесс
    может тут же умереть (например, ``EADDRINUSE`` — порт занят другим
    экземпляром). Без опроса команда ``load`` рапортовала бы «запущен» для
    мёртвого агента. Небольшое окно ожидания ловит именно такие старты.
    """
    deadline = time.monotonic() + timeout
    while True:
        fields = _launchd_fields()
        if fields and fields.get("state") == "running" and fields.get("pid"):
            return fields["pid"]
        if time.monotonic() >= deadline:
            return None
        time.sleep(step)


def _launchd_load(root: str) -> None:
    """Зарегистрировать и **запустить** launchd-агент.

    Два шага, потому что это разные операции launchd: ``bootstrap`` только
    регистрирует агент, а стартует его ``kickstart``. Плист генерируется с
    ``RunAtLoad=false`` (иначе агент поднимался бы при каждом входе, а
    ``install`` обязан только устанавливать), поэтому без ``kickstart``
    команда ``load`` ничего бы не запускала — процесс оставался бы в
    состоянии ``not running``.
    """
    plist_path = _plist_path()
    if not os.path.isfile(plist_path):
        print(f"[FATAL] Plist не найден: {plist_path}")
        print("       Сначала выполните: backend-adapter --launchd install")
        sys.exit(1)

    domain = f"gui/{os.getuid()}"
    if _launchd_loaded():
        # Уже зарегистрирован: bootstrap вернул бы «Input/output error» (5).
        # Регистрация есть — остаётся запустить.
        print(f"[OK]   Агент уже зарегистрирован: {_LAUNCHD_LABEL}")
    else:
        result = subprocess.run(
            ["launchctl", "bootstrap", domain, plist_path],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[FATAL] Не удалось зарегистрировать агент: {result.stderr.strip()}")
            print(f"       Попробуйте: launchctl bootstrap {domain} {plist_path}")
            sys.exit(1)
        print(f"[OK]   Агент зарегистрирован: {plist_path}")

    result = subprocess.run(
        ["launchctl", "kickstart", "-p", _launchd_target()],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[FATAL] Не удалось запустить агент: {result.stderr.strip()}")
        print("       Логи: backend-adapter --launchd logs")
        sys.exit(1)
    kicked_pid = result.stdout.strip()
    # kickstart -p отдаёт PID до того, как процесс успел упасть: при занятом
    # порте агент стартует и тут же умирает с EADDRINUSE. Опрос «state =
    # running» отличает живой запуск от мёртвого — иначе пишем «запущен» для
    # процесса, которого уже нет.
    pid = _wait_launchd_running(_LOAD_SETTLE_TIMEOUT, _LOAD_SETTLE_STEP)
    if pid is None:
        print("[WARN] Агент зарегистрирован, но процесс не поднялся.")
        print(f"       PID от launchd: {kicked_pid or '—'} — вероятно, упал на старте.")
        print("       Частая причина: порт занят другим экземпляром (EADDRINUSE).")
        _print_log_tail(_service_log_path())
        print("       Подробнее: backend-adapter --launchd logs -n 50")
        sys.exit(1)
    print(f"[OK]   Агент запущен: PID {pid}")
    print("       Логи: backend-adapter --launchd logs -f")


def _launchd_unload(root: str) -> None:
    """Остановить агент и снять его регистрацию (``launchctl bootout``).

    ``bootout`` останавливает процесс и выгружает агент одним шагом; повторный
    вызов на уже выгруженном агенте даёт ненулевой код — это не ошибка.
    """
    del root
    if not _launchd_loaded():
        print("[WARN] Агент не зарегистрирован — останавливать нечего.")
        return

    result = subprocess.run(
        ["launchctl", "bootout", _launchd_target()],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[WARN] Не удалось выгрузить агент: {result.stderr.strip()}")
        return
    print(f"[OK]   Агент остановлен: {_LAUNCHD_LABEL}")


def _print_log_tail(path: str, lines: int = 20) -> None:
    """Напечатать последние ``lines`` строк файла (если он есть).

    Читаем в Python, а не через ``tail``: ``status`` — диагностическая
    команда, и её вывод не должен зависеть от внешней утилиты. Файл может
    быть большим — читаем построчно кольцевым буфером (``deque``).
    """
    if not os.path.isfile(path):
        return
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = collections.deque(f, maxlen=lines)
    except OSError as e:
        print(f"       Хвост лога не прочитан: {e}")
        return
    if not tail:
        return
    print(f"       Последние строки лога ({path}):")
    for line in tail:
        print(f"       | {line.rstrip()}")


def _launchd_status(root: str) -> None:
    """Проверить статус launchd-агента: регистрация, состояние, PID, лог.

    Состояние берётся из ``launchctl print`` (машиночитаемые строки
    ``state = running`` / ``pid = N``), а не из ``launchctl list`` — тот
    печатает целый JSON-словарь, из которого человеку нужны две строки.
    """
    plist_path = _plist_path()
    log_path = _service_log_path()

    if not os.path.isfile(plist_path):
        print(f"[FATAL] Plist не найден: {plist_path}")
        print("       Сначала выполните: backend-adapter --launchd install")
        sys.exit(1)

    result = subprocess.run(
        ["launchctl", "print", _launchd_target()],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print("[WARN] Агент не зарегистрирован (запустите: backend-adapter --launchd load)")
        return

    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.strip().partition(" = ")
        if sep:
            fields[key] = value

    state = fields.get("state", "unknown")
    pid = fields.get("pid")
    if state == "running" and pid:
        print(f"[OK]   Агент работает: {_LAUNCHD_LABEL} (PID {pid})")
    else:
        print(f"[WARN] Агент зарегистрирован, но не работает (state: {state})")
        last_exit = fields.get("last exit code")
        if last_exit and last_exit != "(never exited)":
            print(f"       Последний код выхода: {last_exit}")
        # Хвост лога печатаем сразу: агент, падающий на старте, оставляет
        # причину именно там, и заставлять пользователя звать logs — лишний шаг.
        _print_log_tail(log_path)
        print("       Запустить: backend-adapter --launchd load")
        print("       Полный лог: backend-adapter --launchd logs -n 50")

    print(f"       Plist: {plist_path}")
    if os.path.isfile(log_path):
        print(f"       Лог:   {log_path}")
    else:
        print(f"       Лог:   {log_path} (ещё не создан — сервис не стартовал)")


def _launchd_plist(root: str) -> None:
    """Вывести содержимое plist на stdout."""
    plist_path = _plist_path()
    if not os.path.isfile(plist_path):
        print(f"[FATAL] Plist не найден: {plist_path}")
        print("       Сначала выполните: backend-adapter --launchd install")
        sys.exit(1)
    with open(plist_path, encoding="utf-8") as f:
        print(f.read(), end="")


def _launchd_plist_uninstall(root: str) -> None:
    """Удалить **только plist**: остановить агент, снять регистрацию, убрать файл.

    Дом (``<root>``) не трогается: ``adapter.env`` с токенами и конфиги
    остаются на месте. Каждый шаг терпит отсутствие объекта, поэтому
    повторный вызов — чистый no-op. Бэкапы ``<plist>.bak``, оставленные
    повторными ``install``, тоже убираются: это тот же артефакт сервиса.
    """
    plist_path = _plist_path()

    if _launchd_loaded():
        result = subprocess.run(
            ["launchctl", "bootout", _launchd_target()],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[WARN] Не удалось выгрузить агент: {result.stderr.strip()}")
        else:
            print(f"[OK]   Агент остановлен: {_LAUNCHD_LABEL}")
    else:
        print("[OK]   Агент не был зарегистрирован — пропущено.")

    removed = False
    for path in (plist_path, plist_path + ".bak"):
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"[OK]   Удалён файл: {path}")
                removed = True
            except OSError as e:
                print(f"[WARN] Не удалось удалить {path}: {e}")
    if not removed:
        print(f"[OK]   Plist не найден ({plist_path}) — удалять нечего.")

    print(f"       Дом не изменён: {root}")


# ── Полное удаление (v1.0.3): сервис + дом + лог сервиса ─────────────────
# ``uninstall`` у обеих систем = ``plist-uninstall``/``unit-uninstall``
# (только установочный артефакт сервиса) + удаление дома целиком. Это
# разрушительная операция: дом содержит ``adapter.env`` с токенами, поэтому
# она требует подтверждения (``--yes`` для неинтерактивного запуска).

# Корни, удалять которые целиком заведомо неверно: опечатка в ``--root``
# иначе снесла бы каталог пользователя.
_REFUSED_ROOTS = frozenset({"/", "~"})


def _check_purge_root(root: str) -> None:
    """Отказать, если ``root`` не похож на дом адаптера (защита от опечатки)."""
    expanded = os.path.expanduser(root)
    if expanded in _REFUSED_ROOTS or os.path.abspath(expanded) == os.path.expanduser("~"):
        print(f"[FATAL] Отказ: {root!r} — это домашний или корневой каталог, а не дом адаптера.")
        sys.exit(1)
    if not os.path.isdir(expanded):
        print(f"[FATAL] Дом не найден: {root}")
        sys.exit(1)
    marked = os.path.isfile(os.path.join(expanded, _ENV_NAME)) or os.path.isfile(
        os.path.join(expanded, _YAML_NAME)
    )
    if not marked:
        print(f"[FATAL] Отказ: в {root} нет {_ENV_NAME} или {_YAML_NAME} — это не дом адаптера.")
        print("       Полное удаление стирает каталог целиком; укажите верный --root.")
        sys.exit(1)


def _confirm_purge(root: str, extra: list[str], *, assume_yes: bool) -> None:
    """Показать план полного удаления и получить подтверждение.

    ``--yes`` — подтверждение дано заранее (скрипты, CI). Без него нужен
    интерактивный терминал: при перенаправленном stdin или не-tty ввод
    не запрашивается — ``[FATAL]`` с подсказкой добавить ``--yes`` (молчаливое
    удаление токенов по случайному запуску недопустимо).
    """
    print("Будут УДАЛЕНЫ:")
    print(f"  - дом адаптера: {root}")
    for item in extra:
        print(f"  - {item}")
    print("Дом содержит adapter.env с токенами — восстановить его будет нельзя.")
    if assume_yes:
        print("       Подтверждено флагом --yes.")
        return
    if not sys.stdin.isatty():
        print("[FATAL] Нет терминала для подтверждения. Добавьте --yes для удаления без вопросов.")
        sys.exit(1)
    try:
        answer = input("Продолжить? [y/N]: ")
    except EOFError:
        answer = ""
    if answer.strip().lower() not in ("y", "yes", "д", "да"):
        print("[OK]   Отменено — ничего не удалено.")
        sys.exit(0)


def _remove_path(path: str, *, errors: list[str]) -> bool:
    """Удалить файл или каталог; вернуть ``True``, если что-то удалено."""
    if not os.path.exists(path):
        return False
    try:
        if os.path.isdir(path) and not os.path.islink(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
    except OSError as e:
        print(f"[WARN] Не удалось удалить {path}: {e}")
        errors.append(path)
        return False
    print(f"[OK]   Удалён: {path}")
    return True


def _sudo_remove(path: str) -> bool:
    """Удалить системный файл через ``sudo rm -f``; ``True`` при успехе.

    ``-f`` делает операцию идемпотентной: файла нет — код 0, ошибки нет.
    Вызывается только когда файл существует (``uninstall`` не должен
    требовать пароля, если ротация не ставилась).
    """
    result = subprocess.run(["sudo", "rm", "-f", path])
    if result.returncode != 0:
        print(f"[WARN] Не удалось удалить {path} (код {result.returncode}).")
        return False
    print(f"[OK]   Удалён: {path}")
    return True


def _purge_paths(paths: list[str], *, empty_ok: str) -> list[str]:
    """Удалить перечисленные пути; вернуть список тех, что не удались."""
    errors: list[str] = []
    removed = False
    for path in paths:
        if _remove_path(path, errors=errors):
            removed = True
    if not removed and not errors:
        print(f"[OK]   {empty_ok} — удалять нечего.")
    return errors


def _is_inside(parent: str, child: str) -> bool:
    """``child`` лежит внутри ``parent`` (или совпадает) — после resolve."""
    parent_real = os.path.realpath(parent)
    child_real = os.path.realpath(child)
    return child_real == parent_real or child_real.startswith(parent_real + os.sep)


def _launchd_uninstall(root: str, *, assume_yes: bool = False) -> None:
    """Полное удаление (macOS): plist + дом + лог сервиса + конфиг ротации.

    Обратная операция к ``install``: сначала ``plist-uninstall`` (остановить
    агент, снять регистрацию, убрать plist), затем дом ``<root>`` целиком,
    каталог сервисного лога ``~/Library/Logs/backend-adapter/`` и, если он
    ставился, конфиг ротации ``/etc/newsyslog.d/backend-adapter.conf``.
    Требует подтверждения (``assume_yes`` — флаг ``--yes``).
    """
    log_dir = os.path.dirname(_service_log_path())
    # Лог вне дома; если root случайно указывает внутрь каталога лога или
    # наоборот — не удаляем вложенное дважды и не выходим за пределы.
    if _is_inside(root, log_dir) or _is_inside(log_dir, root):
        log_dir = ""
    _check_purge_root(root)
    conf_path = _newsyslog_conf_path()
    extra = [f"лог сервиса: {log_dir}"] if log_dir else []
    if os.path.exists(conf_path):
        extra.append(f"конфиг ротации: {conf_path}")
    _confirm_purge(root, extra, assume_yes=assume_yes)

    _launchd_plist_uninstall(root)
    # Конфиг ротации живёт в /etc: без снятия он остался бы правилом на
    # несуществующий файл. Через sudo и только если он установлен —
    # `uninstall` без ротации не должен требовать пароля.
    if os.path.exists(conf_path) and not _sudo_remove(conf_path):
        print(f"[WARN] Конфиг ротации не удалён: {conf_path}")
    sources = [root] + ([log_dir] if log_dir else [])
    errors = _purge_paths(sources, empty_ok="Дом и лог сервиса")
    if errors:
        print(f"[WARN] Удаление завершено с ошибками: {', '.join(errors)}")
    else:
        print(f"[OK]   Полное удаление завершено: {root}")


def _run_log_viewer(cmd: list[str], *, follow: bool, hint: str) -> None:
    """Запустить просмотрщик лога, наследуя консоль (``tail``/``journalctl``).

    Вывод не перехватывается: строки идут прямо в терминал, включая режим
    наблюдения (``-f``). Ctrl-C в режиме наблюдения — штатный способ выйти:
    ``KeyboardInterrupt`` (его получает и дочерний процесс в той же группе)
    гасится без traceback. ``hint`` — подсказка при ненулевом коде возврата.
    """
    try:
        result = subprocess.run(cmd)
    except KeyboardInterrupt:
        # Наблюдение остановлено пользователем — это не ошибка.
        print()
        sys.exit(0)
    if result.returncode not in (0, 1):
        print(f"[WARN] Не удалось прочитать лог (код {result.returncode}).")
        print(f"       {hint}")
    elif result.returncode == 1 and not follow:
        # Код 1 у tail/journalctl — «файла/записей нет»: подскажем, что делать.
        print(f"[WARN] {hint}")


def _launchd_logs(root: str, *, lines: int, follow: bool) -> None:
    """Показать хвост лога launchd-агента.

    ``lines`` — глубина разового вывода; ``follow`` — не завершаться, а
    следить за файлом до Ctrl-C. Файл
    (``~/Library/Logs/backend-adapter/adapter.log``) задан в plist как
    ``StandardOutPath``/``StandardErrorPath`` (см. ``_service_log_path``).
    """
    del root  # путь лога от дома не зависит
    log_path = _service_log_path()
    if not os.path.isfile(log_path):
        print(f"[WARN] Лог-файл не найден: {log_path}")
        print("       Сервис ещё не запускался (или агент установлен старым plist).")
        print("       Запуск: backend-adapter --launchd load")
        return

    cmd = ["tail", f"-n{lines}"]
    if follow:
        # -F переживает пересоздание файла (агент может перезапуститься).
        cmd.append("-F")
    cmd.append(log_path)
    _run_log_viewer(
        cmd,
        follow=follow,
        hint=f"Лог сервиса: {log_path} (запуск: backend-adapter --launchd load)",
    )


# ── Лог сервиса: очистка и ротация (v1.0.3, ADR-041) ────────────────────
# Лог сервиса — stdout/stderr процесса под launchd
# (~/Library/Logs/backend-adapter/adapter.log). launchd держит его открытым и
# сам не ротирует. Две команды закрывают обе стороны проблемы:
# ``logs-clear`` — разовая очистка, ``log-rotate`` — постоянная политика
# через штатный newsyslog macOS. Механика (truncate портит файл, SIGHUP
# переоткрытие) — в ``service_log`` и docs/adr/041.


def _launchd_logs_clear(root: str) -> None:
    """Очистить лог сервиса: удалить файл; работающий агент перезапустить.

    ``truncate`` не используется: fd launchd открыт без ``O_APPEND``, и
    писатель со своим смещением оставляет в файле NUL-дырку (замерено
    160–490 байт) — см. ADR-041. Файл удаляется; но пока жив старый процесс,
    его fd держит удалённый inode, и место не освобождается, поэтому
    работающий агент перезапускается (``kickstart -k``). Перезапуск рвёт
    in-flight запросы — это осознанный компромисс, о нём сообщаем.

    Остановленный (или незарегистрированный) агент: файл просто удаляется,
    ничего не поднимается. Файла нет — ``[WARN]`` и код 0 (идемпотентно).
    """
    del root  # путь лога от дома не зависит
    log_path = _service_log_path()
    try:
        removed = service_log.clear_log(log_path)
    except OSError as e:
        print(f"[FATAL] Не удалось удалить лог {log_path}: {e}")
        sys.exit(1)
    if not removed:
        print(f"[WARN] Лог-файл не найден: {log_path} — очищать нечего.")
    else:
        print(f"[OK]   Лог удалён: {log_path}")

    fields = _launchd_fields()
    if not fields:
        if removed:
            print("       Агент не зарегистрирован — файл создастся при следующем запуске.")
        return
    if fields.get("state") != "running":
        print(
            f"       Агент не работает (state: {fields.get('state', 'unknown')}) — перезапуск не нужен."
        )
        return

    # Живой процесс держит fd удалённого файла — без перезапуска место не
    # освободится, а новые строки шли бы в невидимый inode.
    result = subprocess.run(
        ["launchctl", "kickstart", "-k", "-p", _launchd_target()],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[WARN] Лог удалён, но агент не перезапущен: {result.stderr.strip()}")
        print("       Остановите и запустите вручную: --launchd unload && --launchd load")
        return
    pid = _wait_launchd_running(_LOAD_SETTLE_TIMEOUT, _LOAD_SETTLE_STEP)
    if pid is None:
        print("[WARN] Агент перезапущен, но процесс не поднялся — см. --launchd logs.")
        return
    print(f"[OK]   Агент перезапущен с чистым логом: PID {pid}")


def _service_pidfile(root: str) -> str:
    """PID-файл работающего агента по настройкам дома (без правки окружения).

    newsyslog получает PID из файла и шлёт ему SIGHUP. Путь считает сам
    адаптер (``daemon._pidfile_path``: ``<ADAPTER_DATA_ROOT>/var/<basename>``),
    поэтому его нельзя угадывать: дом вправе задать другой корень данных в
    ``adapter.env``. Здесь настройки дома читаются в **отдельный** словарь
    (не в ``os.environ`` — CLI не должен менять своё окружение): явное
    значение окружения побеждает файл, как и при обычном применении дома.
    """
    values: dict[str, str] = {}
    try:
        with open(os.path.join(root, _ENV_NAME), encoding="utf-8") as f:
            values = env_check.parse_env_text(f.read())
    except OSError:
        pass
    data_root = (
        os.environ.get("ADAPTER_DATA_ROOT", "").strip()
        or values.get("ADAPTER_DATA_ROOT", "").strip()
        or root
    )
    name = (
        os.environ.get("ADAPTER_PIDFILE", "").strip()
        or values.get("ADAPTER_PIDFILE", "").strip()
        or "adapter.pid"
    )
    return os.path.join(os.path.expanduser(data_root), "var", os.path.basename(name))


def _newsyslog_conf_path() -> str:
    """Путь конфига ротации в системном каталоге newsyslog."""
    return os.path.join(_NEWSYSLOG_DIR, _NEWSYSLOG_CONF_NAME)


def _read_pid(pidfile: str) -> int | None:
    """Прочитать PID из файла и проверить, что процесс жив (``kill -0``)."""
    try:
        with open(pidfile, encoding="utf-8") as f:
            pid = int(f.read().strip())
    except (OSError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return None
    except PermissionError:
        return pid  # процесс существует, но наш — чужой: сигнал не понадобится
    return pid


def _owner_group() -> str:
    """``uid:gid`` владельца лога — архив создаётся с этими же правами."""
    return f"{os.getuid()}:{os.getgid()}"


def _run_newsyslog_dry_run(conf_path: str) -> bool:
    """Проверить конфиг newsyslog без root и без изменения файлов.

    ``newsyslog -r -n -f <conf>`` снимает требование root, ничего не
    ротирует (``-n``) и печатает план. Код 0 — конфиг разобран; иначе в
    ``/etc`` его класть нельзя. Утилиты нет — считаем проверку пройденной с
    ``[WARN]``: ставим конфиг, но предупреждаем (на Linux этой команды нет
    вовсе — но ``log-rotate`` и задуман для macOS).
    """
    if shutil.which("newsyslog") is None:
        print("[WARN] newsyslog не найден — проверка конфига пропущена.")
        return True
    result = subprocess.run(
        ["newsyslog", "-r", "-n", "-f", conf_path],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True
    detail = (result.stderr or result.stdout).strip()
    print(f"[FATAL] newsyslog отверг конфиг (код {result.returncode}): {detail}")
    return False


def _sudo_install(source: str, target: str, *, mode: str = "644") -> bool:
    """Установить файл в системный каталог через ``sudo install``.

    ``sudo`` наследует терминал: пароль спрашивает он сам (без ``-n`` —
    команда интерактивна по замыслу). Отказ или ошибка — ``[FATAL]`` с
    подсказкой; временный файл удаляет вызывающий (``with``).
    """
    result = subprocess.run(["sudo", "install", "-m", mode, source, target])
    if result.returncode != 0:
        print(
            "[FATAL] Не удалось установить "
            f"{target} (код {result.returncode}). Нужны права root: команда "
            "запускает sudo."
        )
        return False
    return True


def _launchd_log_rotate(root: str) -> None:
    """Поставить ротацию лога сервиса через newsyslog (macOS, ADR-041).

    Порядок: проверить дом и plist → резолвить PID-файл и убедиться, что
    агент жив (без PID newsyslog не смог бы уведомить процесс, и тот писал
    бы в переименованный файл) → сгенерировать конфиг → проверить его
    dry-run'ом **без root** → установить через ``sudo install`` в
    ``/etc/newsyslog.d``. Демон newsyslog (``com.apple.newsyslog``, раз в час
    в :30) применит конфиг сам; перезапуск демона не нужен.
    """
    if not _home_is_marked(root):
        print(f"[FATAL] Дом не размечен: {root} (нет {_ENV_NAME}).")
        print(f"       Сначала: backend-adapter --launchd install --root {root}")
        sys.exit(1)
    if not os.path.isfile(_plist_path()):
        print(f"[FATAL] Plist не найден: {_plist_path()}")
        print("       Сначала выполните: backend-adapter --launchd install")
        sys.exit(1)

    log_path = _service_log_path()
    if not os.path.isfile(log_path):
        print(f"[FATAL] Лог сервиса не найден: {log_path}")
        print("       Сервис ещё не запускался — запустите его и повторите:")
        print("       backend-adapter --launchd load")
        sys.exit(1)
    pidfile = _service_pidfile(root)
    if _read_pid(pidfile) is None:
        print(f"[FATAL] PID-файл агента не найден или процесс не жив: {pidfile}")
        print("       Без PID newsyslog не сможет уведомить адаптер о ротации,")
        print("       и тот продолжит писать в переименованный файл.")
        print("       Перезапустите сервис (--launchd load) и повторите.")
        sys.exit(1)

    count, size_kb = 5, 1024
    content = templates.render_newsyslog_conf(
        log_path,
        owner_group=_owner_group(),
        pid_file=pidfile,
        count=count,
        size_kb=size_kb,
    )
    conf_path = _newsyslog_conf_path()
    fd, tmp_path = tempfile.mkstemp(prefix="backend-adapter-newsyslog-", suffix=".conf")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if not _run_newsyslog_dry_run(tmp_path):
            print("       Конфиг не установлен (в /etc ничего не изменено).")
            sys.exit(1)
        if not _sudo_install(tmp_path, conf_path):
            sys.exit(1)
    finally:
        with contextlib.suppress(OSError):
            os.remove(tmp_path)

    print(f"[OK]   Ротация лога настроена: {conf_path}")
    print(f"       Лог: {log_path}")
    print(f"       Политика: {count} архивов по {size_kb} КБ, gzip, только по размеру.")
    print("       Проверяет демон newsyslog (раз в час в :30).")
    print("       Снять: backend-adapter --launchd log-rotate-remove")


def _launchd_log_rotate_remove(root: str) -> None:
    """Снять конфиг ротации лога (``/etc/newsyslog.d``, через ``sudo``)."""
    del root  # путь конфига от дома не зависит
    conf_path = _newsyslog_conf_path()
    result = subprocess.run(["sudo", "rm", "-f", conf_path])
    if result.returncode != 0:
        print(f"[FATAL] Не удалось удалить {conf_path} (код {result.returncode}).")
        sys.exit(1)
    print(f"[OK]   Конфиг ротации снят: {conf_path}")
    print("       Уже ротированные архивы (adapter.log.0.gz …) не удаляются.")


# ── systemd (Linux, user-level) — аналог --launchd ──────────────────────
# User-level юнит (~/.config/systemd/user): без root, настройки из дома
# (--root), токены в юнит не попадают. Отдельно от системного юнита
# install.sh --service (/etc/systemd/system) — это другой уровень.


def _dispatch_systemd(
    action: str, root: str, *, lines: int, follow: bool, assume_yes: bool
) -> None:
    """Перенаправить подкоманду ``--systemd`` к нужной функции."""
    if action == "logs":
        _systemd_logs(root, lines=lines, follow=follow)
        return
    if action == "uninstall":
        _systemd_uninstall(root, assume_yes=assume_yes)
        return
    dispatch = {
        "install": _systemd_install,
        "unit-install": _systemd_unit_install,
        "start": _systemd_start,
        "stop": _systemd_stop,
        "status": _systemd_status,
        "unit": _systemd_unit,
        "unit-uninstall": _systemd_unit_uninstall,
    }
    func = dispatch.get(action)
    if func is None:
        print(f"[FATAL] --systemd: неизвестная подкоманда {action!r}.")
        sys.exit(2)
    func(root)


def _require_systemctl() -> None:
    """``systemctl`` обязателен для управления сервисом; иначе ``[FATAL]``."""
    if shutil.which("systemctl") is None:
        print("[FATAL] systemctl не найден — управление systemd недоступно.")
        print("       Установите systemd или используйте другой способ запуска.")
        sys.exit(1)


def _systemctl_user(*args: str) -> subprocess.CompletedProcess[str]:
    """Вызвать ``systemctl --user <args>`` и вернуть результат."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
    )


def _systemd_reload() -> None:
    """``systemctl --user daemon-reload`` — best-effort.

    Юнит-файл уже записан, это главное. Если ``systemctl`` нет (не-systemd
    хост, контейнер) или reload не удался — ``[WARN]``, но не фатал: файл
    установлен, пользователь разберётся с инициализацией сам.
    """
    if shutil.which("systemctl") is None:
        print("[WARN] systemctl не найден — daemon-reload пропущен.")
        return
    result = _systemctl_user("daemon-reload")
    if result.returncode != 0:
        print(f"[WARN] daemon-reload не удался: {result.stderr.strip()}")


def _write_systemd_unit(root: str) -> str:
    """Сгенерировать user-level юнит под ``root`` и записать его.

    ``--root`` подход: настройки из ``<root>/adapter.env``, токены в юнит не
    попадают. Как и plist, юнит сам задаёт ``ADAPTER_BACKEND_CONFIG`` (путь к
    ``<root>/adapter.yaml``) и ``ADAPTER_DETACH_ENABLE=0`` — значения юнита
    побеждают ``adapter.env`` (см. ``templates.render_systemd_unit``). Прежний
    юнит, если был, сохраняется рядом как ``<unit>.bak``. Возвращает путь
    юнита.
    """
    binary_path, interpreter = _find_binary(root)
    unit_content = templates.render_systemd_unit(binary_path, root, interpreter=interpreter)

    unit_path = _unit_path()
    os.makedirs(os.path.dirname(unit_path), exist_ok=True)
    if os.path.exists(unit_path):
        backup = unit_path + ".bak"
        try:
            shutil.copy2(unit_path, backup)
            print(f"[WARN] {unit_path} уже существует — прежняя копия в {backup}.")
        except OSError as e:
            print(f"[WARN] Прежний юнит не скопирован в {backup}: {e} — перезаписан.")

    with open(unit_path, "w", encoding="utf-8", newline="") as f:
        f.write(unit_content)
    os.chmod(unit_path, 0o644)
    return unit_path


def _systemd_install(root: str) -> None:
    """Разметить дом (если нужно) и установить user-level systemd-юнит.

    Только установка: юнит записывается и регистрируется (``daemon-reload``),
    но автозапуск НЕ включается и сервис НЕ стартует — запуск отдельной
    подкомандой ``start`` (и ``systemctl --user enable`` для автозагрузки).
    """
    if not _home_is_marked(root):
        _install_home(root)

    unit_path = _write_systemd_unit(root)
    _systemd_reload()

    print(f"[OK]   systemd-юнит установлен: {unit_path}")
    print("       Запустить: backend-adapter --systemd start")
    print("       Автозагрузка: systemctl --user enable backend-adapter.service")
    print("       Остановить: backend-adapter --systemd stop")


def _systemd_unit_install(root: str) -> None:
    """Установить юнит для УЖЕ размеченного дома, не трогая сам дом.

    В отличие от ``install`` не размечает дом и не перезаписывает
    ``adapter.env``/``adapter.yaml``. Если дом не размечен (нет
    ``adapter.env``) — ``[FATAL]`` с подсказкой сначала выполнить ``--install``.
    """
    if not _home_is_marked(root):
        print(f"[FATAL] Дом не размечен: {root} (нет {_ENV_NAME}).")
        print(f"       Сначала разметьте дом: backend-adapter --install --root {root}")
        print("       Либо используйте --systemd install (разметит дом и поставит юнит).")
        sys.exit(1)

    unit_path = _write_systemd_unit(root)
    _systemd_reload()

    print(f"[OK]   systemd-юнит установлен: {unit_path}")
    print(f"       Дом не изменён: {root}")
    print("       Запустить: backend-adapter --systemd start")


def _systemd_start(root: str) -> None:
    """Запустить сервис (``systemctl --user start``)."""
    _require_systemctl()
    result = _systemctl_user("start", _SYSTEMD_UNIT_NAME)
    if result.returncode != 0:
        print(f"[FATAL] Не удалось запустить сервис: {result.stderr.strip()}")
        print("       Сначала установите юнит: backend-adapter --systemd install")
        sys.exit(1)
    print(f"[OK]   Сервис запущен: {_SYSTEMD_UNIT_NAME}")


def _systemd_stop(root: str) -> None:
    """Остановить сервис (``systemctl --user stop``)."""
    _require_systemctl()
    result = _systemctl_user("stop", _SYSTEMD_UNIT_NAME)
    if result.returncode != 0:
        print(f"[WARN] Не удалось остановить сервис: {result.stderr.strip()}")
        return
    print(f"[OK]   Сервис остановлен: {_SYSTEMD_UNIT_NAME}")


def _systemd_status(root: str) -> None:
    """Проверить статус сервиса и процесса."""
    _require_systemctl()

    # status печатает отчёт и во встроенном формате, и в exit-код (0 — активен).
    result = _systemctl_user("status", "--no-pager", _SYSTEMD_UNIT_NAME)
    output = (result.stdout or result.stderr).strip()
    if output:
        print(output)
    if result.returncode == 0:
        print(f"[OK]   Сервис активен: {_SYSTEMD_UNIT_NAME}")
    else:
        print("[WARN] Сервис не активен (запустите: backend-adapter --systemd start)")

    # Проверка процесса — по аргументу --root (как у --launchd).
    result = subprocess.run(
        ["pgrep", "-f", rf"backend-adapter.*{root}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        pids = result.stdout.strip().split("\n")
        print(f"[OK]   Процесс запущен (PID: {', '.join(pids)})")
    else:
        print("[WARN] Процесс backend-adapter не найден.")


def _systemd_unit(root: str) -> None:
    """Вывести содержимое установленного юнита на stdout."""
    unit_path = _unit_path()
    if not os.path.isfile(unit_path):
        print(f"[FATAL] Юнит не найден: {unit_path}")
        print("       Сначала выполните: backend-adapter --systemd install")
        sys.exit(1)
    with open(unit_path, encoding="utf-8") as f:
        print(f.read(), end="")


def _systemd_unit_uninstall(root: str) -> None:
    """Удалить **только user-level systemd-юнит**: остановить, отключить, убрать файл.

    Дом (``<root>``) не трогается: ``adapter.env`` с токенами и конфиги
    остаются на месте. Каждый шаг терпит отсутствие объекта, поэтому
    повторный вызов — чистый no-op. Бэкапы ``<unit>.bak``, оставленные
    повторными ``install``, тоже убираются: это тот же артефакт сервиса.
    """
    unit_path = _unit_path()

    if shutil.which("systemctl") is not None:
        # disable --now = выключить автозагрузку и остановить сервис одним шагом.
        result = _systemctl_user("disable", "--now", _SYSTEMD_UNIT_NAME)
        if result.returncode != 0:
            print(f"[WARN] Не удалось остановить/отключить сервис: {result.stderr.strip()}")
        else:
            print(f"[OK]   Сервис остановлен и отключён: {_SYSTEMD_UNIT_NAME}")
    else:
        print("[WARN] systemctl не найден — остановка/отключение пропущены.")

    removed = False
    for path in (unit_path, unit_path + ".bak"):
        if os.path.exists(path):
            try:
                os.remove(path)
                print(f"[OK]   Удалён файл: {path}")
                removed = True
            except OSError as e:
                print(f"[WARN] Не удалось удалить {path}: {e}")
    if not removed:
        print(f"[OK]   Юнит не найден ({unit_path}) — удалять нечего.")

    if shutil.which("systemctl") is not None:
        _systemctl_user("daemon-reload")

    print(f"       Дом не изменён: {root}")


def _systemd_uninstall(root: str, *, assume_yes: bool = False) -> None:
    """Полное удаление (Linux): юнит + дом целиком.

    Обратная операция к ``install``: сначала ``unit-uninstall`` (остановить,
    отключить, убрать юнит), затем дом ``<root>``. Логи сервиса живут в
    journald, а не в доме, поэтому отдельного артефакта лога нет — записи
    журнала остаются (удалять чужие записи journald нельзя). Требует
    подтверждения (``assume_yes`` — флаг ``--yes``).
    """
    _check_purge_root(root)
    _confirm_purge(root, [], assume_yes=assume_yes)

    _systemd_unit_uninstall(root)
    errors = _purge_paths([root], empty_ok="Дом")
    if errors:
        print(f"[WARN] Удаление завершено с ошибками: {', '.join(errors)}")
    else:
        print(f"[OK]   Полное удаление завершено: {root}")


def _systemd_logs(root: str, *, lines: int, follow: bool) -> None:
    """Показать логи сервиса из journald (``journalctl --user -u <unit>``).

    Юнит пишет вывод в journal (``StandardOutput=journal``), поэтому источник
    лога — journald, а не файл: работают ротация и таймстампы. ``lines`` —
    глубина разового вывода (``-n``), ``follow`` — наблюдение до Ctrl-C
    (``-f``). ``journalctl`` обязателен — иначе ``[FATAL]``.
    """
    if shutil.which("journalctl") is None:
        print("[FATAL] journalctl не найден — просмотр логов journald недоступен.")
        print("       Установите systemd или смотрите вывод процесса вручную.")
        sys.exit(1)

    cmd = ["journalctl", "--user", "-u", _SYSTEMD_UNIT_NAME, "--no-pager", f"-n{lines}"]
    if follow:
        cmd.append("-f")
    _run_log_viewer(
        cmd,
        follow=follow,
        hint=(
            f"Лог сервиса: journalctl --user -u {_SYSTEMD_UNIT_NAME} "
            "(запуск: backend-adapter --systemd start)"
        ),
    )
