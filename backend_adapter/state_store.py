"""Перманентное состояние runtime-пула (v0.9.6): ``state.yaml``.

Хранит на диске значения настроек, которые можно менять на лету через WEBUI
``/config`` — то есть ровно ключи ``config.RUNTIME_CONFIG_POOL`` (объём
логирования/трейсов, рубильники стриминга, TARGET-маршрутизация входов,
маппинг моделей). Файл лежит в корне WEBUI рядом с ``model-usage.yaml``:
имя — ``config.ADAPTER_STATE`` (env ``ADAPTER_STATE``, дефолт ``state.yaml``),
директория — ``config.ADAPTER_DEBUG_LOGPATH`` (см. ``state_path``).

Зачем: настройки, выставленные через ``/config``, раньше жили только в
памяти процесса и терялись при перезапуске — приходилось снова править env.
Теперь каждое изменение пула записывается в файл, а на старте файл
применяется ПОВЕРХ env (файл побеждает переменные окружения: он отражает
последнее явное действие пользователя, а env — исходную конфигурацию).

Жизненный цикл:

- файла нет → он создаётся из текущих (env-)значений пула (``save``);
- файл есть → каждое валидное значение применяется через
  ``config.set_runtime_config`` (перекрывая env); невалидные ключи/значения
  пропускаются с консольным ``[WARN]`` — битый файл НЕ роняет старт
  (принцип проекта «битый файл — работаем дальше, ошибка в консоль»);
- любое последующее изменение пула (``/config`` POST) → колбек ``save``,
  зарегистрированный в ``config.set_on_change`` (см. ниже), дописывает файл.

ПЕР-СЕССИОННЫЕ настройки (``session_settings._OVERRIDES``) НЕ сохраняются:
они адресованы session_id и живут в памяти процесса по замыслу — сессия
эфемерна, её переопределения не должны переживать перезапуск адаптера и
распространяться на другие сессии.

Модуль — лист DAG: импортирует только ``config`` (корень) и stdlib. Обратной
зависимости нет: ``config`` о колбеке знает лишь как о непрозрачной функции
(``set_on_change``), а персистентность инициирует сам ``state_store`` при
импорте — цикла не возникает.
"""

import os
import threading

import yaml

from . import config

# Снимок последнего успешно записанного состояния: повторная запись того же
# содержимого пропускается (POST /config без реальных изменений не должен
# дёргать диск). None — файл ещё не писали в этом процессе.
_LAST_SAVED: dict | None = None

# Запись/чтение файла могут идти из разных потоков (WEBUI-сервер многопоточный:
# set_runtime_config вызывается обработчиком /config, а apply_on_startup — из
# главного потока старта) — сериализуем доступ к _LAST_SAVED и к самому файлу.
_LOCK = threading.Lock()


def state_path() -> str:
    """Путь к файлу состояния: LOGPATH (живое чтение) + имя ADAPTER_STATE.

    Читает атрибуты config на каждый вызов, а не снимок при импорте: тесты и
    смена точки хранения должны видеть актуальный путь.
    """
    return os.path.join(config.ADAPTER_DEBUG_LOGPATH, config.ADAPTER_STATE)


def load() -> dict:
    """Прочитать и ПРОВАЛИДИРОВАТЬ файл состояния.

    Возвращает словарь ``{имя_настройки: значение}``, содержащий только ключи
    из ``config.RUNTIME_CONFIG_POOL`` с корректным для своей настройки типом
    (``config.accepts_value``). Нет файла → ``{}`` без шума. Битый YAML,
    не-словарь в корне, неизвестный ключ или значение неверного типа —
    ``[WARN]`` в консоль, запись пропускается; остальные записи читаются.
    """
    path = state_path()
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"[WARN] state: не удалось прочитать {path}: {e} — работаем на env")
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        print(
            f"[WARN] state: {path} — ожидался словарь настроек, "
            f"получено {type(data).__name__} — работаем на env"
        )
        return {}

    valid: dict = {}
    for name, value in data.items():
        if name not in config.RUNTIME_CONFIG_POOL:
            print(f"[WARN] state: {path} — неизвестная настройка {name!r} пропущена")
            continue
        expected = config._RUNTIME_CONFIG_TYPES[name]
        if not config.accepts_value(expected, value):
            print(
                f"[WARN] state: {path} — {name}={value!r} не соответствует типу "
                f"настройки — пропущено"
            )
            continue
        valid[name] = value
    return valid


def save(values: dict) -> None:
    """Атомарно записать состояние пула в ``state.yaml``.

    В файл попадают только ключи ``config.RUNTIME_CONFIG_POOL`` (прочее
    молча отбрасывается — вызывающий обычно передаёт снимок
    ``config.get_runtime_config()``). Запись атомарная: временный файл в той
    же директории + ``os.replace``. Если содержимое совпадает с последним
    записанным снимком — запись пропускается. Ошибка записи не роняет
    вызывающего (канал наблюдательный): ``[WARN]`` в консоль.
    """
    payload = {k: v for k, v in values.items() if k in config.RUNTIME_CONFIG_POOL}
    global _LAST_SAVED
    with _LOCK:
        if payload == _LAST_SAVED:
            return
        path = state_path()
        tmp_path = f"{path}.tmp"
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp_path, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    payload,
                    f,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
            os.replace(tmp_path, path)
        except OSError as e:
            print(f"[WARN] state: не удалось записать {path}: {e}")
            return
        _LAST_SAVED = payload


def _on_config_change(values: dict) -> None:
    """Колбек config.set_on_change: снимок пула → на диск."""
    save(values)


def apply_on_startup() -> None:
    """Применить файл состояния на старте (файл ПОВЕРХ env) и подписаться.

    Файл есть → каждое прочитанное значение применяется через
    ``config.set_runtime_config`` (живое применение, как у POST /config).
    Файла нет → текущие env-значения немедленно записываются в файл, чтобы
    он существовал и дальше отражал актуальное состояние. В обоих случаях
    регистрируется колбек записи — последующие изменения ``/config``
    персистентны. Битый/частично невалидный файл не роняет старт: пропуски
    уже отмечены ``[WARN]`` в ``load``, остаются env-значения.
    """
    config.set_on_change(_on_config_change)
    data = load()
    if not data:
        # Файла нет (или он пуст/битый) — фиксируем стартовое состояние из env.
        save(config.get_runtime_config())
        return
    config.set_runtime_config(**data)
