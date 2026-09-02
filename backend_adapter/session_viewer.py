#!/usr/bin/env python3
"""
session_viewer.py — простой локальный веб-сервер: сканирует корневую папку
на вложенные *.parts-директории сессий (session-XXXX.parts). Для каждой:
  - если в ней уже есть artefacts/tree.html и он НЕ устарел — используется
    как есть;
  - если tree.html нет, или он есть, но появились более новые part-файлы
    (дамп сессии дополнился) — генерируется/перегенерируется НА ЛЕТУ
    вызовом artifact_tree.generate() (тот же самый код, что и в CLI-версии:
    артефакты, tree.puml, tree.png, tree.html — всё, без сокращений);
  - если сама *.parts-папка исчезла с диска — её вкладка перестаёт
    появляться сама собой (список никогда не кэшируется, каждый заход на
    "/" — это заново os.listdir по факту).
Готовые деревья показываются на ОДНОЙ странице во вкладках — каждая
вкладка грузит свой tree.html в <iframe> (сам tree.html самодостаточен,
никаких общих ресурсов ему для этого не нужно).

URL-схема сервера ЗЕРКАЛИТ реальную структуру диска один в один:
    /session/<имя сессии>/<путь относительно самой *.parts директории>
например:
    /session/session-X.parts/artefacts/tree.html      -> .../session-X.parts/artefacts/tree.html
    /session/session-X.parts/artefacts/toolcall-1.yaml -> .../session-X.parts/artefacts/toolcall-1.yaml
    /session/session-X.parts/bb1194de-116401-openai_body.yaml -> .../session-X.parts/bb1194de-...yaml
Это осознанное решение: tree.html содержит гиперссылки на артефакты
(соседи в той же artefacts/) и на исходные raw part-файлы (на уровень
выше, "../имя файла") — при таком зеркалировании ОДНИ И ТЕ ЖЕ
относительные ссылки одинаково резолвятся что при открытии tree.html
напрямую через file://, что через этот сервер. Раньше сервер отдавал
tree.html по плоскому "/session/<имя>/tree.html" без сегмента artefacts/,
из-за чего "../raw_file.yaml" уводил не туда — обсуждали и намеренно не
стали "чинить" это копированием raw-файлов внутрь artefacts/ (это
задублировало бы данные на диске и размыло границу между исходными
данными и производной директорией) — вместо этого зеркалируем URL.

Требует artifact_tree рядом (в той же директории) — импортируется
напрямую, а не дублирует логику генерации и не дёргает его через
subprocess. Логгер "artifact_tree" наследует формат логов от того, кто
запустил процесс (вручную — стандартный запасной хендлер logging):
сообщения из session_viewer и artifact_tree.generate() выходят в одном
формате, настроенном на уровне root-логгера процесса.

Реактивность реализована ПОЛЛИНГОМ при заходе на "/" (без фонового
watcher'а на inotify/FSEvents и без внешних зависимостей вроде watchdog —
ту же задачу решает простое сравнение mtime, и оно полностью переносимо):
каждая загрузка страницы заново сканирует диск и решает для каждой сессии,
нужна ли (пере)генерация. Автообновление браузера БЕЗ действия
пользователя сознательно не делается — открытая вкладка может быть в
процессе активного исследования (pan/zoom, открытый popup), и молча
дёргать reload было бы разрушительно; вместо этого — просто обновите
страницу браузера, когда захотите увидеть актуальное состояние.

Использование:
    python3 session_viewer.py [ROOT_DIR] [--port 8765] [--host 127.0.0.1]

ROOT_DIR по умолчанию — текущая директория (предполагается, что сервер
запускается прямо в корне папки с логами, как и просили).
"""
import argparse
import html
import http.server
import logging
import os
import shutil
import sys
import urllib.parse

SESSION_SUFFIX = ".parts"
TREE_RELATIVE_PATH = os.path.join("artefacts", "tree.html")
ARTEFACTS_DIRNAME = "artefacts"

