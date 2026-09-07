#!/usr/bin/env python3
"""
webui_ops.py — операционные (health/liveness/readiness) эндпойнты WEBUI.

Контракт — k8s-схема зондов на ОБЩЕМ слушателе WEBUI (тот же сервер, что
отдаёт "/", "/session", "/config"; поднимается при ADAPTER_WEBUI_ENABLE=1):

  GET /healthz  — живость процесса и сервера (алиас — GET /health).
                 Всегда 200 application/json:
                 {"status": "ok", "version": "<WebContext.version>",
                  "uptime": <сек>, "pid": <os.getpid()>}
  GET /live     — то же (liveness: процесс жив, сервер отвечает).
  GET /ready    — готовность принимать трафик: 200 {"status": "ready", ...},
                 если настроен хотя бы один бэкенд (config._BACKENDS непуст)
                 И стартовый/последний опрос дал непустой кэш моделей
                 (config._AVAILABLE_MODELS непуст) — «бэкенды настроены и
                 прогреты». Иначе 503 {"status": "not_ready", "reason": ...}
                 с различием причины (нет настроенных бэкендов / кэш
                 моделей пуст). 503 отдаётся ТЕЛОМ (не send_error), чтобы
                 у зонда был JSON c Content-Type.

Зонды не делают сетевых запросов к бэкендам и не трогают кэши/состояние
проверок: это чистые наблюдательные GET. Имена /healthz + алиас /health
зафиксированы пользователем (v0.8.5); /leave не добавлялся.

Матчинг префиксов (webserver.Handler._match по самому длинному префиксу)
не конфликтует: "/health" матчит только "/health" (+ "/health/..."), а
"/healthz" — только "/healthz" (+ "/healthz/...").
"""

import json
import os
import time

from . import config, webserver

# Момент импорта модуля: uptime = time.time() - _BOOT_TS. Константа модуля —
# health отвечает «с какого времени жив ПРОЦЕСС» (импорт случается при
# старте WEBUI-сервера, в том же процессе адаптера).
_BOOT_TS = time.time()

_HEALTH_CONTENT_TYPE = "application/json; charset=utf-8"


def _health_body(context) -> bytes:
    """JSON-тело живости/готовности: версия из WebContext, uptime, pid.

    Единый формат для /healthz, /health, /live и /ready (у /ready — поля
    status/reason вместо ok). version — WebContext.version (в адаптере —
    __version__ из backend-adapter.py, единственный источник)."""
    return json.dumps(
        {
            "status": "ok",
            "version": getattr(context, "version", "unknown"),
            "uptime": round(time.time() - _BOOT_TS, 1),
            "pid": os.getpid(),
        },
        ensure_ascii=False,
    ).encode("utf-8")


def _ready_body(context) -> tuple[int, bytes]:
    """Тело /ready: (200, тело) если бэкенды настроены и прогреты; иначе
    (503, тело not_ready с причиной). Сети нет — только текущие кэши."""
    base = json.loads(_health_body(context))
    reason = None
    if not config._BACKENDS:
        reason = "no backends configured"
    elif not config._AVAILABLE_MODELS:
        reason = "models cache empty (backend probe never succeeded)"
    if reason is not None:
        base.update({"status": "not_ready", "reason": reason})
        return 503, json.dumps(base, ensure_ascii=False).encode("utf-8")
    base["status"] = "ready"
    return 200, json.dumps(base, ensure_ascii=False).encode("utf-8")


def _handle_health(handler, remainder: str) -> None:
    """Общий GET-обработчик живости (/healthz, /health, /live).

    Пустой remainder → 200 JSON; непустой (вложенный путь) → 404. Логика
    вынесена в функцию, чтобы классы-эндпойнты оставались тонкими
    алиасами (health-ответ един для всех трёх префиксов)."""
    if remainder:
        handler.send_error(404, "Not found")
        return
    handler._write(200, _HEALTH_CONTENT_TYPE, _health_body(handler.context))


@webserver.register
class HealthzEndpoint(webserver.Endpoint):
    """Эндпойнт "/healthz": живость процесса/сервера (k8s liveness).

    GET без remainder → 200 application/json: status/version/uptime/pid.
    Не-GET метод — 405 (дефолт базового Endpoint)."""

    prefix = "/healthz"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        _handle_health(handler, remainder)


@webserver.register
class HealthEndpoint(webserver.Endpoint):
    """Алиас "/health" эндпоинта "/healthz" (имена зафиксированы: оба
    отдают одинаковый JSON живости). Свой класс — каждый prefix
    регистрируется отдельно в реестре эндпойнтов."""

    prefix = "/health"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        _handle_health(handler, remainder)


@webserver.register
class LiveEndpoint(webserver.Endpoint):
    """Эндпойнт "/live": liveness — процесс жив, сервер отвечает.

    GET без remainder → 200 application/json (тот же health-ответ).
    От /healthz отличается только префиксом (контракт зонда)."""

    prefix = "/live"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        _handle_health(handler, remainder)


@webserver.register
class ReadyEndpoint(webserver.Endpoint):
    """Эндпойнт "/ready": готовность принимать трафик (k8s readiness).

    «Бэкенды настроены и прогреты» ⇔ config._BACKENDS непуст И
    config._AVAILABLE_MODELS непуст (стартовый/последний опрос моделей
    завершился успешно — refresh_models нашёл модели). Иначе 503 с JSON-
    телом {"status": "not_ready", "reason": "..."} (текст различия: нет
    настроенных бэкендов / кэш моделей пуст). 503 отдаётся телом через
    handler._write — у зонда есть JSON и Content-Type."""

    prefix = "/ready"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        if remainder:
            handler.send_error(404, "Not found")
            return
        status, body = _ready_body(handler.context)
        handler._write(status, _HEALTH_CONTENT_TYPE, body)


__all__ = [
    "HealthzEndpoint",
    "HealthEndpoint",
    "LiveEndpoint",
    "ReadyEndpoint",
    "_health_body",
    "_ready_body",
]
