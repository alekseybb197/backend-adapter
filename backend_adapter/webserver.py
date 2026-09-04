#!/usr/bin/env python3
"""
webserver.py — общее ядро локального веб-сервера backend-adapter
(WEBUI просмотра сессий и статуса).

ЗАЧЕМ ЯДРО: встроенный веб-сервер раньше жил в session_viewer.py целиком
(CLI + serve() + обработчик + рендеры в одном файле), и каждый новый
эндпойнт требовал правки этого растущего файла. Теперь файл разделён так:

    webserver.py       — общее ядро: реестр эндпойнтов, единый Handler-
                         диспетчер, serve(), CLI;
    session_viewer.py  — эндпойнт "/session": просмотр *.parts сессий
                         (вкладки + раздача файлов);
    webui_status.py    — эндпойнт "/": статус-страница (версия кода,
                         эндпойнты LLM, модели).

ДОБАВЛЕНИЕ НОВОГО ЭНДПОЙНТА: создать новый модуль, объявить в нём класс
``class XxxEndpoint(webserver.Endpoint)`` с ``prefix`` и методами GET/POST,
пометить ``@webserver.register`` и дописать ОДНУ строку импорта этого модуля
внутри ``serve()``. Больше ничего править не нужно — роутинг, раздача,
обработка 404/405 и контекст (root_dir/version) достаются автоматически.
(Импорт внутри serve(), а не на верхнем уровне модуля, — чтобы эндпойнт-
модули могли импортировать webserver без циклической зависимости.)

CLI (ручной запуск вне адаптера):
    python3 -m backend_adapter.webserver [ROOT_DIR] [--port 8765] [--host 127.0.0.1]
Встроенный запуск — из backend-adapter.py при ADAPTER_WEBUI_ENABLE=1
(daemon-поток процесса адаптера), через serve().
"""

import argparse
import contextlib
import http.server
import logging
import os
import re
import sys
import urllib.parse

logger = logging.getLogger("webserver")

# ==================== РЕЕСТР ЭНДПОЙНТОВ ====================


class Endpoint:
    """Базовый класс эндпойнта веб-сервера.

    Атрибут класса ``prefix`` — путь, на который отвечает эндпойнт
    (например "/session" или "/"). Методы GET/POST принимают
    ``(handler, remainder)``: handler — экземпляр Handler (доступны
    handler.context с root_dir/version и handler._write(...) для ответа),
    remainder — часть пути ПОСЛЕ prefix без ведущего "/" (пустая строка,
    если путь в точности равен prefix). По умолчанию эндпойнт не отвечает
    ни на один метод (404/405), поэтому для простого эндпойнта достаточно
    реализовать один GET.
    """

    prefix = ""

    def GET(self, handler, remainder: str):
        handler.send_error(404, "Not found")

    def POST(self, handler, remainder: str):
        handler.send_error(405, "Method Not Allowed")


ENDPOINTS: list = []  # list[type[Endpoint]] — заполняется декоратором register


def register(cls):
    """Декоратор: зарегистрировать класс эндпойнта в общем реестре.

    Применяется на уровне импорта модуля-эндпойнта:
        @webserver.register
        class SessionEndpoint(webserver.Endpoint): ...
    """
    if cls not in ENDPOINTS:
        ENDPOINTS.append(cls)
    return cls


# ==================== HTTP: КОНТЕКСТ И ДИСПЕТЧЕР ====================

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".yaml": "text/plain; charset=utf-8",
    ".yml": "text/plain; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".png": "image/png",
    ".puml": "text/plain; charset=utf-8",
    ".dot": "text/plain; charset=utf-8",
}


class WebContext:
    """Контекст запущенного веб-сервера: root_dir (корень, где лежат
    *.parts сессии), version (версия кода — из backend-adapter.py,
    передаётся в serve(), в пакете константы версии нет), verbose
    (понижать ли прогресс-сообщения генерации деревьев до DEBUG)."""

    def __init__(self, root_dir: str, version: str, verbose: bool):
        self.root_dir = root_dir
        self.version = version
        self.verbose = verbose


