#!/usr/bin/env python3
"""prometheus_exporter.py — Prometheus-экспортёр backend-adapter (v0.8.5).

Отдельный лёгкий HTTP-слушатель на ADAPTER_EXPORTER_PORT (дефолт 9100) и
адресе ADAPTER_WEBUI_HOST, поднимаемый вместе с WEBUI при
ADAPTER_EXPORTER_ENABLE=1 (см. блок в backend-adapter.py). Отдаёт метрики
в тексте text exposition format 0.0.4 (GET /metrics; GET / — то же самое)
БЕЗ внешних библиотек — только stdlib.

СВОЙ слушатель, а не эндпоинт общего WEBUI-сервера (webserver): повторный
serve() перезаписал бы Handler.context/endpoints и открыл бы весь WEBUI на
порту экспортёра, а /metrics не должен висеть на порту статуса. Роутинг
минимален — только путь / (или /metrics), всё прочее — 404.

Источники — живые конфиг-глобалы, те же, что у статус-страницы "/"
(см. webui_status._config_snapshot/_usage_rows_html):
  - настройки/статус приложения: версия (передаётся в serve_exporter из
    backend-adapter.py __version__), uptime процесса (модульная константа
    _BOOT_TS при импорте — как _BOOT_TS в webui_ops), число настроенных
    бэкендов (len config._BACKENDS) и моделей в кэше (len
    config._AVAILABLE_MODELS);
  - по бэкенду: backend_up (1/0 — есть ли ошибка бэкенда в снимке
    config.refresh_state()["errors"], т.е. не смог отдать /v1/models),
    число моделей бэкенда (из config._MODEL_TO_BACKEND), доступность
    API-эндпоинтов (backend_adapter_backend_endpoint{endpoint=...} 1/0 по
    config._ENDPOINT_STATE — единый источник после синхронизации
    found-эндпоинтов модели, см. model_usage._sync_found_endpoints);
  - по использованной модели: счётчики calls/input_tokens/output_tokens из
    model_usage.usage_snapshot() (уже копирует таблицу под _TABLE_LOCK).

Чтение глобалов — напрямую (как рендер страницы; refresh_errors читается
через config.refresh_state() — снимок без лока). Имена label-значений
(бэкендов/моделей) экранируются по правилам text format (\\, ", перевод
строки); значения int/float не экранируются.
"""

import http.server
import logging
import time
import urllib.parse

from . import config, model_usage

logger = logging.getLogger("prometheus_exporter")

# Момент импорта модуля: uptime процесса. Импорт случается при старте
# адаптера (подъём экспортёра в daemon-потоке) — как _BOOT_TS в webui_ops.
_BOOT_TS = time.time()

_METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _label_escape(value: str) -> str:
    """Экранировать label-значение text exposition: \\, " и перевод строки.

    Имена бэкендов/моделей приходят из YAML/запросов и теоретически могут
    содержать кавычки — без экранирования строка метрики была бы битой."""
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _emit(lines: list[str], name: str, help_text: str, typ: str, samples: list[str]) -> None:
    """Блок одной метрики: # HELP + # TYPE + строки-семплы."""
    lines.append(f"# HELP {name} {help_text}")
    lines.append(f"# TYPE {name} {typ}")
    lines.extend(samples)


# ==================== РЕНДЕР ====================


def _render_metrics() -> str:
    """Текст text exposition 0.0.4 из живых конфиг-глобалов (без сети).

    version — из классового атрибута MetricsHandler.version (устанавливается
    serve_exporter из __version__ backend-adapter.py; standalone/тесты — как
    задано). Порядок групп фиксирован: приложение → бэкенды (по имени) →
    модели (в порядке usage_snapshot). Возвращает текст с завершающим \n."""
    lines: list[str] = []
    version = _label_escape(MetricsHandler.version)

    _emit(
        lines,
        "backend_adapter_info",
        "Backend-Adapter version.",
        "gauge",
        [f'backend_adapter_info{{version="{version}"}} 1'],
    )
    _emit(
        lines,
        "backend_adapter_build_info",
        "Build information (version label).",
        "gauge",
        [f'backend_adapter_build_info{{version="{version}"}} 1'],
    )
    uptime = max(0.0, time.time() - _BOOT_TS)
    _emit(
        lines,
        "backend_adapter_uptime_seconds",
        "Process uptime (seconds since exporter module import).",
        "gauge",
        [f"backend_adapter_uptime_seconds {uptime:.1f}"],
    )
    _emit(
        lines,
        "backend_adapter_backends_configured",
        "Number of configured backends (config._BACKENDS).",
        "gauge",
        [f"backend_adapter_backends_configured {len(config._BACKENDS)}"],
    )
    _emit(
        lines,
        "backend_adapter_models_available",
        "Number of models in the available-models cache (config._AVAILABLE_MODELS).",
        "gauge",
        [f"backend_adapter_models_available {len(config._AVAILABLE_MODELS)}"],
    )

    lines.extend(_backend_metric_lines())
    lines.extend(_model_metric_lines())
    return "\n".join(lines) + "\n"


