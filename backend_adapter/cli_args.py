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
import os
import shutil
import subprocess
import sys

from . import env_check, templates

# Ключи версии и справки (синонимы: короткий/длинный; -? — исторический
# «вопросительный» вариант, привычный по Windows-утилитам).
_VERSION_FLAGS = ("-v", "--version")
_HELP_FLAGS = ("-h", "--help", "-?")
_LAUNCHD_FLAG = "--launchd"
# install — разметить дом + plist; plist-install — только plist для уже
# готового дома (дом не трогает); остальные — управление установленным plist.
_LAUNCHD_ACTIONS = frozenset(
    {"install", "plist-install", "load", "unload", "status", "plist", "logs", "uninstall"}
)
# Действие по умолчанию, если подкоманда опущена (--launchd --root ...):
# так вызывают install.sh и scripts/install-launchd.sh.
_LAUNCHD_DEFAULT_ACTION = "install"

# Linux-аналог launchd: user-level systemd-юнит (~/.config/systemd/user).
# install — разметить дом + юнит (запуск отдельно, через start);
# unit-install — только юнит для уже готового дома; остальные — управление.
_SYSTEMD_FLAG = "--systemd"
_SYSTEMD_ACTIONS = frozenset(
    {"install", "unit-install", "start", "stop", "status", "unit", "logs", "uninstall"}
)
_SYSTEMD_DEFAULT_ACTION = "install"

# Просмотр логов сервиса (v1.0.2): ``--launchd logs`` / ``--systemd logs``.
# ``-n`` — глубина разового вывода (по умолчанию последние 20 строк),
# ``-f`` — не завершаться, а следить за логом до Ctrl-C.
_LOGS_DEFAULT_LINES = 20
_LOGS_LINES_FLAGS = ("-n", "--lines")
_LOGS_FOLLOW_FLAGS = ("-f", "--follow")

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
                     uninstall — остановить агент, снять регистрацию и удалить
                     plist (дом не трогает).
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
                     uninstall — остановить и отключить сервис, удалить юнит
                     (дом не трогает).
                     Все подкоманды принимают --root (по умолчанию ~/.ba).
  -n, --lines <N>    Сколько последних строк лога вывести (только с logs;
                     по умолчанию 20).
  -f, --follow       Не завершаться после вывода, а следить за логом сервиса
                     в консоли до Ctrl-C (только с logs).

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

    Подкоманда ``uninstall`` (v1.0.2) у обеих систем удаляет установочный
    артефакт сервиса (plist/юнит и его ``.bak``), предварительно остановив
    и сняв регистрацию; дом не трогается.
    """
    root: str | None = None
    install = False
    check = False
    launchd_action: str | None = None
    systemd_action: str | None = None
    logs_lines = _LOGS_DEFAULT_LINES
    logs_follow = False
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

    if check:
        sys.exit(env_check.run_check(_resolve_root(root)))
    if launchd_action:
        _dispatch_launchd(launchd_action, _resolve_root(root), lines=logs_lines, follow=logs_follow)
        sys.exit(0)
    if systemd_action:
        _dispatch_systemd(systemd_action, _resolve_root(root), lines=logs_lines, follow=logs_follow)
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


def _dispatch_launchd(action: str, root: str, *, lines: int, follow: bool) -> None:
    """Перенаправить подкоманду ``--launchd`` к нужной функции."""
    if action == "logs":
        _launchd_logs(root, lines=lines, follow=follow)
        return
    dispatch = {
        "install": _launchd_install,
        "plist-install": _launchd_plist_install,
        "load": _launchd_load,
        "unload": _launchd_unload,
        "status": _launchd_status,
        "plist": _launchd_plist,
        "uninstall": _launchd_uninstall,
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


def _probe_binary(binary: str, interpreter: str | None) -> str | None:
    """Проверить, что бинарник понимает ``--root``; вернуть его версию.

    Проба — единственный надёжный способ: standalone-бинарник PyInstaller
    сжат, искать в нём литерал ``--root`` бесполезно. Новый бинарник на
    ``--help`` сразу печатает справку с ``--root`` и выходит с кодом 0;
    старый (без CLI) справки не знает — запускает сервер и падает на
    ``[FATAL]`` (или висит, если окружение настроено), поэтому нужен таймаут.

    Возвращает строку версии (``backend-adapter X.Y.Z``) при успехе и
    ``None`` — если бинарник несовместим или не отвечает.
    """
    try:
        result = subprocess.run(
            _binary_argv(binary, interpreter, "--help"),
            capture_output=True,
            text=True,
            timeout=_BINARY_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[WARN] Не удалось проверить {binary}: {e}")
        return None
    if result.returncode != 0 or "--root" not in (result.stdout or ""):
        return None
    try:
        version = subprocess.run(
            _binary_argv(binary, interpreter, "--version"),
            capture_output=True,
            text=True,
            timeout=_BINARY_PROBE_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (version.stdout or "").strip() if version.returncode == 0 else ""


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

    Путь — ``~/Library/Logs/backend-adapter/adapter.log`` (тот же каталог, что
    у ``install.sh``), а **не** ``<root>/log``: в доме живут логи *работающего*
    адаптера (``session-*.log``, ``.err``, ``*.parts``), и складывать туда
    вывод сервиса значило бы смешивать два разных источника в одном каталоге
    данных.

    launchd без ``StandardOutPath``/``StandardErrorPath`` отправляет вывод
    сервиса в ``/dev/null`` — логов не остаётся вовсе. Поэтому генерируемый
    plist всегда задаёт этот файл, а ``--launchd logs`` читает его (``tail``).
    """
    return os.path.expanduser(os.path.join("~/Library/Logs", "backend-adapter", "adapter.log"))


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
    pid = result.stdout.strip()
    print(f"[OK]   Агент запущен{': PID ' + pid if pid else ''}")
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