class Handler(http.server.BaseHTTPRequestHandler):
    """Единый диспетчер WEBUI-сервера.

    Запрос роутится по самому длинному зарегистрированному префиксу
    эндпойнта: путь либо равен prefix, либо начинается с prefix + "/".
    Исключение — корневой эндпойнт с prefix="/": он совпадает ТОЛЬКО с
    путём "/" (иначе он перехватывал бы все вложенные пути раньше более
    конкретных эндпойнтов вроде "/session/..."). Не совпало ни одного —
    404; эндпойнт не реализовал метод — 405.

    Контекст (root_dir/version) и список эндпойнтов задаются КЛАССОВЫМИ
    атрибутами перед запуском сервера (тот же приём, что Handler.root_dir
    в старом session_viewer: хэндлер пересоздаётся на каждый запрос,
    instance-атрибуты были бы недоступны)."""

    context: WebContext = None  # type: ignore[assignment]
    endpoints: list = []  # list[Endpoint] — инстанцированные эндпойнты

    def log_message(self, fmt, *args):
        logger.debug(f"{self.address_string()} - {fmt % args}")

    # ---- сервис для эндпойнтов ----

    def _write(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _content_type_for(self, path: str) -> str:
        ext = os.path.splitext(path)[1].lower()
        return CONTENT_TYPES.get(ext, "application/octet-stream")

    # ---- диспетчеризация ----

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method: str):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        if not path.startswith("/"):
            path = "/" + path

        endpoint, remainder = self._match(path)
        if endpoint is None:
            self.send_error(404, "Not found")
            return
        try:
            getattr(endpoint, method)(self, remainder)
        except ConnectionError:
            # клиент (браузер/вкладка) оборвал соединение — не ронять поток
            raise
        except BrokenPipeError:
            raise
        except Exception as e:
            logger.exception("[WEBUI] %s %s: ошибка обработки: %s", method, path, e)
            with contextlib.suppress(Exception):
                self.send_error(500, "Internal Server Error")

    def _match(self, path: str):
        """Самый длинный подходящий префикс среди endpoints.

        Возвращает (endpoint, remainder); remainder без ведущего "/".
        Для корневого эндпойнта (prefix="/") — только точный путь "/".
        """
        best = None
        best_len = -1
        for ep in self.endpoints:
            prefix = ep.prefix
            if not prefix.startswith("/"):
                prefix = "/" + prefix
            if prefix == "/":
                # корень совпадает ТОЛЬКО с путём "/" — иначе он перехватил бы
                # все вложенные пути раньше более конкретных эндпойнтов
                if path == "/":
                    return ep, ""
                continue
            if (path == prefix or path.startswith(prefix + "/")) and len(prefix) > best_len:
                best, best_len = ep, len(prefix)
        return (best, path[best_len:].lstrip("/")) if best else (None, "")


class QuietWebServer(http.server.ThreadingHTTPServer):
    """ThreadingHTTPServer с подавлением шумных ошибок соединения в
    handle_error (зеркало QuietThreadingHTTPServer из server.py — свой
    класс, чтобы WEBUI не зависел от 600-строчного прокси-модуля)."""

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)):
            return
        super().handle_error(request, client_address)


# ==================== ЗАПУСК ====================