# artifact_tree.py — соседний модуль пакета. Импортируем напрямую (а не
# subprocess), чтобы переиспользовать generate() как есть, без дублирования
# логики генерации дерева.
from . import artifact_tree

logger = logging.getLogger("session_viewer")


def _is_stale(parts_dir: str, tree_html_path: str) -> bool:
    """True, если хотя бы один "сырой" файл дампа (*.json/*.yaml прямо в
    parts_dir, НЕ внутри artefacts/) новее самого tree.html — то есть
    адаптер дописал новые part-файлы уже ПОСЛЕ того, как дерево было
    построено в прошлый раз, и его нужно пересчитать."""
    try:
        tree_mtime = os.path.getmtime(tree_html_path)
    except OSError:
        return True
    try:
        entries = os.listdir(parts_dir)
    except OSError:
        return False
    for entry in entries:
        if entry == ARTEFACTS_DIRNAME:
            continue
        if not (entry.endswith(".json") or entry.endswith(".yaml")):
            continue
        full = os.path.join(parts_dir, entry)
        try:
            if os.path.getmtime(full) > tree_mtime:
                return True
        except OSError:
            continue
    return False


def find_or_generate_sessions(root_dir: str, verbose: bool = False):
    """Возвращает [(session_name, abs_parts_dir), ...], отсортировано по
    имени. abs_parts_dir — абсолютный путь к САМОЙ *.parts директории
    (не к artefacts/ и не к tree.html внутри неё) — сервер зеркалит на неё
    всю свою URL-схему, поэтому дальше нужен именно корень, а не готовый
    файл. Для каждой *.parts-директории прямо внутри root_dir:
      - нет tree.html -> генерируем;
      - tree.html есть, но устарел (см. _is_stale) -> перегенерируем (сперва
        стерев старую artefacts/, чтобы не копились файлы-сироты от
        предыдущей генерации, если дедупликация артефактов между запусками
        сдвинулась);
      - иначе -> используем как есть, без повторной генерации.
    Директория, для которой генерация не удалась, пропускается с
    предупреждением в лог сервера — а не роняет всю страницу целиком.

    Удаление сессии обрабатывается САМО СОБОЙ — эта функция каждый раз
    заново делает os.listdir по факту, ничего не кэширует между вызовами."""
    sessions = []
    if not os.path.isdir(root_dir):
        return sessions
    for entry in sorted(os.listdir(root_dir)):
        full = os.path.join(root_dir, entry)
        if not os.path.isdir(full) or not entry.endswith(SESSION_SUFFIX):
            continue

        tree_html = os.path.join(full, TREE_RELATIVE_PATH)
        exists = os.path.isfile(tree_html)
        stale = exists and _is_stale(full, tree_html)

        if not exists or stale:
            if stale:
                logger.info(f"[{entry}] tree.html устарел (появились новые part-файлы) — перегенерирую...")
                shutil.rmtree(os.path.join(full, ARTEFACTS_DIRNAME), ignore_errors=True)
            else:
                logger.info(f"[{entry}] tree.html не найден — генерирую...")
            try:
                artifact_tree.generate(full, verbose=verbose)
            except Exception as e:
                logger.warning(f"[{entry}] генерация не удалась, пропускаю сессию: {e}")
                continue
        sessions.append((entry, os.path.abspath(full)))
    return sessions


