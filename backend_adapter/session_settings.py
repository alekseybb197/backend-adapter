"""session_settings.py — пер-сессионные переопределения настроек (v0.9.5).

Зачем модуль: до v0.9.5 «сессия агента» была единицей НАБЛЮДЕНИЯ (таблица
Sessions WEBUI, см. session_registry) — адаптер умел показать, чем трафик
одной сессии отличается от другой, но не умел этой разницей УПРАВЛЯТЬ.
Теперь сессия — ещё и единица управления: набор настроек, переопределяемых
для одного ``session_id`` поверх общих настроек приложения.

Пул переопределяемого — ``config.SESSION_CONFIG_POOL`` (логирование +
TARGET-роутинг входов), типы и домены — ``config._SESSION_CONFIG_TYPES``.
(v0.9.6: отдельно от этого пула, в том же хранилище ``_OVERRIDES``, живёт
служебный ключ активной модели сессии — см. set_model_override/
model_override ниже. Он не проходит через set_config/SESSION_CONFIG_POOL,
так как это не enum/bool-тумблер, а произвольное имя модели, которое
сессия задаёт себе сама командой "/model <имя>" в чате.)
**Две модели наследования (v0.9.8).** Настройки пула делятся на два вида,
и это деление принципиально:

  - **Логирование (Log) — СНИМОК.** В момент образования сессии
    (``ensure_session``, первое обращение к ``session_id``) значение общего
    тумблера ``config.ADAPTER_DEBUG`` КОПИРУЕТСЯ в переопределения сессии.
    Дальше сессия живёт своим значением, а общий тумблер служит лишь шаблоном
    для НОВЫХ сессий: его последующая смена уже существующие сессии не
    трогает. Иначе говоря, глобальный флаг ничего не включает и не выключает —
    он только инициализирует (v0.9.8). Состояния ``"inherit"`` у этого поля
    нет: домен — bool, а «вернуться к общему» выражается повторной
    инициализацией из текущего общего значения (``set_config(..., clear=...)``)
    — тоже снимок, а не живая связь. В WEBUI у селекта Log ровно два
    положения: on/off. (v0.9.10: второй флаг — Parts — снят; части протокола
    ``*.parts`` собираются вместе с логами по тому же ADAPTER_DEBUG.)
  - **TARGET-поля — ЖИВОЕ НАСЛЕДОВАНИЕ.** Два состояния (v0.9.9):
    - НЕ ЗАДАНА (записи нет) — действует общая настройка приложения
      (``config.X``), включая последующие изменения через /config. Это и
      есть «вернуться к общему»: состояния ``"inherit"`` у TARGET больше
      нет вовсе (домен — ``config.TARGET_ALLOWED_VALUES``, как у глобальной
      настройки), а выбор в WEBUI значения, совпадающего с текущим общим,
      снимает переопределение записью ``set_config(..., clear=...)``;
    - конкретное значение — собственно переопределение.

Хранение — в памяти процесса, ключ = ``session_id`` (тот же идентификатор,
что в колонке «Сессия» таблицы Sessions, т.е. значение заголовка
ADAPTER_SESSION_HEADER). Не персистентно, как и сама таблица Sessions:
перезапуск адаптера возвращает все сессии к общим настройкам. Записи
переживают вытеснение строки из таблицы Sessions (настройка адресуется
сессии, а не строке-кортежу): вернувшаяся сессия получит свои прежние
переопределения.

Сессия без идентификатора (пустой ``session_id``) настройками не
адресуется: ``ensure_session`` для неё — no-op, а гейты логирования
(``session_log.logging_enabled``) отвечают False, поэтому файлов на диске
такой запрос не создаёт (v0.9.8).

Модуль — лист DAG: на верхнем уровне импортирует только ``config``.
Потребители: ``routing`` (TARGET для decide/target_for_input), ``logger``/
``tracer``/``session_log`` (гейт файловой записи), ``webui_sessions``
(страница и API).
"""

from __future__ import annotations

import threading
from typing import Any

from . import config

# session_id -> {имя_настройки: значение}. Значение — уже провалидированное
# (bool или строка из домена).
_OVERRIDES: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()

# Ключ переопределения активной модели сессии (v0.9.6, routing:
# responses→responses / detect_model_switch_command в convert.py). Хранится
# в ТОМ ЖЕ ``_OVERRIDES``, что и обычные настройки, но НЕ через
# config.SESSION_CONFIG_POOL/set_config: это не переключаемый в WEBUI
# конфиг-тумблер с доменом enum/bool, а произвольное имя модели, которое
# сессия задаёт себе сама командой "/model <имя>" в чате. Ведущее
# подчёркивание — соглашение «служебный ключ, не для set_config/WEBUI»
# (нет в SESSION_CONFIG_POOL, поэтому set_config его молча проигнорировал
# бы, если бы кто-то передал его туда напрямую).
_MODEL_OVERRIDE_KEY = "_model_override"

