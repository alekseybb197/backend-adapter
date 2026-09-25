"""CLI-ключи адаптера: ``--version``, ``--help``, ``--root``, ``--install``.

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
      adapter.env     — переменные окружения (генерирует --install)
      adapter.yaml    — конфигурация бэкендов (копия sample.adapter.yaml)
      tariffs.yaml    — тарифы моделей (копия sample.tariffs.yaml)
      tmp/adapter/    — ADAPTER_DATA_ROOT: внутри log/ и var/

Запуск с ``--root`` подставляет из дома ровно то, чего нет в окружении
(``os.environ.setdefault`` — **явный env всегда побеждает**): путь данных,
переменные из ``adapter.env``, ``adapter.yaml``/``tariffs.yaml``. Без
``--root``/``--install`` дом не читается вовсе — поведение «нулевой настройки»
(дефолт ``./tmp/adapter``, обязательный ``ADAPTER_BACKEND_CONFIG``) сохраняется.

Модуль — лист DAG (только stdlib). Версию принимает аргументом: импортировать
``backend-adapter.py`` нельзя (имя с дефисом — не валидный идентификатор), а
держать её здесь значило бы раздвоить единственный источник версии
(см. докстринг ``cli.py``).

Разбор — ручной, а не ``argparse``: нужен ключ ``-?`` (argparse трактует
``-h``/``--help`` специально), а для горстки плоских ключей таблица проще
настройки ``add_help=False``.
"""

import os
import sys

# Ключи версии и справки (синонимы: короткий/длинный; -? — исторический
# «вопросительный» вариант, привычный по Windows-утилитам).
_VERSION_FLAGS = ("-v", "--version")
_HELP_FLAGS = ("-h", "--help", "-?")

# Домашняя папка адаптера по умолчанию: используется для --install без
# --root (--root без аргумента — ошибка, см. parse_args).
_DEFAULT_ROOT = "~/.ba"

# Подпапка данных внутри дома — тот же дефолт, что ADAPTER_DATA_ROOT в
# «нулевой настройке» (config.ADAPTER_DATA_ROOT), только от корня дома.
_DATA_SUBDIR = os.path.join("tmp", "adapter")

# Имена файлов, которые --install кладёт в дом (adapter.env генерируется,
# два других копируются из docs/samples/).
_ENV_NAME = "adapter.env"
_YAML_NAME = "adapter.yaml"
_TARIFFS_NAME = "tariffs.yaml"

_HELP_TEXT = """\
backend-adapter — [AN] Messages <-> [OI]-compatible backends for AI agents

Usage:
  backend-adapter [OPTIONS]

Options:
  -v, --version      Показать версию и завершить работу.
  -h, --help, -?     Показать эту справку и завершить работу.
  --root <путь>      Домашняя папка адаптера (по умолчанию ~/.ba). Из неё
                     подставляются недостающие настройки: корень данных
                     <путь>/tmp/adapter, переменные <путь>/adapter.env,
                     конфиги <путь>/adapter.yaml и <путь>/tariffs.yaml.
                     Явно заданные переменные окружения всегда побеждают.
  --install          Разметить домашнюю папку для работы адаптера: создать
                     каталоги данных и положить adapter.env (все переменные
                     со значениями по умолчанию), sample.adapter.yaml и
                     sample.tariffs.yaml. Существующие файлы не перезаписывает.
                     Завершает работу, адаптер не запускает.

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
    """
    root: str | None = None
    install = False
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
        elif arg == "--root":
            i += 1
            if i >= len(argv):
                print("[FATAL] --root требует путь к домашней папке.")
                sys.exit(2)
            root = argv[i]
        else:
            print(f"[FATAL] Unknown option: {arg!r}. См. backend-adapter --help.")
            sys.exit(2)
        i += 1

    if install:
        _install_home(_resolve_root(root))
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
    сам, как реальный ``~/.ba``: там путь данных — ``tmp/logs``), и лишь
    затем — подстановка путей по умолчанию от ``root``. Каждое присваивание
    через ``setdefault``: уже заданное в окружении значение сильнее и файла,
    и подстановки.
    """
    _load_env_file(os.path.join(root, _ENV_NAME))
    os.environ.setdefault("ADAPTER_DATA_ROOT", os.path.join(root, _DATA_SUBDIR))
    os.environ.setdefault("ADAPTER_BACKEND_CONFIG", os.path.join(root, _YAML_NAME))
    tariffs = os.path.join(root, _TARIFFS_NAME)
    if os.path.isfile(tariffs):
        os.environ.setdefault("ADAPTER_MODELS_TARIFFS", tariffs)


def _load_env_file(path: str) -> None:
    """Прочитать env-файл и подставить НЕзаданные переменные.

    Формат — построчный ``export K=V`` / ``K=V`` (как ``source`` в sh, но без
    исполнения shell): снимаются кавычки, ``#`` в начале строки — комментарий,
    пробелы вокруг имени/значения игнорируются. Уже заданное в окружении
    значение сохраняется (``setdefault``) — файл дома лишь заполняет пробелы.
    Отсутствие файла — не ошибка (дом может обходиться без adapter.env).
    """
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key or key in os.environ:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        os.environ[key] = value


def _install_home(root: str) -> None:
    """Разметить домашнюю папку; существующие файлы не трогать.

    Создаёт ``<root>`` с подпапками данных (log/ + var/ — те же, что создаёт
    старт адаптера), кладёт ``adapter.yaml`` и ``tariffs.yaml`` из
    ``docs/samples/`` и генерирует ``adapter.env`` со значениями по умолчанию.
    Каждый уже существующий файл — пропуск с ``[WARN]`` (идемпотентность:
    повторный запуск ничего не ломает и не перезаписывает правки пользователя).
    """
    samples = _samples_dir()
    os.makedirs(os.path.join(root, _DATA_SUBDIR, "log"), exist_ok=True)
    os.makedirs(os.path.join(root, _DATA_SUBDIR, "var"), exist_ok=True)

    _copy_sample(os.path.join(samples, "sample.adapter.yaml"), os.path.join(root, _YAML_NAME))
    _copy_sample(os.path.join(samples, "sample.tariffs.yaml"), os.path.join(root, _TARIFFS_NAME))

    env_path = os.path.join(root, _ENV_NAME)
    if os.path.exists(env_path):
        print(f"[WARN] {env_path} уже существует — оставлен без изменений.")
    else:
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(_default_env_file(root))
        # В adapter.env попадает токен бэкенда — файл только для владельца.
        os.chmod(env_path, 0o600)
        print(f"[OK]   {env_path} — создан (заполните токен).")

    print(f"[OK]   Домашняя папка адаптера готова: {root}")
    print(f"       Запуск: backend-adapter --root {root}")


def _samples_dir() -> str:
    """Каталог ``docs/samples/`` рядом с пакетом; понятный [FATAL] если нет."""
    # cli_args.py лежит в backend_adapter/ — образцы на уровень выше, в docs/.
    samples = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "samples")
    samples = os.path.normpath(samples)
    if not os.path.isdir(samples):
        print(
            "[FATAL] Не найдены образцы конфигов (docs/samples/) — --install "
            "доступен только при запуске из репозитория с исходниками "
            "(в standalone-бинарник образцы не входят)."
        )
        sys.exit(2)
    return samples


def _copy_sample(src: str, dst: str) -> None:
    """Скопировать образец, не перезаписывая существующий файл."""
    if os.path.exists(dst):
        print(f"[WARN] {dst} уже существует — оставлен без изменений.")
        return
    try:
        with open(src, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"[FATAL] Не удалось прочитать образец {src}: {e}")
        sys.exit(2)
    with open(dst, "wb") as f:
        f.write(data)
    print(f"[OK]   {dst} — создан.")


def _default_env_file(root: str) -> str:
    """Содержимое генерируемого adapter.env — все переменные с дефолтами.

    Значения совпадают с дефолтами ``config.py``/``docs/environment.md``;
    пути к данным и конфигам — абсолютные, вычисленные от ``root`` (чтобы
    запуск ``--root`` находил их при любом рабочем каталоге).
    """
    data_root = os.path.join(root, _DATA_SUBDIR)
    return f"""\
