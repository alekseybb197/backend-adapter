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
Настройка живёт в одном из трёх состояний:


  - НЕ ЗАДАНА (записи нет) — действует общая настройка приложения
    (``config.X``): сессия «наследует» её, включая последующие изменения
    общей настройки через /config;
  - ``"inherit"`` (только у TARGET-полей, домен
    ``config.SESSION_TARGET_VALUES``) — переопределение существует, но
    трактуется как «взять общее». Нужно затем, чтобы в WEBUI поле можно
    было вернуть к общему значению, не удаляя запись вовсе: значение
    остаётся видимым и явным;
  - конкретное значение — собственно переопределение.

У логических настроек (ADAPTER_DEBUG/ADAPTER_DEBUG_PARTS) состояния
``"inherit"`` нет: их домен — bool, и «снять переопределение» выражается
удалением записи (``set_config(..., clear=...)``). В WEBUI обе формы
доступны как три положения выпадающего списка inherit/on/off.

Хранение — в памяти процесса, ключ = ``session_id`` (тот же идентификатор,
что в колонке «Сессия» таблицы Sessions, т.е. значение заголовка
ADAPTER_SESSION_HEADER). Не персистентно, как и сама таблица Sessions:
перезапуск адаптера возвращает все сессии к общим настройкам. Записи
переживают вытеснение строки из таблицы Sessions (настройка адресуется
сессии, а не строке-кортежу): вернувшаяся сессия получит свои прежние
переопределения.

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
# (bool или строка из домена); "inherit" хранится как обычное значение.
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

__all__ = [
    "reset",
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


def override(session_id: str, name: str) -> Any | None:
    """Сырое переопределение настройки для сессии (или None, если не задано).

    Возвращается именно ХРАНИМОЕ значение, включая ``"inherit"``: решение
    «трактовать это как общую настройку» принимает вызывающий (routing.py
    для TARGET, webui_sessions для рендера формы)."""
    if not session_id:
        return None
    with _LOCK:
        row = _OVERRIDES.get(session_id)
        if not row:
            return None
        return row.get(name)


def effective(session_id: str, name: str) -> Any:
    """Действующее значение настройки для сессии.

    Переопределение (если задано и это не ``"inherit"``), иначе — общая
    настройка приложения ``config.X``. Живое чтение атрибута config (как
    routing.target_for_input): смена общей настройки через /config сразу
    видна всем сессиям, которые её наследуют."""
    value = override(session_id, name)
    if value is None or value == "inherit":
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
    ``clear`` — имена настроек, у которых переопределение снимается (сессия
    снова наследует общее значение). Возвращает КОПИЮ переопределений сессии
    ПОСЛЕ применения либо None, если ``session_id`` пуст (переопределять
    нечего — сессия не идентифицирована).

    Каскад Log/Parts (v0.9.6) — тот же, что в ``config.set_runtime_config``,
    но по ДЕЙСТВУЮЩИМ значениям сессии (переопределение или общая настройка,
    см. ``effective``): сессия не может иметь Parts без Log. Направление
    выбирается по явному намерению Parts (``values[ADAPTER_DEBUG_PARTS]``):
    ``True`` → включается Log (переопределением), иначе при Log=off Parts
    снимается/гасится (записью ``False`` — наследование общей настройки, под
    которой Parts мог быть включён, недопустимо)."""
    if not session_id:
        return None
    with _LOCK:
        row = _OVERRIDES.setdefault(session_id, {})
        for name in clear:
            if name in config.SESSION_CONFIG_POOL:
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
    """Каскад Log/Parts пер-сессионных переопределений (v0.9.6).

    ``row`` — переопределения сессии (мутируется на месте), ``values`` —
    значения текущего вызова. Действующие значения считаются как
    ``effective``: переопределение, иначе общая настройка ``config.X``.
    Вызывается ПОД ``_LOCK`` (из set_config), поэтому читает ``row`` напрямую,
    без повторного взятия блокировки."""
    debug = row.get("ADAPTER_DEBUG", config.ADAPTER_DEBUG)
    parts = row.get("ADAPTER_DEBUG_PARTS", config.ADAPTER_DEBUG_PARTS)
    if not (parts and not debug):
        return
    if values.get("ADAPTER_DEBUG_PARTS") is True:
        # Явное намерение Parts=on: включаем Log переопределением.
        row["ADAPTER_DEBUG"] = True
    else:
        # Log выключен — Parts не может быть активен: фиксируем off явно
        # (наследование общей настройки с Parts=on недопустимо).
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

    Исключений не бросает — вызывающий (эндпойнт) сам решает, что ответить."""
    try:
        if not session_id:
            return False
        with _LOCK:
            return _OVERRIDES.pop(session_id, None) is not None
    except Exception:
        return False