def _backend_metric_lines() -> list[str]:
    """Строки метрик по настроенным бэкендам (config._BACKENDS, по имени).

    - backend_up: 1 — бэкенда нет в errors снимка config.refresh_state()
      (последний опрос /v1/models прошёл или проверок ещё не было); 0 — текст
      ошибки в снимке (как строка «недоступен» статус-страницы);
    - backend_models: число моделей бэкенда в config._MODEL_TO_BACKEND;
    - backend_endpoint{endpoint=pname}: 1/0 — found эндпоинта в
      config._ENDPOINT_STATE[бэкенд] (единый источник: сюда же
      синхронизируются found-эндпоинты проб модели). Не пробованный путь → 0."""
    lines: list[str] = []
    state = config._ENDPOINT_STATE
    refresh_errors = config.refresh_state().get("errors") or {}

    for b in sorted(config._BACKENDS, key=lambda x: x["name"]):
        name = _label_escape(b["name"])
        base = _label_escape(b.get("base", ""))
        labels = f'{{name="{name}",base="{base}"}}'

        _emit(
            lines,
            "backend_adapter_backend_up",
            "Whether the last models probe of the backend succeeded (1/0).",
            "gauge",
            [f"backend_adapter_backend_up{labels} {0 if b['name'] in refresh_errors else 1}"],
        )
        backend_models = sum(
            1 for mid, (bname, _bcfg) in config._MODEL_TO_BACKEND.items() if bname == b["name"]
        )
        _emit(
            lines,
            "backend_adapter_backend_models",
            "Number of models of the backend in the available-models cache.",
            "gauge",
            [f"backend_adapter_backend_models{labels} {backend_models}"],
        )

        ep_state = (state.get(b["name"]) or {}).get("endpoints", {})
        samples = []
        for pname, path, _tpl in config.ENDPOINT_PROBES:
            entry = ep_state.get(path) or {}
            found = 1 if entry.get("found") else 0
            samples.append(
                f'backend_adapter_backend_endpoint{{name="{name}",'
                f'base="{base}",endpoint="{pname}"}} {found}'
            )
        _emit(
            lines,
            "backend_adapter_backend_endpoint",
            "Whether the backend endpoint answered HTTP 200 to the smoke probe (1/0).",
            "gauge",
            samples,
        )
    return lines


def _model_metric_lines() -> list[str]:
    """Строки счётчиков по использованным моделям (model_usage.usage_snapshot).

    По строке таблицы Models in use: calls/input_tokens/output_tokens —
    счётчики-канторы с label model (клиентское имя) и backend."""
    lines: list[str] = []
    rows = model_usage.usage_snapshot()
    if not rows:
        return lines
    for r in rows:
        model = _label_escape(r.get("model", ""))
        backend = _label_escape(r.get("backend", ""))
        labels = f'{{model="{model}",backend="{backend}"}}'
        _emit(
            lines,
            "backend_adapter_model_calls_total",
            "Total accepted requests per used model.",
            "counter",
            [f"backend_adapter_model_calls_total{labels} {r.get('calls', 0)}"],
        )
        _emit(
            lines,
            "backend_adapter_model_input_tokens_total",
            "Total input tokens (usage.prompt_tokens) per used model.",
            "counter",
            [f"backend_adapter_model_input_tokens_total{labels} {r.get('input_tokens', 0)}"],
        )
        _emit(
            lines,
            "backend_adapter_model_output_tokens_total",
            "Total output tokens (usage.completion_tokens) per used model.",
            "counter",
            [f"backend_adapter_model_output_tokens_total{labels} {r.get('output_tokens', 0)}"],
        )
    return lines


# ==================== HTTP-СЛУШАТЕЛЬ ====================


class MetricsHandler(http.server.BaseHTTPRequestHandler):
    """Обработчик /metrics на отдельном слушателе экспортёра.

    Только GET: пути "/" и "/metrics" → 200 text/plain (version=0.0.4) с
    _render_metrics(); прочие пути — 404; не-GET методы — 501 дефолтом
    BaseHTTPRequestHandler. log_message подавлен — экспортёр молчит (как
    QuietWebServer/QuietThreadingHTTPServer)."""

    version = "unknown"  # классовый атрибут — выставляется serve_exporter

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path not in ("/", "/metrics"):
            self.send_error(404, "Not found")
            return
        body = _render_metrics().encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", _METRICS_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve_exporter(version: str, host: str = "127.0.0.1", port: int = 9100, verbose: bool = False):
    """Поднять экспортёр в ДАННОМ процессе (не fork) и вернуть httpd.

    Устанавливает MetricsHandler.version (источник label-версии) и поднимает
    ThreadingHTTPServer на (host, port). Возвращает инстанс сервера — звать
    serve_forever() (например, в daemon-потоке, как делает backend-adapter.py);
    None — порт занят/адрес недоступен (OSError): экспортёр не должен ронять
    адаптер, вызывающий печатает и продолжает."""
    MetricsHandler.version = version
    try:
        httpd = http.server.ThreadingHTTPServer((host, port), MetricsHandler)
    except OSError as e:
        print(f"[EXPORTER] Failed to bind {host}:{port}: {e}")
        return None
    httpd.daemon_threads = True
    msg = f"[EXPORTER] http://{host}:{httpd.server_port}/metrics"
    if verbose:
        logger.info(msg)
    else:
        logger.debug(msg)
    return httpd


__all__ = [
    "MetricsHandler",
    "serve_exporter",
    "_render_metrics",
    "_backend_metric_lines",
    "_model_metric_lines",
    "_label_escape",
    "_BOOT_TS",
    "_METRICS_CONTENT_TYPE",
]