# adapter.env — окружение backend-adapter (сгенерировано --install).
# Заполните токен бэкенда в ADAPTER_BACKEND_KEY_LLM_SERVICE (имя переменной
# задано полем key в adapter.yaml) и при необходимости поправьте значения.
# Переменные, заданные в оболочке, побеждают этот файл.

# --- Backend connection ---
export ADAPTER_BACKEND_CONFIG='{os.path.join(root, _YAML_NAME)}'
export ADAPTER_BACKEND_KEY_LLM_SERVICE='*****'

# --- Server settings ---
export ADAPTER_PROXY_PORT=9999
export ADAPTER_ENDPOINT_HOST="127.0.0.1"

# --- Network ---
export ADAPTER_TIMEOUT=300
export ADAPTER_RETRY_COUNT=3

# --- Streaming ---
export ADAPTER_STREAMING_ENABLE=1
export ADAPTER_STREAM_INCLUDE_USAGE=1

# --- Models ---
export ADAPTER_STRICT_MODELS=1
export ADAPTER_MODELS_MAPPING=""
export ADAPTER_MODELS_TARIFFS='{os.path.join(root, _TARIFFS_NAME)}'

# --- Input endpoint routing (TARGET) ---
export ADAPTER_MESSAGES_TARGET=completions
# export ADAPTER_COMPLETIONS_TARGET=passthrough
# export ADAPTER_RESPONSES_TARGET=passthrough

# --- Sessions ---
# export ADAPTER_SESSION_HEADER='X-Claude-Code-Session-Id,x-opencode-session,x-codex-turn-metadata:session_id'
export ADAPTER_SESSIONS_TABLE=10

# --- Logging ---
export ADAPTER_DEBUG_ENABLE=0
export ADAPTER_DATA_ROOT='{data_root}'
export ADAPTER_DEBUG_TRIM=3000
export ADAPTER_TRACE_REASONING_MAX_CHARS=0
export ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS=0

# --- Sanitizer (secret masking in logs) ---
export ADAPTER_SENSITIVE_LOGGING_ENABLE=0

# --- WEBUI ---
export ADAPTER_WEBUI_HOST="127.0.0.1"
export ADAPTER_WEBUI_PORT=8765
# export ADAPTER_EXPORTER_ENABLE=1
# export ADAPTER_EXPORTER_PORT=9100

# --- Mode ---
export ADAPTER_DETACH_ENABLE=0
export ADAPTER_PIDFILE='adapter.pid'
# export ADAPTER_STATE='state.yaml'
"""