SHELL_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Деревья артефактов сессий</title>
<style>
  html, body {{ margin: 0; padding: 0; height: 100%; font-family: -apple-system, Segoe UI, Arial, sans-serif; }}
  #tabs {{ display: flex; flex-wrap: wrap; background: #222; }}
  #tabs button {{ background: #222; color: #ccc; border: none; padding: 10px 16px; cursor: pointer;
                  font-size: 13px; border-right: 1px solid #444; white-space: nowrap; }}
  #tabs button.active {{ background: #fff; color: #111; font-weight: bold; }}
  #tabs button:hover {{ background: #444; }}
  #tabs button.active:hover {{ background: #fff; }}
  #frame-wrap {{ position: absolute; top: 42px; left: 0; right: 0; bottom: 0; }}
  iframe {{ width: 100%; height: 100%; border: none; }}
  #empty {{ padding: 24px; color: #555; }}
  #reload {{ margin-left: auto; background: #222; color: #ccc; border: none; padding: 10px 16px; cursor: pointer; font-size: 13px; }}
  #reload:hover {{ background: #444; }}
</style>
</head>
<body>
{body}
<script>
function showTab(id, btn) {{
  document.querySelectorAll('#tabs button.tab').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.querySelectorAll('iframe').forEach(f => f.style.display = 'none');
  document.getElementById('frame-' + id).style.display = 'block';
}}
</script>
</body>
</html>
"""


def render_shell(sessions, root_dir):
    if not sessions:
        body = (f'<div id="empty">Не найдено ни одной директории <code>*{SESSION_SUFFIX}</code> в '
                 f'<code>{html.escape(root_dir)}</code>.'
                 f'<br><br>Как только рядом появится папка <code>session-XXXX{SESSION_SUFFIX}</code> с дампами '
                 f'адаптера — просто обновите эту страницу: если в ней ещё нет готового '
                 f'<code>{TREE_RELATIVE_PATH.replace(os.sep, "/")}</code>, он будет сгенерирован автоматически. '
                 f'Перезапускать сервер не нужно.</div>')
        return SHELL_TEMPLATE.format(body=body)

    tabs_html = []
    frames_html = []
    for i, (name, _) in enumerate(sessions):
        safe_id = "".join(c if c.isalnum() else "_" for c in name)
        # ".parts" в подписи вкладки — чисто техническое расширение имени
        # директории, самостоятельного смысла не несёт. В URL и во всех
        # словарях-поисках имя сессии остаётся полным.
        display_name = name[:-len(SESSION_SUFFIX)] if name.endswith(SESSION_SUFFIX) else name
        active = " active" if i == 0 else ""
        display_style = "block" if i == 0 else "none"
        tabs_html.append(
            f'<button class="tab{active}" onclick="showTab(\'{safe_id}\', this)">{html.escape(display_name)}</button>'
        )
        tree_url = f"/session/{urllib.parse.quote(name)}/{urllib.parse.quote(TREE_RELATIVE_PATH.replace(os.sep, '/'))}"
        frames_html.append(
            f'<iframe id="frame-{safe_id}" src="{tree_url}" style="display:{display_style}"></iframe>'
        )
    tabs_html.append('<button id="reload" onclick="location.reload()">⟳ обновить список</button>')
    body = f'<div id="tabs">{"".join(tabs_html)}</div><div id="frame-wrap">{"".join(frames_html)}</div>'
    return SHELL_TEMPLATE.format(body=body)


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


class Handler(http.server.BaseHTTPRequestHandler):
    root_dir = "."  # переопределяется в main() перед запуском сервера

    def log_message(self, fmt, *args):
        logger.debug("%s - %s" % (self.address_string(), fmt % args))

    def _write(self, status: int, content_type: str, body: bytes):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path in ("/", ""):
            sessions = find_or_generate_sessions(self.root_dir, verbose=True)
            self._write(200, "text/html; charset=utf-8",
                        render_shell(sessions, self.root_dir).encode("utf-8"))
            return

        if path.startswith("/session/"):
            # /session/<имя сессии>/<путь> — <путь> может быть как
            # "artefacts/tree.html" или "artefacts/toolcall-1.yaml" (внутри
            # производной директории), так и просто "bb1194de-...yaml"
            # (исходный raw part-файл на уровень выше artefacts/, на него
            # ссылаются относительным "../..." из tree.html) — URL-схема
            # НАМЕРЕННО зеркалит реальную структуру диска 1:1, см. докстринг
            # модуля.
            remainder = path[len("/session/"):]
            session_name, _, rel_path = remainder.partition("/")
            if not rel_path:
                self.send_error(404, "Not found")
                return

            sessions = dict(find_or_generate_sessions(self.root_dir, verbose=False))
            parts_dir = sessions.get(session_name)
            if parts_dir is None:
                self.send_error(404, "Session not found")
                return

            # Защита от обхода пути: разрешаем вложенность (нужно для
            # "artefacts/имя.yaml"), но итоговый АБСОЛЮТНЫЙ путь после
            # разрешения обязан остаться СТРОГО внутри parts_dir — а не
            # "../../.." куда-то наружу. Проверяем через realpath, а не
            # текстовый поиск "..", чтобы не полагаться на конкретное
            # написание (URL-кодирование, двойные слэши и т.п. уже сняты
            # urllib.parse.unquote выше).
            candidate = os.path.realpath(os.path.join(parts_dir, rel_path))
            parts_dir_real = os.path.realpath(parts_dir)
            if os.path.commonpath([candidate, parts_dir_real]) != parts_dir_real:
                self.send_error(404, "Not found")
                return

            try:
                with open(candidate, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_error(404, "File not found")
                return
            ext = os.path.splitext(candidate)[1].lower()
            content_type = CONTENT_TYPES.get(ext, "application/octet-stream")
            self._write(200, content_type, body)
            return

        self.send_error(404, "Not found")


def serve(root_dir: str, host: str = "127.0.0.1", port: int = 8765,
          verbose: bool = False):
    """Поднять веб-сервер просмотра сессий в ДАННОМ процессе (не fork).

    Создаёт ``ThreadingHTTPServer`` (поток на запрос, как в адаптере) и
    возвращает его инстанс — вызывающий сам решает, когда звать
    ``serve_forever()`` (например, в отдельном daemon-потоке, как делает
    backend-adapter.py при ``ADAPTER_WEBUI_ENABLE=1``).

    ``root_dir`` — папка, в которой лежат ``*.parts`` директории сессий
    (в адаптере это ``ADAPTER_DEBUG_LOGFILE``). ``verbose`` — как в
    ``artifact_tree.generate()``: False понижает рутинные прогресс-сообщения
    генерации до DEBUG, чтобы не спамить общий лог при каждом запросе.

    Возвращает инстанс сервера, либо None, если ``root_dir`` не существует
    или не является директорией."""
    root_dir = os.path.abspath(root_dir)
    if not os.path.isdir(root_dir):
        logger.error(f"[WEBUI] Не найдена директория: {root_dir}")
        return None

    Handler.root_dir = root_dir
    sessions = find_or_generate_sessions(root_dir, verbose=verbose)
    logger.info(f"[WEBUI] Корень: {root_dir}")
    logger.info(f"[WEBUI] Сессий готово к показу (найдено или сгенерировано на лету): {len(sessions)}")
    for name, _ in sessions:
        logger.info(f"  - {name}")
    if not sessions:
        logger.warning("[WEBUI] Ни одной *.parts директории не найдено — страница откроется пустой. "
                       "Как только появится новая, обновите браузер (перезапуск сервера не нужен) — "
                       "tree.html для неё сгенерируется автоматически.")

    httpd = http.server.ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    logger.info(f"[WEBUI] Сервер запущен: http://{host}:{port}/  (Ctrl+C — остановить)")
    return httpd


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("root_dir", nargs="?", default=".",
                     help="Корневая папка с *.parts сессиями (по умолчанию — текущая)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1",
                     help="По умолчанию только localhost — содержимое сессий (git log, "
                          "файлы, reasoning) не должно случайно утечь в сеть")
    args = ap.parse_args()

    httpd = serve(args.root_dir, host=args.host, port=args.port, verbose=True)
    if httpd is None:
        sys.exit(1)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("Остановлено.")


if __name__ == "__main__":
    main()
