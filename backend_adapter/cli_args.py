"""CLI-ключи адаптера: ``--version``, ``--help``, ``--root``, ``--install``,
``--name``/``--base``/``--key``, ``--check``.

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
``adapter.yaml``/``tariffs.yaml``. Без ``--root``/``--install`` дом не читается
вовсе, но дефолт ``ADAPTER_DATA_ROOT`` всё равно ``~/.ba``: «нулевой запуск»
требует лишь ``ADAPTER_BACKEND_CONFIG`` в окружении.

Модуль — лист DAG: stdlib + ``templates`` (тоже stdlib-only), откуда берутся
встроенные шаблоны ``--install``. Версию принимает аргументом: импортировать
``backend-adapter.py`` нельзя (имя с дефисом — не валидный идентификатор), а
держать её здесь значило бы раздвоить единственный источник версии
(см. докстринг ``cli.py``).

Разбор — ручной, а не ``argparse``: нужен ключ ``-?`` (argparse трактует
``-h``/``--help`` специально), а для горстки плоских ключей таблица проще
настройки ``add_help=False``.
"""

import os
import sys

from . import env_check, templates

# Ключи версии и справки (синонимы: короткий/длинный; -? — исторический
# «вопросительный» вариант, привычный по Windows-утилитам).
_VERSION_FLAGS = ("-v", "--version")
_HELP_FLAGS = ("-h", "--help", "-?")

# Домашняя папка адаптера по умолчанию: используется для --install без
# --root (--root без аргумента — ошибка, см. parse_args).
_DEFAULT_ROOT = "~/.ba"

# Имена файлов дома — в templates (там же, где рендерится adapter.env); берём
# их оттуда, чтобы не держать два списка имён.
_ENV_NAME = templates.ENV_NAME
_YAML_NAME = templates.YAML_NAME
_TARIFFS_NAME = templates.TARIFFS_NAME

_HELP_TEXT = """\
backend-adapter — [AN] Messages <-> [OI]-compatible backends for AI agents

Usage:
  backend-adapter [OPTIONS]

Options:
  -v, --version      Показать версию и завершить работу.
  -h, --help, -?     Показать эту справку и завершить работу.
  --root <путь>      Домашняя папка адаптера (по умолчанию ~/.ba). Одновременно
                     корень данных (ADAPTER_DATA_ROOT). Из неё подставляются
                     недостающие настройки: переменные <путь>/adapter.env,
                     конфиги <путь>/adapter.yaml и <путь>/tariffs.yaml,
                     данные <путь>/log и <путь>/var.
                     Явно заданные переменные окружения всегда побеждают.
  --install          Разметить домашнюю папку для работы адаптера: создать
                     каталоги данных и положить adapter.env (все переменные
                     со значениями по умолчанию), adapter.yaml и tariffs.yaml.
                     Существующие файлы не перезаписывает. Завершает работу,
                     адаптер не запускает.
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
    """
    root: str | None = None
    install = False
    check = False
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

    if check:
        sys.exit(env_check.run_check(_resolve_root(root)))
    if install:
        _install_home(_resolve_root(root), name=name, base=base, key_env=key)
        sys.exit(0)
    if root is not None:
        _apply_home(_resolve_root(root))


def _resolve_root(root: str | None) -> str:
    """Абсолютный путь домашней папки; ``None`` → дефолт ``~/.ba``."""
    return os.path.abspath(os.path.expanduser(root or _DEFAULT_ROOT))


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
    """Записать файл из встроенного шаблона, не перезаписывая существующий.

    ``newline=""`` отключает трансляцию ``\\n`` → ``os.linesep``: файлы ложатся
    ровно теми байтами, что и ``docs/samples/*.yaml`` (важно для Windows, где
    иначе получился бы CRLF). ``mode`` — права создаваемого файла (``None`` —
    по umask).
    """
    if os.path.exists(path):
        print(f"[WARN] {path} уже существует — оставлен без изменений.")
        return
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(content)
    if mode is not None:
        os.chmod(path, mode)
    print(f"[OK]   {path} — создан{hint}.")
