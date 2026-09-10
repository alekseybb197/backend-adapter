#!/usr/bin/env python3
"""session_registry.py — реестр активных сессий агентов (v0.9.2).

Что делает: агрегирует по КЛИЕНТСКОЙ СЕССИИ (заголовок
``X-Claude-Code-Session-Id``) то, что разбросано по отдельным запросам и
видно только в логе: имя агента (``User-Agent``), последнюю использованную
модель, бэкенд и выбранное правило TARGET-маршрутизации, а также счётчики
обращений и ошибок и время последнего обращения. Состояние выводится
секцией «Sessions» на статус-странице WEBUI "/" ниже таблицы «Models in
use» (см. webui_status._sessions_rows_html).

**Без персистентности** (осознанное решение пользователя): таблица живёт
только в памяти процесса — каждый запуск адаптера начинает её заново, файла
на диске нет (в отличие от model_usage.py с model-usage.yaml).

Строка таблицы (ключ — session_id):

    {"session": str,     # session_id как пришёл в заголовке
     "agent": str,       # User-Agent до первого пробела (claude-cli/2.1.236)
     "model": str,       # клиентское имя модели последнего запроса
     "backend": str,     # имя бэкенда из config._resolve_backend
     "route": str,       # "passthrough messages→messages" / "convert …" /
                         #   "reject" / "disabled" (правило роутинга)
     "last_seen": str,   # "YYYY-MM-DD HH:MM:SS" последнего обращения
     "calls": int,       # счётчик обращений (растёт на каждый запрос)
     "errors": int,      # счётчик финальных HTTP-ответов >= 400
     "_ts": float}       # time.time() последнего обращения — служебное поле
                         #   для сортировки/эвикции, в snapshot НЕ отдаётся

Точки учёта — server.do_POST:
- ``touch(session_id, agent)`` — сразу после распознавания входного пути
  (только /v1/messages, /v1/chat/completions, /v1/responses): регистрирует
  обращение ДО чтения тела, поэтому 400 «Invalid JSON»/«Missing model»
  тоже учтены. 404 на не-входной путь строку НЕ создаёт.
- ``set_route(...)`` — после routing.decide (включая reject/disabled: таблица
  показывает, куда агент пытался попасть).
- ``record_error(session_id)`` — из общих _send_json/_send_raw по финальному
  статусу >= 400. **No-op для незарегистрированной сессии** — так служебные
  ответы (404 не-входных путей, GET /api/*, health) в счётчик не попадают,
  хотя перехват стоит в общих методах отправки.

Порядок строк: ``sessions_snapshot()`` сортирует по ``_ts`` desc — новая
сессия сверху, старая с новым обращением всплывает наверх. Глубина таблицы
ограничена живым лимитом ``config.ADAPTER_SESSIONS_TABLE`` (дефолт 10; 0 —
таблица отключена): при регистрации обращения лишняя (самая старая) строка
вытесняется.

Модуль — лист DAG: на верхнем уровне импортирует только ``backend_adapter.
config`` (корень DAG). Потребители — server.py (пишет) и webui_status.py
(читает/рендерит + JSON-эндпойнт).
"""

import threading
import time

from . import config

# Реестр сессий: session_id → строка (схема — в docstring модуля).
_TABLE: dict[str, dict] = {}
_TABLE_LOCK = threading.Lock()

__all__ = [
    "touch",
    "set_route",
    "record_error",
    "sessions_snapshot",
]


def _limit() -> int:
    """Живой лимит глубины таблицы (config.ADAPTER_SESSIONS_TABLE).

    Читается через атрибут модуля config (а не снимок импорта) — как
    routing.target_for_input: переживает reload конфига в тестах. Значение
    <= 0 означает «таблица отключена» (ничего не храним)."""
    return int(getattr(config, "ADAPTER_SESSIONS_TABLE", 10))


def touch(session_id: str, agent: str) -> None:
    """Регистрирует обращение сессии: создаёт строку либо обновляет в ней
    agent/last_seen и инкрементит calls. Затем вытесняет самую старую строку,
    если таблица стала глубже лимита. Не бросает исключений наружу — учёт не
    должен влиять на запрос."""
    try:
        if not session_id:
            return
        limit = _limit()
        if limit <= 0:
            return
        now = time.time()
        last_seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        with _TABLE_LOCK:
            row = _TABLE.get(session_id)
            if row is None:
                _TABLE[session_id] = {
                    "session": session_id,
                    "agent": agent,
                    "model": "",
                    "backend": "",
                    "route": "",
                    "last_seen": last_seen,
                    "calls": 1,
                    "errors": 0,
                    "_ts": now,
                }
            else:
                row["agent"] = agent
                row["last_seen"] = last_seen
                row["calls"] += 1
                row["_ts"] = now
            # Эвикция: держим не больше limit строк (вытесняем самые старые).
            while len(_TABLE) > limit:
                oldest = min(_TABLE, key=lambda sid: _TABLE[sid]["_ts"])
                del _TABLE[oldest]
    except Exception:
        # Учёт не должен ронять запрос (как model_usage.record_model_usage).
        pass


def set_route(session_id: str, *, model: str, backend: str, route: str) -> None:
    """Фиксирует последние известные model/backend/route сессии. No-op, если
    строки нет (сессия не зарегистрирована touch — напр. запрос не дошёл до
    входного пути). Не бросает исключений наружу."""
    try:
        with _TABLE_LOCK:
            row = _TABLE.get(session_id)
            if row is None:
                return
            row["model"] = model
            row["backend"] = backend
            row["route"] = route
    except Exception:
        pass


def record_error(session_id: str) -> None:
    """Инкрементит счётчик ошибок сессии (финальный HTTP-ответ >= 400).
    No-op для незарегистрированной сессии — так служебные ответы в счётчик
    не попадают, хотя вызов стоит в общих методах отправки. Не бросает
    исключений наружу."""
    try:
        with _TABLE_LOCK:
            row = _TABLE.get(session_id)
            if row is None:
                return
            row["errors"] += 1
    except Exception:
        pass


def sessions_snapshot() -> list[dict]:
    """Копии строк реестра, отсортированные по времени последнего обращения
    (новые сверху), без служебного поля ``_ts``. Публичный геттер для WEBUI.

    Сортировка — по ``_ts`` (float) ДО удаления служебного поля: две сессии
    с одинаковой секундой в ``last_seen`` иначе получили бы нестабильный
    порядок."""
    with _TABLE_LOCK:
        ordered = sorted(_TABLE.values(), key=lambda r: r["_ts"], reverse=True)
        return [{k: v for k, v in row.items() if k != "_ts"} for row in ordered]
