"""Шаблоны дефолтных настроек — встроены в код, а не в ``docs/samples/``.

``--install`` (v0.9.11) размечает домашнюю папку: кладёт ``adapter.yaml``,
``tariffs.yaml`` и ``adapter.env``. До v0.9.12 источником первых двух служил
каталог ``docs/samples/`` рядом с пакетом — но он есть только в репозитории с
исходниками. В standalone-бинарнике (PyInstaller ``--onefile``: ``__file__``
живёт в ``_MEIPASS``, куда ``docs/`` не попадает) и в wheel (``pyproject.toml``
включает только пакет ``backend_adapter/``) его нет, поэтому ``--install``
падал ``[FATAL]`` ещё до разметки.

Теперь типовые шаблоны лежат **в этом модуле** и потому доступны всегда — из
исходников, из wheel и из бинарника одинаково. ``templates.py`` — лист DAG
(только stdlib: строковые константы + ``os``), импортируется ``cli_args``.
Дополнительных данных в сборку класть не нужно: PyInstaller находит модуль в
пакете автоматически, ``--add-data`` не требуется.

**``docs/samples/*.yaml`` остаются** файлами-справочниками для читателя (их
видно в репозитории, на них ссылается документация). Правка шаблона — всегда
правка обоих мест; расхождение ловит ``tests/test_templates.py`` (сверка
байт-в-байт, пропускается там, где ``docs/samples/`` недоступен).

Почему ``adapter.env`` не хранится строкой, а рендерится: три пути
(``ADAPTER_BACKEND_CONFIG``, ``ADAPTER_MODELS_TARIFFS``, ``ADAPTER_DATA_ROOT``)
обязаны быть **абсолютными от ``<root>``**, чтобы ``--root`` работал при любом
рабочем каталоге; поэтому файл собирается из ``root`` в момент установки.
Сам ``ADAPTER_DATA_ROOT`` с v0.9.13 равен ``<root>`` (дом и корень данных
совпадают): конфиги и данные (``log/`` + ``var/``) лежат в одной папке.
"""

import os

# Имена файлов, которые --install кладёт в <root>. Экспортируются — потребитель
# (cli_args) не должен дублировать имена, под которые рендерится adapter.env.
ENV_NAME = "adapter.env"
YAML_NAME = "adapter.yaml"
TARIFFS_NAME = "tariffs.yaml"

# Имя переменной с токеном по умолчанию — то же, что поле key у образцового
# бэкенда SAMPLE_ADAPTER_YAML (llm-service). --install без --key берёт его;
# --install --key <имя> подставляет пользовательское имя и в adapter.yaml, и в
# adapter.env (см. render_env / render_adapter_yaml).
DEFAULT_KEY_ENV = "ADAPTER_BACKEND_KEY_LLM_SERVICE"


SAMPLE_ADAPTER_YAML = """\
# ============================================================
# sample.adapter.yaml — пример конфигурации LLM-бэкендов
# ============================================================
# Единственный способ указать путь подключения к бэкенду —
# переменная окружения ADAPTER_BACKEND_CONFIG, ссылающаяся на
# YAML-файл такой структуры (пример: docs/samples/sample.adapter.env).
# Готовый файл размечает `backend-adapter --install`; один бэкенд задаётся
# сразу: `--install --name <имя> --base <url> --key <имя_переменной_токена>`
# (v0.9.13) — формат записи тот же (name/base/key).
#
# backend:  — обязательный корневой ключ
#   - name: — произвольное имя бэкенда (используется в маршрутизации
#             моделей и в веб-статусе: «<имя>.<модель>» при коллизиях)
#     base: — базовый URL [OI]-совместимого эндпойнта LLM
#     key:  — имя переменной окружения, в которой лежит токен
#             (Bearer). Адаптер раскрывает её значение при старте;
#             сам токен в YAML-файл писать не нужно.
#     Примечание: ключ probe: в записи бэкенда больше не поддерживается —
#             дымовые пробы эндпойнтов сняты в v0.9.9. Проверка бэкенда —
#             только опрос списка моделей (GET /v1/models); неверный выбор
#             эндпойнта агентом фиксируется по факту ошибки. Ключ probe,
#             оставшийся в старом файле, молча игнорируется.
# ============================================================

backend:
  - name: llm-service
    base: https://llm.service.example.com
    key: ADAPTER_BACKEND_KEY_LLM_SERVICE

# Дополнительные бэкенды добавляются записями в тот же список:
#
#   - name: another
#     base: https://llm.service.another.com
#     key: ADAPTER_BACKEND_KEY_ANOTHER

# Если модель с одним id встречается на нескольких бэкендах — адаптер
# автоматически разводит коллизию префиксом: «<имя_бэкенда>.<модель>».
"""

