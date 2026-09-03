#!/usr/bin/env python3
"""
session_viewer.py — эндпойнт "/session" общего веб-сервера WEBUI:
просмотр сессий адаптера (директории *.parts) в браузере.

Что делает эндпойнт: сканирует корневую папку (WebContext.root_dir) на
вложенные *.parts-директории сессий (session-XXXX.parts). Для каждой:
  - если в ней уже есть artefacts/tree.html и он НЕ устарел — используется
    как есть;
  - если tree.html нет, или он есть, но появились более новые part-файлы
    (дамп сессии дополнился) — генерируется/перегенерируется НА ЛЕТУ
    вызовом artifact_tree.generate() (тот же самый код, что и в CLI-версии:
    артефакты, tree.puml, tree.png, tree.html — всё, без сокращений);
  - если сама *.parts-папка исчезла с диска — её вкладка перестаёт
    появляться сама собой (список никогда не кэшируется, каждый заход —
    это заново os.listdir по факту).
Готовые деревья показываются на ОДНОЙ странице во вкладках — каждая
вкладка грузит свой tree.html в <iframe> (сам tree.html самодостаточен,
никаких общих ресурсов ему для этого не нужно).

URL-схема эндпойнта ЗЕРКАЛИТ реальную структуру диска один в один:
    /session/<имя сессии>/<путь относительно самой *.parts директории>
например:
    /session/session-X.parts/artefacts/tree.html      -> .../session-X.parts/artefacts/tree.html
    /session/session-X.parts/artefacts/toolcall-1.yaml -> .../session-X.parts/artefacts/toolcall-1.yaml
    /session/session-X.parts/bb1194de-116401-openai_body.yaml -> .../session-X.parts/bb1194de-...yaml
Это осознанное решение: tree.html содержит гиперссылки на артефакты
(соседи в той же artefacts/) и на исходные raw part-файлы (на уровень
выше, "../имя файла") — при таком зеркалировании ОДНИ И ТЕ ЖЕ
относительные ссылки одинаково резолвятся что при открытии tree.html
напрямую через file://, что через веб-сервер. Раньше сервер отдавал
tree.html по плоскому "/session/<имя>/tree.html" без сегмента artefacts/,
из-за чего "../raw_file.yaml" уводил не туда — обсуждали и намеренно не
стали "чинить" это копированием raw-файлов внутрь artefacts/ (это
задублировало бы данные на диске и размыло границу между исходными
данными и производной директорией) — вместо этого зеркалируем URL.

Модуль — чистый эндпойнт: НЕ содержит CLI (main/argparse), создания
сервера и обработчика — всё это переехало в общее ядро webserver.py
(CLI — `python -m backend_adapter.webserver [ROOT] [--port] [--host]`).
Требует artifact_tree рядом (в той же директории) — импортируется
напрямую, а не дублирует логику генерации и не дёргает его через
subprocess. Логгер "artifact_tree" наследует формат логов от того, кто
запустил процесс (вручную — стандартный запасной хендлер logging).

Реактивность реализована ПОЛЛИНГОМ при заходе на страницу (без фонового
watcher'а на inotify/FSEvents и без внешних зависимостей вроде watchdog —
ту же задачу решает простое сравнение mtime, и оно полностью переносимо):
каждая загрузка заново сканирует диск и решает для каждой сессии, нужна
ли (пере)генерация. Автообновление браузера БЕЗ действия пользователя
сознательно не делается — открытая вкладка может быть в процессе
активного исследования (pan/zoom, открытый popup), и молча дёргать
reload было бы разрушительно; вместо этого — просто обновите страницу
браузера, когда захотите увидеть актуальное состояние.
"""

import html
import logging
import os
import shutil
import urllib.parse

from . import artifact_tree, webserver