# Настройки, у которых модель наследования — СНИМОК (v0.9.8): их значения
# инициализируются из общего тумблера в момент образования сессии
# (ensure_session) и дальше живут независимо от config. У них нет состояния
# "inherit", а «сбросить» означает «взять общий тумблер заново» (свежий
# снимок), а не «живую связь». v0.9.10: остался один Log — пара Log/Parts
# схлопнута в один флаг.
_SNAPSHOT_NAMES = ("ADAPTER_DEBUG",)

# session_id, для которых снимок уже взят. Отдельно от _OVERRIDES, потому что
# запись может быть снята (clear_session) — повторный снимок делать нельзя:
# сессия уже существует, и её флаг не должен «подхватывать» более позднее
# изменение общего тумблера.
_SEEDED: set[str] = set()

__all__ = [
    "reset",
    "ensure_session",
    "override",
    "effective",
    "session_overrides",
    "set_config",
    "clear_session",
    "set_model_override",
    "model_override",
]


def reset() -> None:
    """Полный сброс таблицы переопределений (изоляция тестов)."""
    with _LOCK:
        _OVERRIDES.clear()
        _SEEDED.clear()


def _snapshot_values() -> dict[str, Any]:
    """Снимок общего флага логирования для сессии (v0.9.8).

    Единственное место, где глобальный ``config.ADAPTER_DEBUG`` попадает в
    сессию. Снимок — ТОЧНАЯ копия флага: поведение новой сессии предсказуемо
    повторяет общий флаг как есть (v0.9.10: пара Log/Parts схлопнута, копировать
    больше нечего — отбор идёт по ``_SNAPSHOT_NAMES``)."""
    return {name: bool(getattr(config, name, False)) for name in _SNAPSHOT_NAMES}


def ensure_session(session_id: str) -> bool:
    """Образовать сессию: скопировать в неё текущий общий флаг логирования.

    Вызывается на каждом обращении к ``session_id`` (идемпотентно, дёшево:
    проверка множества под блокировкой), но снимок берётся РОВНО ОДИН РАЗ —
    при первой встрече. С этого момента сессия живёт своим значением Log,
    а общий тумблер служит лишь шаблоном для НОВЫХ сессий: его последующая
    смена через /config уже существующие сессии не трогает (v0.9.8,
    требование «глобальные флаги не включают и не выключают функционал»).

    Заполняются только ОТСУТСТВУЮЩИЕ ключи: если сессия успела задать Log
    явно (страница /sessions) до первого запроса, её значение не затирается
    общим — снимок дополняет, а не переписывает.

    Пустой ``session_id`` — no-op (False): сессия не идентифицирована,
    инициализировать нечего. Исключений не бросает — настройка наблюдаемости
    не должна ронять запрос. Возвращает True, если снимок взят этим вызовом."""
    if not session_id:
        return False
    try:
        with _LOCK:
            if session_id in _SEEDED:
                return False
            _SEEDED.add(session_id)
            row = _OVERRIDES.setdefault(session_id, {})
            for name, value in _snapshot_values().items():
                row.setdefault(name, value)
        return True
    except Exception:
        return False


def override(session_id: str, name: str) -> Any | None:
    """Сырое переопределение настройки для сессии (или None, если не задано).

    Возвращается именно ХРАНИМОЕ значение: ``None`` — переопределения нет, и
    для TARGET-поля это означает «наследовать общую настройку» (решение
    принимает вызывающий: routing.py для роутинга, webui_sessions для рендера
    формы).

    Для Log записи нет только у необразованной сессии: ``ensure_session``
    заполняет его снимком общего флага, и дальше значение всегда лежит в
    таблице (в т.ч. после clear — см. ``set_config``)."""
    if not session_id:
        return None
    with _LOCK:
        row = _OVERRIDES.get(session_id)
        if not row:
            return None
        return row.get(name)


def effective(session_id: str, name: str) -> Any:
    """Действующее значение настройки для сессии.

    Две модели (v0.9.8):

    * Log (``_SNAPSHOT_NAMES``) — СНИМОК: значение сессии, взятое при
      её образовании (``ensure_session``). Живое чтение ``config`` осталось
      только страховкой для необразованной сессии (пустой session_id,
      прямые вызовы в тестах) — в рабочем тракте оно недостижимо.
    * TARGET-поля — ЖИВОЕ наследование: переопределение (если задано), иначе
      общая настройка приложения ``config.X`` (как routing.target_for_input):
      смена общей настройки через /config сразу видна всем сессиям, которые
      её наследуют."""
    value = override(session_id, name)
    if name in _SNAPSHOT_NAMES:
        if value is None:
            return bool(getattr(config, name, False))
        return bool(value)
    if value is None:
        return getattr(config, name)
    return value