SAMPLE_TARIFFS_YAML = """\
# ============================================================
# sample.tariffs.yaml — пример тарифов моделей (для колонки Cost)
# ============================================================
# Путь к этому файлу задаётся переменной окружения ADAPTER_MODELS_TARIFFS
# (пример: docs/samples/sample.adapter.env). Файл опционален: без него (или
# если он пуст/не читается) колонка Cost в таблице «Models in use» на
# статус-странице WEBUI показывает «--» — учёт токенов работает и без тарифов.
#
# Структура: корневой ключ tariffs: — список записей.
#   name:         — имя модели КАК ЕГО ВИДИТ КЛИЕНТ (то, что приходит в
#                   поле model запроса; с учётом префикса «<бэкенд>.» при
#                   коллизии имён между бэкендами).
#   backend:      — имя бэкенда из adapter.yaml. Необязательно: без поля
#                   запись действует для любого бэкенда (wildcard); с полем —
#                   только для названного. При совпадении обоих побеждает
#                   точная пара (name, backend).
#   input_price:  — цена за price_per ВХОДНЫХ токенов.
#   output_price: — цена за price_per ВЫХОДНЫХ токенов.
#   currency:     — 3-буквенный код валюты (USD, RUB, …).
#   price_per:    — база цены: на сколько токенов даны input/output_price.
#                   Дефолт 1 (цена за один токен); удобнее указывать
#                   1000000 — «цена за миллион токенов».
#
# Числа можно писать и с запятой в качестве десятичного разделителя
# (например, 0,25). Отрицательные цены трактуются как 0; отсутствующие
# input/output_price — тоже 0.
#
# Формула стоимости для строки таблицы:
#   cost = input_tokens  * input_price  / price_per
#        + output_tokens * output_price / price_per
# ============================================================

tariffs:
  # Бесплатная локальная модель: явные нули → cost 0, колонка покажет «0,00 ...».
  - name: local-model
    backend: local
    input_price: 0
    output_price: 0
    currency: RUB
    price_per: 1000000

  # Платная модель конкретного бэкенда: цена за миллион токенов.
  - name: paid-model
    backend: llm-service
    input_price: 10
    output_price: 30
    currency: RUB
    price_per: 1000000

  # Wildcard-тариф (без backend): действует на всех бэкендах, где встречается
  # такое имя модели, кроме случаев, когда выше есть точная пара (name, backend).
  - name: shared-model
    input_price: 1
    output_price: 3
    currency: USD
    price_per: 1000000
"""


def render_adapter_yaml(name: str, base: str, key_env: str) -> str:
    """Содержимое adapter.yaml для ``--install --name/--base/--key``.

    Один бэкенд, заданный пользователем: ``name`` — произвольное имя, ``base``
    — базовый URL [OI]-совместимого эндпойнта, ``key_env`` — **имя** переменной
    окружения с токеном (не сам токен). Формат совпадает с helper'ом
    ``write_backend_yaml`` в ``install.sh`` (service-установка) — правка
    раскладки должна попасть в оба места.
    """
    return (
        "# backend-adapter config (generated by --install)\n"
        "backend:\n"
        f"  - name: {name}\n"
        f"    base: {base}\n"
        f"    key: {key_env}\n"
    )


def render_env(root: str, *, key_env: str = DEFAULT_KEY_ENV) -> str:
    """Содержимое генерируемого adapter.env — все переменные с дефолтами.

    Значения совпадают с дефолтами ``config.py``/``docs/environment.md``;
    пути к данным и конфигам — абсолютные, вычисленные от ``root`` (чтобы
    запуск ``--root`` находил их при любом рабочем каталоге). ``ADAPTER_DATA_ROOT``
    равен ``root`` (v0.9.13: дом и корень данных совпадают).

    ``key_env`` — имя переменной токена: совпадает с полем ``key`` в
    генерируемом adapter.yaml (``--install --key``). Значение-заглушка
    ``*****``: реальный токен пользователь вписывает в файл (или задаёт в
    окружении — тогда побеждает именно оно).
    """
    data_root = root
    return f"""\
# adapter.env — окружение backend-adapter (сгенерировано --install).
# Заполните токен бэкенда в {key_env} (имя переменной
# задано полем key в adapter.yaml) и при необходимости поправьте значения.
# Переменные, заданные в оболочке, побеждают этот файл.

# --- Backend connection ---
export ADAPTER_BACKEND_CONFIG='{os.path.join(root, YAML_NAME)}'
export {key_env}='*****'

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
export ADAPTER_MODELS_TARIFFS='{os.path.join(root, TARIFFS_NAME)}'

# --- Input endpoint routing (TARGET) ---
# Все три входа принимаются и конвертируются в chat completions (дефолт).
export ADAPTER_MESSAGES_TARGET=completions
export ADAPTER_COMPLETIONS_TARGET=completions
export ADAPTER_RESPONSES_TARGET=completions
# Альтернативы: passthrough — дословная передача на входной эндпойнт бэкенда;
# none — выключить вход (404); messages — конвертация в Messages API.
# export ADAPTER_MESSAGES_TARGET=passthrough
# export ADAPTER_COMPLETIONS_TARGET=passthrough
# export ADAPTER_RESPONSES_TARGET=passthrough

# --- Sessions ---
# export ADAPTER_SESSION_HEADER='X-Claude-Code-Session-Id,x-opencode-session,x-codex-turn-metadata:session_id'
export ADAPTER_SESSIONS_TABLE=10

# --- Logging ---
export ADAPTER_DEBUG_ENABLE=0
export ADAPTER_DATA_ROOT='{data_root}'
export ADAPTER_LOG_TRIM=1000
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
