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

  - **Логирование (Log/Parts) — СНИМОК.** В момент образования сессии
    (``ensure_session``, первое обращение к ``session_id``) значения общих
    тумблеров ``config.ADAPTER_DEBUG``/``ADAPTER_DEBUG_PARTS`` КОПИРУЮТСЯ в
    переопределения сессии. Дальше сессия живёт своими значениями, а общие
    тумблеры служат лишь шаблоном для НОВЫХ сессий: их последующая смена уже
    существующие сессии не трогает. Иначе говоря, глобальные флаги ничего не
    включают и не выключают — они только инициализируют (v0.9.8). Состояния
    ``"inherit"`` у этих полей нет: домен — bool, а «вернуться к общему»
    выражается повторной инициализацией из текущего общего значения
    (``set_config(..., clear=...)``) — тоже снимок, а не живая связь. В WEBUI
    у селектов Log/Parts ровно два положения: on/off.
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
# инициализируются из общих тумблеров в момент образования сессии
# (ensure_session) и дальше живут независимо от config. У них нет состояния
# "inherit", а «сбросить» означает «взять общий тумблер заново» (свежий
# снимок), а не «живую связь».
_SNAPSHOT_NAMES = ("ADAPTER_DEBUG", "ADAPTER_DEBUG_PARTS")

# session_id, для которых снимок уже взят. Отдельно от _OVERRIDES, потому что
# запись может быть снята (clear_session) — повторный снимок делать нельзя:
# сессия уже существует, и её флаги не должны «подхватывать» более позднее
# изменение общих тумблеров.
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
    """Снимок общих флагов логирования для сессии (v0.9.8).

    Единственное место, где глобальные ``config.ADAPTER_DEBUG``/
    ``ADAPTER_DEBUG_PARTS`` попадают в сессию. Снимок — ТОЧНАЯ копия пары:
    согласованность «Parts ⊆ Log» обеспечивает не он, а гейт
    ``session_log.parts_enabled`` (Parts без Log неактивен у любого
    потребителя). Зато поведение новой сессии предсказуемо повторяет общие
    флаги как есть — включая env-пару PARTS=1/DEBUG=0 (её выровнял бы каскад
    ``config.set_runtime_config``, но env он не трогает)."""
    return {name: bool(getattr(config, name, False)) for name in _SNAPSHOT_NAMES}


def ensure_session(session_id: str) -> bool:
    """Образовать сессию: скопировать в неё текущие общие флаги логирования.

    Вызывается на каждом обращении к ``session_id`` (идемпотентно, дёшево:
    проверка множества под блокировкой), но снимок берётся РОВНО ОДИН РАЗ —
    при первой встрече. С этого момента сессия живёт своими флагами Log/Parts,
    а общие тумблеры служат лишь шаблоном для НОВЫХ сессий: их последующая
    смена через /config уже существующие сессии не трогает (v0.9.8,
    требование «глобальные флаги не включают и не выключают функционал»).

    Заполняются только ОТСУТСТВУЮЩИЕ ключи: если сессия успела задать Log/Parts
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

    Для Log/Parts записи нет только у необразованной сессии: ``ensure_session``
    заполняет их снимком общих флагов, и дальше значение всегда лежит в
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

    * Log/Parts (``_SNAPSHOT_NAMES``) — СНИМОК: значение сессии, взятое при
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
    запись удаляется и сессия снова ЖИВО наследует общую настройку; у
    Log/Parts удалять нечего — их значение всегда лежит в таблице, поэтому
    ``clear`` возвращает СНИМОК общих тумблеров на текущий момент (сессия
    переинициализируется, как новая).

    Каскад Log/Parts (v0.9.6) — тот же, что в ``config.set_runtime_config``,
    но по ДЕЙСТВУЮЩИМ значениям сессии: сессия не может иметь Parts без Log.
    Направление выбирается по явному намерению Parts
    (``values[ADAPTER_DEBUG_PARTS]``): ``True`` → включается Log, иначе при
    Log=off Parts гасится."""
    if not session_id:
        return None
    # Сессия обязана быть образованной ДО записи: у Log/Parts не должно
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
        _cascade_log_parts(row, values or {})
        if not row:
            _OVERRIDES.pop(session_id, None)
        return dict(row)


def _cascade_log_parts(row: dict, values: dict) -> None:
    """Каскад Log/Parts пер-сессионных значений (v0.9.6, снимок — v0.9.8).

    ``row`` — значения сессии (мутируется на месте), ``values`` — значения
    текущего вызова. Действующие значения считаются как ``effective``:
    запись сессии, а для необразованной сессии — общий тумблер (страховка).
    Вызывается ПОД ``_LOCK`` (из set_config), поэтому читает ``row`` напрямую,
    без повторного взятия блокировки."""
    debug = row.get("ADAPTER_DEBUG", config.ADAPTER_DEBUG)
    parts = row.get("ADAPTER_DEBUG_PARTS", config.ADAPTER_DEBUG_PARTS)
    if not (parts and not debug):
        return
    if values.get("ADAPTER_DEBUG_PARTS") is True:
        # Явное намерение Parts=on: включаем Log (Parts без Log невозможен).
        row["ADAPTER_DEBUG"] = True
    else:
        # Log выключен — Parts не может быть активен: гасим его явно.
        row["ADAPTER_DEBUG_PARTS"] = False


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

    Log/Parts не удаляются вместе с остальными: у образованной сессии они не
    могут остаться «не заданными» — иначе действующее значение снова начало
    бы читаться из ``config`` живьём, а глобальные тумблеры обязаны влиять
    только на НОВЫЕ сессии (v0.9.8). Поэтому сессии с уже взятым снимком
    переинициализируются текущими общими флагами (как новая), а всё прочее
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