def session_overrides(session_id: str) -> dict[str, Any]:
    """Копия переопределений сессии (пустой dict, если их нет)."""
    if not session_id:
        return {}
    with _LOCK:
        return dict(_OVERRIDES.get(session_id) or {})


def set_config(
    session_id: str,
    values: dict[str, Any] | None = None,
    clear: tuple[str, ...] | list[str] = (),
) -> dict[str, Any] | None:
    """Применить переопределения сессии.

    ``values`` — {имя_настройки: значение}; ключи вне
    ``config.SESSION_CONFIG_POOL`` и значения, не проходящие валидацию
    ``config.accepts_value`` по ``config._SESSION_CONFIG_TYPES``, ИГНОРИРУЮТСЯ
    МОЛЧА (та же политика, что у ``config.set_runtime_config``: вызывающий
    сверяет ответ с тем, что послал, и сам решает, как сообщить об этом).
    ``clear`` — имена настроек, у которых переопределение снимается.
    Возвращает КОПИЮ переопределений сессии ПОСЛЕ применения либо None, если
    ``session_id`` пуст (переопределять нечего — сессия не идентифицирована).

    Снятие различается по модели наследования (v0.9.8): у TARGET-полей
    запись удаляется и сессия снова ЖИВО наследует общую настройку; у Log
    удалять нечего — его значение всегда лежит в таблице, поэтому ``clear``
    возвращает СНИМОК общего тумблера на текущий момент (сессия
    переинициализируется, как новая)."""
    if not session_id:
        return None
    # Сессия обязана быть образованной ДО записи: у Log не должно
    # оставаться состояния «записи нет» — иначе действующее значение снова
    # читалось бы из config живьём (см. effective).
    ensure_session(session_id)
    with _LOCK:
        row = _OVERRIDES.setdefault(session_id, {})
        for name in clear:
            if name not in config.SESSION_CONFIG_POOL:
                continue
            if name in _SNAPSHOT_NAMES:
                # «Сбросить» у снимка = переинициализировать из общего тумблера
                # (сессия как новая). Живой связи с config у него нет.
                row[name] = _snapshot_values()[name]
            else:
                row.pop(name, None)
        for name, value in (values or {}).items():
            if name not in config.SESSION_CONFIG_POOL:
                continue
            expected = config._SESSION_CONFIG_TYPES[name]
            if not config.accepts_value(expected, value):
                continue
            row[name] = value
        if not row:
            _OVERRIDES.pop(session_id, None)
        return dict(row)


def set_model_override(session_id: str, model_name: str) -> None:
    """Задаёт активную модель сессии (команда "/model <имя>", v0.9.6) —
    действует для ВСЕХ последующих запросов этой сессии, пока не будет
    переключена снова (новый вызов) или снята (clear_session). В отличие
    от set_config, значение не валидируется доменом
    config._SESSION_CONFIG_TYPES — это простое имя модели, а не
    enum/bool-настройка; проверка допустимости имени (ADAPTER_STRICT_MODELS
    / _AVAILABLE_MODELS) остаётся на стороне server.py, как и для модели,
    присланной клиентом напрямую в теле запроса. No-op при пустом
    ``session_id`` (переопределять нечего — сессия не идентифицирована)."""
    if not session_id:
        return
    with _LOCK:
        _OVERRIDES.setdefault(session_id, {})[_MODEL_OVERRIDE_KEY] = model_name


def model_override(session_id: str) -> str | None:
    """Активная модель сессии, заданная set_model_override, либо None —
    переопределения нет (сессия использует модель, присланную клиентом в
    теле запроса, без изменений)."""
    if not session_id:
        return None
    with _LOCK:
        row = _OVERRIDES.get(session_id)
        return row.get(_MODEL_OVERRIDE_KEY) if row else None


def clear_session(session_id: str) -> bool:
    """Снять ВСЕ переопределения сессии. True, если что-то было снято.

    Log не удаляется вместе с остальными: у образованной сессии он не
    может остаться «не заданным» — иначе действующее значение снова начало
    бы читаться из ``config`` живьём, а глобальный тумблер обязан влиять
    только на НОВЫЕ сессии (v0.9.8). Поэтому сессии с уже взятым снимком
    переинициализируются текущим общим флагом (как новая), а всё прочее
    (TARGET-переопределения, служебный ключ модели) снимается.

    Исключений не бросает — вызывающий (эндпойнт) сам решает, что ответить."""
    try:
        if not session_id:
            return False
        with _LOCK:
            row = _OVERRIDES.get(session_id)
            if row is None:
                return False
            if session_id in _SEEDED:
                row.clear()
                row.update(_snapshot_values())
                return True
            _OVERRIDES.pop(session_id, None)
            return True
    except Exception:
        return False
