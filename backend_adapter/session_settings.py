"""session_settings.py — пер-сессионные переопределения настроек (v0.9.5).

Зачем модуль: до v0.9.5 «сессия агента» была единицей НАБЛЮДЕНИЯ (таблица
Sessions WEBUI, см. session_registry) — адаптер умел показать, чем трафик
одной сессии отличается от другой, но не умел этой разницей УПРАВЛЯТЬ.
Теперь сессия — ещё и единица управления: набор настроек, переопределяемых
для одного ``session_id`` поверх общих настроек приложения.

Пул переопределяемого — ``config.SESSION_CONFIG_POOL`` (логирование +
TARGET-роутинг входов), типы и домены — ``config._SESSION_CONFIG_TYPES``.
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

__all__ = [
    "reset",
    "override",
    "effective",
    "session_overrides",
    "set_config",
    "clear_session",
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
    нечего — сессия не идентифицирована)."""
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
        if not row:
            _OVERRIDES.pop(session_id, None)
        return dict(row)


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