def _launchd_uninstall(root: str) -> None:
    """Удалить launchd-агент: остановить, снять регистрацию, убрать plist.

    Дом (``<root>``) не трогается: ``adapter.env`` с токенами и конфиги
    остаются на месте. Удаляется только установочный артефакт сервиса.
    Каждый шаг терпит отсутствие объекта, поэтому повторный вызов — чистый
    no-op. Бэкапы ``<plist>.bak``, оставленные повторными ``install``, тоже
    убираются: это тот же артефакт сервиса.
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


# ── systemd (Linux, user-level) — аналог --launchd ──────────────────────
# User-level юнит (~/.config/systemd/user): без root, настройки из дома
# (--root), токены в юнит не попадают. Отдельно от системного юнита
# install.sh --service (/etc/systemd/system) — это другой уровень.


def _dispatch_systemd(action: str, root: str, *, lines: int, follow: bool) -> None:
    """Перенаправить подкоманду ``--systemd`` к нужной функции."""
    if action == "logs":
        _systemd_logs(root, lines=lines, follow=follow)
        return
    dispatch = {
        "install": _systemd_install,
        "unit-install": _systemd_unit_install,
        "start": _systemd_start,
        "stop": _systemd_stop,
        "status": _systemd_status,
        "unit": _systemd_unit,
        "uninstall": _systemd_uninstall,
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


def _systemd_uninstall(root: str) -> None:
    """Удалить user-level systemd-юнит: остановить, отключить, убрать файл.

    Дом (``<root>``) не трогается: ``adapter.env`` с токенами и конфиги
    остаются на месте. Удаляется только установочный артефакт сервиса.
    Каждый шаг терпит отсутствие объекта, поэтому повторный вызов — чистый
    no-op. Бэкапы ``<unit>.bak``, оставленные повторными ``install``, тоже
    убираются: это тот же артефакт сервиса.
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
