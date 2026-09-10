#!/usr/bin/env python3
"""session_registry.py — реестр активных сессий агентов (v0.9.2).

Что делает: фиксирует СООТВЕТСТВИЕ между клиентской сессией
(``X-Claude-Code-Session-Id``) и обработчиком её трафика — имя агента
(``User-Agent``), клиентскую модель, бэкенд и выбранное правило
TARGET-маршрутизации, — дополняя его счётчиками обращений/ошибок и временем
последнего обращения. Состояние выводится секцией «Sessions» на
статус-странице WEBUI "/" ниже таблицы «Models in use» (см.
webui_status._sessions_rows_html).

**Строка = кортеж (session, agent, model, backend, route).** Ключ таблицы —
все пять колонок сразу: как только агент переключается на новую модель или
для входного эндпоинта меняется обработчик (правило TARGET-маршрутизации) —
это НОВОЕ событие и НОВАЯ строка таблицы; прежняя строка остаётся как есть.
Возврат к уже использованному кортежу (``m1 → m2 → m1``) новой строки НЕ
создаёт: строка того же кортежа просто получает ``calls+1`` и свежее
``last_seen``. Три последние колонки (last_seen, calls, errors) — не часть
ключа, а сопровождение уже описанного кортежа.

Строка таблицы (ключ — кортеж (session, agent, model, backend, route)):

    {"session": str,     # session_id как пришёл в заголовке
     "agent": str,       # User-Agent до первого пробела (claude-cli/2.1.236)
     "model": str,       # клиентское имя модели этого обращения
     "backend": str,     # имя бэкенда из config._resolve_backend
     "route": str,       # "passthrough messages→messages" / "convert …" /
                         #   "reject" / "disabled" (правило роутинга)
     "last_seen": str,   # "YYYY-MM-DD HH:MM:SS" последнего обращения кортежа
     "calls": int,       # счётчик обращений ЭТОГО кортежа
     "errors": int,      # счётчик финальных HTTP-ответов >= 400 по кортежу
     "_ts": float}       # time.time() последнего обращения — служебное поле
                         #   для сортировки/эвикции, в snapshot НЕ отдаётся

Точки учёта — server.do_POST, единственная функция ``register`` на каждый
запрос (ровно одна строка на запрос):

- ``register(session_id, agent, model=…, backend=…, route=…)`` — после
  ``routing.decide``: реальные model/backend/route (включая reject/disabled —
  таблица показывает, куда агент пытался);
- те же ``register`` с ПУСТЫМИ model/backend/route — в 400-ветках валидации
  тела (Invalid JSON / Missing model / strict-модель), которые срабатывают ДО
  ``routing.decide``: запрос не дошёл до выбора обработчика, но обращение
  учтено (пустой кортеж);
- 404 на не-входной путь строку НЕ создаёт — учёт стоит после ``return``;
- ``record_error(key)`` — из общих _send_json/_send_raw по финальному статусу
  >= 400. **No-op для None/неизвестного ключа** — так служебные ответы
  (404 не-входных путей, GET /api/*, health) в счётчик не попадают, хотя
  перехват стоит в общих методах отправки.

Порядок строк: ``sessions_snapshot()`` сортирует по ``_ts`` desc — новый
кортеж сверху, старая строка с новым обращением всплывает наверх. Каждая
строка снимка несёт поле ``key`` (см. ``key_json``) — компактную JSON-строку
кортежа: одна сессия может занимать несколько строк, поэтому ``session`` уже
не уникален и JS-матчинг строк идёт по ``key``. Глубина таблицы ограничена
живым лимитом ``config.ADAPTER_SESSIONS_TABLE`` (дефолт 10; 0 — таблица
отключена): лимит считает СТРОКИ (пары сессия+обработчик), а не сессии; при
регистрации обращения лишняя (самая старая) строка вытесняется.

Модуль — лист DAG: на верхнем уровне импортирует только ``backend_adapter.
config`` (корень DAG). Потребители — server.py (пишет) и webui_status.py
(читает/рендерит + JSON-эндпойнт).
"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from . import config

# Ключ строки — кортеж первых пяти колонок: (session, agent, model, backend,
# route). Все поля — str, поэтому кортеж хешируем и годится ключом dict.
SessionKey = tuple[str, str, str, str, str]

# Реестр сессий: кортеж-ключ → строка (схема — в docstring модуля).
_TABLE: dict[SessionKey, dict[str, Any]] = {}
_TABLE_LOCK = threading.Lock()

__all__ = [
    "SessionKey",
    "key_json",
    "register",
    "record_error",
    "sessions_snapshot",
]


def _limit() -> int:
    """Живой лимит глубины таблицы (config.ADAPTER_SESSIONS_TABLE).

    Читается через атрибут модуля config (а не снимок импорта) — как
    routing.target_for_input: переживает reload конфига в тестах. Значение
    <= 0 означает «таблица отключена» (ничего не храним)."""
    return int(getattr(config, "ADAPTER_SESSIONS_TABLE", 10))


def key_json(key: SessionKey) -> str:
    """Компактная JSON-строка ключа — стабильный дискриминатор строки.

    Одна и та же строка уходит и в атрибут ``data-key`` HTML-строки таблицы,
    и в поле ``"key"`` снимка ``sessions_snapshot()``: JS сопоставляет строки
    точным равенством строк, не разбирая кортеж. Служебное поле — как
    ``cost_html`` у ``/api/model-usage/snapshot``: клиент может игнорировать.
    """
    return json.dumps(list(key), ensure_ascii=False, separators=(",", ":"))


def register(
    session_id: str,
    agent: str,
    *,
    model: str,
    backend: str,
    route: str,
) -> SessionKey | None:
    """Регистрирует обращение по ПОЛНОМУ ключу-кортежу: создаёт строку либо
    инкрементит ``calls`` и обновляет ``last_seen``/``_ts``. Затем вытесняет
    самую старую строку, если таблица стала глубже лимита.

    Возвращает ключ (сервер кладёт его в ``_req_ctx.session_key``, чтобы
    ``record_error`` попал в ту же строку) либо None, если учёт не ведётся:
    пустой ``session_id``, выключенный лимит или внутренняя ошибка. Не бросает
    исключений наружу — учёт не должен влиять на запрос.

    ``model``/``backend``/``route`` — обязательные keyword-only: 400-ветки до
    ``routing.decide`` передают их явно пустыми (обработчик ещё не выбран),
    что самодокументирует «недостроенный» кортеж."""
    try:
        if not session_id:
            return None
        limit = _limit()
        if limit <= 0:
            return None
        now = time.time()
        last_seen = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
        key: SessionKey = (session_id, agent, model, backend, route)
        with _TABLE_LOCK:
            row = _TABLE.get(key)
            if row is None:
                _TABLE[key] = {
                    "session": session_id,
                    "agent": agent,
                    "model": model,
                    "backend": backend,
                    "route": route,
                    "last_seen": last_seen,
                    "calls": 1,
                    "errors": 0,
                    "_ts": now,
                }
            else:
                row["last_seen"] = last_seen
                row["calls"] += 1
                row["_ts"] = now
            # Эвикция: держим не больше limit строк (вытесняем самые старые).
            while len(_TABLE) > limit:
                oldest = min(_TABLE, key=lambda k: _TABLE[k]["_ts"])
                del _TABLE[oldest]
        return key
    except Exception:
        # Учёт не должен ронять запрос (как model_usage.record_model_usage).
        return None


def record_error(key: SessionKey | None) -> None:
    """Инкрементит счётчик ошибок строки-кортежа (финальный HTTP-ответ
    >= 400). No-op для None/неизвестного ключа — так служебные ответы
    (404 не-входных путей, GET /api/*, health) в счётчик не попадают, хотя
    вызов стоит в общих методах отправки. Не бросает исключений наружу."""
    try:
        if key is None:
            return
        with _TABLE_LOCK:
            row = _TABLE.get(key)
            if row is None:
                return
            row["errors"] += 1
    except Exception:
        pass


def sessions_snapshot() -> list[dict[str, Any]]:
    """Копии строк реестра, отсортированные по времени последнего обращения
    (новые сверху), без служебного поля ``_ts`` и с полем ``"key"`` —
    JSON-строкой кортежа (дискриминатор строки для JS; см. ``key_json``).
    Публичный геттер для WEBUI.

    Сортировка — по ``_ts`` (float) ДО удаления служебного поля: две строки
    с одинаковой секундой в ``last_seen`` иначе получили бы нестабильный
    порядок."""
    with _TABLE_LOCK:
        ordered = sorted(_TABLE.items(), key=lambda kv: kv[1]["_ts"], reverse=True)
        return [
            {"key": key_json(key), **{k: v for k, v in row.items() if k != "_ts"}}
            for key, row in ordered
        ]