SESSION_SUFFIX = ".parts"
TREE_RELATIVE_PATH = os.path.join("artefacts", "tree.html")
ARTEFACTS_DIRNAME = "artefacts"

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
    (не к artefacts/ и не к tree.html внутри неё) — эндпойнт зеркалит на
    неё всю свою URL-схему, поэтому дальше нужен именно корень, а не
    готовый файл. Для каждой *.parts-директории прямо внутри root_dir:
      - нет tree.html -> генерируем;
      - tree.html есть, но устарел (см. _is_stale) -> перегенерируем
        (сперва стерев старую artefacts/, чтобы не копились файлы-сироты
        от предыдущей генерации, если дедупликация артефактов между
        запусками сдвинулась);
      - иначе -> используем как есть, без повторной генерации.
    Директория, для которой генерация не удалась, пропускается с
    предупреждением в лог — а не роняет страницу целиком.

    Удаление сессии обрабатывается САМО СОБОЙ — функция каждый раз заново
    делает os.listdir по факту, ничего не кэширует между вызовами."""
    sessions: list[tuple[str, str]] = []
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
                logger.info(
                    f"[{entry}] tree.html устарел (появились новые part-файлы) — перегенерирую..."
                )
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
        body = (
            f'<div id="empty">Не найдено ни одной директории <code>*{SESSION_SUFFIX}</code> в '
            f"<code>{html.escape(root_dir)}</code>."
            f"<br><br>Как только рядом появится папка <code>session-XXXX{SESSION_SUFFIX}</code> с дампами "
            f"адаптера — просто обновите эту страницу: если в ней ещё нет готового "
            f"<code>{TREE_RELATIVE_PATH.replace(os.sep, '/')}</code>, он будет сгенерирован автоматически. "
            f"Перезапускать сервер не нужно.</div>"
        )
        return SHELL_TEMPLATE.format(body=body)

    tabs_html = []
    frames_html = []
    for i, (name, _) in enumerate(sessions):
        safe_id = "".join(c if c.isalnum() else "_" for c in name)
        # ".parts" в подписи вкладки — чисто техническое расширение имени
        # директории, самостоятельного смысла не несёт. В URL и во всех
        # словарях-поисках имя сессии остаётся полным.
        display_name = name[: -len(SESSION_SUFFIX)] if name.endswith(SESSION_SUFFIX) else name
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


@webserver.register
class SessionEndpoint(webserver.Endpoint):
    """Эндпойнт "/session": просмотр *.parts сессий (страница с вкладками)
    и раздача файлов сессии по URL-схеме, зеркалящей диск 1:1 (см. докстринг
    модуля)."""

    prefix = "/session"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        sessions = find_or_generate_sessions(self.context.root_dir, verbose=self.context.verbose)
        if not remainder:
            # "/session" — страница с вкладками всех сессий
            body = render_shell(sessions, self.context.root_dir)
            handler._write(200, "text/html; charset=utf-8", body.encode("utf-8"))
            return

        # "/session/<имя сессии>/<путь>" — <путь> может быть как
        # "artefacts/tree.html" или "artefacts/toolcall-1.yaml" (внутри
        # производной директории), так и просто "bb1194de-...yaml"
        # (исходный raw part-файл на уровень выше artefacts/, на него
        # ссылаются относительным "../..." из tree.html) — URL-схема
        # НАМЕРЕННО зеркалит реальную структуру диска 1:1, см. докстринг.
        session_name, _, rel_path = remainder.partition("/")
        if not rel_path:
            # "/session/<имя>" без пути — отдаём дерево сессии (если есть);
            # это удобная форма для прямой ссылки на конкретную сессию
            rel_path = TREE_RELATIVE_PATH.replace(os.sep, "/")

        sessions_dict = dict(sessions)
        parts_dir = sessions_dict.get(session_name)
        if parts_dir is None:
            handler.send_error(404, "Session not found")
            return

        # Защита от обхода пути: разрешаем вложенность (нужно для
        # "artefacts/имя.yaml"), но итоговый АБСОЛЮТНЫЙ путь после
        # разрешения обязан остаться СТРОГО внутри parts_dir — а не
        # "../../.." куда-то наружу. Проверяем через realpath, а не
        # текстовый поиск "..", чтобы не полагаться на конкретное
        # написание (URL-кодирование, двойные слэши и т.п. уже сняты
        # urllib.parse.unquote в диспетчере ядра).
        candidate = os.path.realpath(os.path.join(parts_dir, rel_path))
        parts_dir_real = os.path.realpath(parts_dir)
        if os.path.commonpath([candidate, parts_dir_real]) != parts_dir_real:
            handler.send_error(404, "Not found")
            return

        try:
            with open(candidate, "rb") as f:
                body = f.read()
        except OSError:
            handler.send_error(404, "File not found")
            return
        handler._write(200, handler._content_type_for(candidate), body)


__all__ = [
    "SESSION_SUFFIX",
    "TREE_RELATIVE_PATH",
    "ARTEFACTS_DIRNAME",
    "_is_stale",
    "find_or_generate_sessions",
    "render_shell",
    "SessionEndpoint",
]