def serve(
    root_dir: str, version: str, host: str = "127.0.0.1", port: int = 8765, verbose: bool = False
):
    """Поднять веб-сервер WEBUI в ДАННОМ процессе (не fork).

    Импортирует модули встроенных эндпойнтов (единственное место, где
    перечислены эндпойнты; новый добавляется сюда одной строкой), создаёт
    инстансы зарегистрированных классов с общим WebContext и возвращает
    инстанс ``QuietWebServer`` — вызывающий сам решает, когда звать
    ``serve_forever()`` (например, в daemon-потоке, как делает
    backend-adapter.py при ADAPTER_WEBUI_ENABLE=1).

    ``root_dir`` — папка, в которой лежат ``*.parts`` директории сессий
    (в адаптере это директория ADAPTER_DEBUG_LOGPATH). ``version`` — версия кода
    (передаётся из backend-adapter.py, где объявлена ``__version__``;
    в пакете её нет). ``verbose`` — как в artifact_tree.generate(): False
    понижает рутинные прогресс-сообщения генерации до DEBUG.

    Возвращает инстанс сервера, либо None, если root_dir не существует
    или не является директорией."""
    root_dir = os.path.abspath(root_dir)
    if not os.path.isdir(root_dir):
        logger.error(f"[WEBUI] Не найдена директория: {root_dir}")
        return None

    # Встроенные эндпойнты — единственное место их перечисления. Импорт
    # внутри функции (не на верхнем уровне): эндпойнт-модули сами
    # импортируют webserver (register/Endpoint), верхнеуровневый импорт
    # создал бы цикл.
    from . import session_viewer, webui_config_api, webui_status  # noqa: F401  (регистрируют себя)

    context = WebContext(root_dir=root_dir, version=version, verbose=verbose)
    Handler.context = context
    Handler.endpoints = [ep_cls(context) for ep_cls in ENDPOINTS]

    httpd = QuietWebServer((host, port), Handler)
    httpd.daemon_threads = True
    logger.info(f"[WEBUI] Сервер запущен: http://{host}:{port}/  (Ctrl+C — остановить)")
    logger.info(f"[WEBUI] Корень: {root_dir}")
    logger.info(f"[WEBUI] Версия кода: {version}")
    logger.info(
        "[WEBUI] Эндпойнты: " + ", ".join(sorted({f"{ep.prefix}" for ep in Handler.endpoints}))
    )
    return httpd


def _detect_version() -> str:
    """Версия кода для standalone-запуска (python -m webserver).

    Единственный источник версии — ``__version__`` в backend-adapter.py
    уровнем выше пакета; здесь она только ЧИТАЕТСЯ регэкспом, чтобы не
    дублировать значение. Если файла рядом нет (нестандартная установка) —
    "unknown"."""
    try:
        script = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "backend-adapter.py"
        )
        with open(script, encoding="utf-8") as f:
            head = f.read(4000)
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', head, re.MULTILINE)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "unknown"


def main():
    """CLI-запуск ядра: python -m backend_adapter.webserver [ROOT_DIR].

    Раньше этот ручной запуск жил в session_viewer.py (python -m
    backend_adapter.session_viewer ...) — при разделении файла CLI переехал
    сюда вместе с остальным серверным функционалом, а модули-эндпойнты
    остались чистыми библиотеками без main()/argparse. Встроенные
    эндпойнты регистрируются внутри serve(), так что из CLI видны те же
    страницы, что и внутри адаптера."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "root_dir",
        nargs="?",
        default=".",
        help="Корневая папка с *.parts сессиями (по умолчанию — текущая)",
    )
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument(
        "--host",
        default="127.0.0.1",
        help="По умолчанию только localhost — содержимое сессий (git log, "
        "файлы, reasoning) не должно случайно утечь в сеть",
    )
    args = ap.parse_args()

    httpd = serve(args.root_dir, _detect_version(), host=args.host, port=args.port, verbose=True)
    if httpd is None:
        sys.exit(1)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Остановлено.")


if __name__ == "__main__":
    # При `python -m` runpy исполняет модуль ДВАЖДЫ: pre-импортирует его в
    # sys.modules (каноническая копия, куда импортируются эндпойнт-модули
    # через `from . import ...`) и затем выполняет этот же файл ещё раз как
    # __main__ (отдельный namespace с ПУСТЫМ реестром ENDPOINTS). Если бы
    # serve() звался из namespace __main__, все эндпойнты остались бы
    # незарегистрированными и сервер отвечал бы 404 на каждый путь
    # (воспроизведено вручную: python -m backend_adapter.webserver → 404
    # на /, /session; в тестах не видно — там serve() зовётся напрямую из
    # канонического модуля). Поэтому __main__ — только трамплин: делегирует
    # канонической копии, которая и содержит зарегистрированные эндпойнты.
    from backend_adapter.webserver import main as _main

    _main()

__all__ = [
    "Endpoint",
    "register",
    "ENDPOINTS",
    "CONTENT_TYPES",
    "WebContext",
    "Handler",
    "QuietWebServer",
    "serve",
    "main",
    "_detect_version",
]
