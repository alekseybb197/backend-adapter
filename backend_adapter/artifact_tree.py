#!/usr/bin/env python3
"""
artifact_tree.py — извлекает уникальные текстовые артефакты из
последовательности openai_body/fetch_raw дампов внутри *.parts директории
адаптера и строит дерево того, как эти артефакты складываются в каждый
запрос по мере роста диалога с LLM.

ПОЧЕМУ ИМЕННО openai_body & fetch_raw (а не body/response/tool_result) —
см. обсуждение: это единственная пара, которая содержит одновременно полный
вход модели (полная история сообщений) и её СЫРОЙ выход, включая
reasoning_content — то, что теряется при сборке response.json. body и
tool_result содержательно избыточны относительно openai_body (тот же
tool_result уже внутри openai_body.messages[], просто в другой
сериализации — role:"tool" вместо type:"tool_result").

=== Логика извлечения артефактов ===
Chat-completions-подобный протокол пересылает ПОЛНУЮ историю сообщений в
каждом запросе — значит system-промпт, исходный user-промпт и все прошлые
tool_result будут повторяться почти в КАЖДОМ openai_body. Дедуплицируем по
содержимому (sha256 текста): один и тот же контент регистрируется как
артефакт только один раз, при первом появлении, и получает имя вида
"<домен>-<part_id>", где part_id — номер part-файла, где он впервые
встретился (как и просили: имя = часть, где артефакт впервые использован).

Домены: system, user, tool_result (то, что вернул инструмент), toolcall
(решение модели вызвать инструмент + аргументы), reasoning (chain-of-thought
из fetch_raw), response (текст ответа модели — финального или
промежуточного), other (всё остальное, что не подошло под эти классы).

=== Политика "почти дубликатов" ===
Некоторые части system-промпта содержат ВОЛАТИЛЬНЫЕ вставки, которые меняются
от запроса к запросу без изменения содержательного смысла — например,
`<total_tokens>N tokens left</total_tokens>` (счётчик остатка бюджета
контекста, дублируется мидстримом И довешивается ещё раз в конце с новым
числом на каждом следующем ходе). Без нормализации это плодит фиктивно
"новые" артефакты на каждом ходе, хотя system-промпт содержательно не
менялся. Перед вычислением ключа дедупликации такие фрагменты ВЫРЕЗАЮТСЯ
целиком (не заменяются плейсхолдером с числом — иначе тексты с разным
КОЛИЧЕСТВОМ вхождений тега всё равно не совпадут). Список паттернов —
VOLATILITY_PATTERNS ниже, расширяемый по мере обнаружения новых волатильных
вставок. Сохраняется при этом ИСХОДНЫЙ (не нормализованный) текст первого
появления — нормализация используется только для сравнения, не для контента
файла.

Использование:
    python3 artifact_tree.py /path/to/session-XXXX.parts
"""

import argparse
import json
import logging
import os
import re
import sys

# PyYAML-блок: многострочные значения (reasoning, tool_result, response и т.п.)
# читаются заметно удобнее как block scalar (content: |), чем как
# однострочная кавычная строка с буквальными \n внутри — при этом
# содержимое остаётся тем же самым, программный разбор через
# yaml.safe_load не меняется.
try:
    import yaml

    YAML_AVAILABLE = True

    def _yaml_str_representer(dumper, data):
        style = "|" if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    yaml.add_representer(str, _yaml_str_representer, Dumper=yaml.SafeDumper)
except ImportError:
    YAML_AVAILABLE = False

# Цвета и YAML_AVAILABLE — для обратной совместимости с внешним кодом,
# обращавшимся к ним как к атрибутам artifact_tree (в монолите они лежали
# прямо здесь). Реальное определение: DOMAIN_COLOR/KIND_COLOR — в common.py,
# ANCHOR_COLOR/SINK_COLOR/ORPHAN_COLOR — в html.py.
from .artifact_tree_common import DOMAIN_COLOR, KIND_COLOR, YAML_AVAILABLE
from .artifact_tree_graphviz import (
    render_png_via_graphviz_fallback,
    render_png_via_plantuml,
)
from .artifact_tree_html import (
    ANCHOR_COLOR,
    HTML_TEMPLATE,
    ORPHAN_COLOR,
    SINK_COLOR,
    _category_color,
    artifact_filename,
    build_graph_model,
    render_html,
    run_dot_plain_layout,
)
from .artifact_tree_parse import (
    classify_kind,
    compute_inline_labels,
    determine_fetch_raw_kind,
    discover_parts,
    fetch_raw_has_tool_calls,
    load_json,
    looks_like_structured_output,
    process_fetch_raw,
    process_openai_body,
)
from .artifact_tree_plantuml import puml_id, render_plantuml
from .artifact_tree_registry import ArtifactRegistry
from .artifact_tree_turnbuilder import build_turns

logger = logging.getLogger("artifact_tree")

# ==================== MAIN ====================


def _raw_file_href(parts_dir: str, preferred_yaml_name):
    """НЕ копирует ничего — только проверяет, что raw part-файл реально
    существует (предпочтительно .yaml, .json как честный фолбэк, если
    .yaml-соседа нет), и возвращает готовую ОТНОСИТЕЛЬНУЮ ссылку на него
    "снаружи" artefacts/ — "../<имя файла>". Само artefacts/ остаётся
    чисто производной директорией (вычислено из raw-дампов, можно стереть
    и пересчитать в любой момент) — копирование raw-файлов туда размыло бы
    эту границу и задублировало данные на диске. Единообразие ссылки что
    при открытии tree.html напрямую через file://, что через сервер
    достигается зеркалированием URL-схемы сервера на реальную структуру
    диска — см. session_viewer.py.

    ИДЕМПОТЕНТНА: при инкрементальной сборке (см. build_turns(resume_state=…))
    turns из чекпойнта уже прошли через эту функцию в ПРЕДЫДУЩЕМ вызове
    generate() — их ob_raw_file/fr_raw_file уже "../имя" (или None), не
    голое имя файла. Без этой проверки повторный вызов попытался бы
    искать несуществующий файл "../имя" внутри parts_dir и молча стирал бы
    уже рабочую ссылку в None."""
    if preferred_yaml_name is None or preferred_yaml_name.startswith("../"):
        return preferred_yaml_name
    yaml_src = os.path.join(parts_dir, preferred_yaml_name)
    if os.path.isfile(yaml_src):
        return f"../{preferred_yaml_name}"
    json_name = preferred_yaml_name[: -len(".yaml")] + ".json"
    json_src = os.path.join(parts_dir, json_name)
    if os.path.isfile(json_src):
        return f"../{json_name}"
    return None


CHECKPOINT_FILENAME = ".build_state.json"


def _load_checkpoint(out_dir: str, tag: str):
    """Загружает чекпойнт инкрементальной сборки (если есть и валиден).

    Возвращает (resume_state, registry) — resume_state=None и НОВЫЙ
    (пустой) ArtifactRegistry, если чекпойнта нет ИЛИ он повреждён/не
    распарсился: битый/устаревший чекпойнт — повод для холодной пересборки
    с нуля, а не повод падать (это тот же принцип, что и раньше — старая
    session_viewer._is_stale() полностью сносила artefacts/ при
    подозрении на рассинхронизацию; теперь степень доверия чекпойнту та же,
    просто по возможности мы пробуем им воспользоваться, а не сразу стираем)."""
    path = os.path.join(out_dir, CHECKPOINT_FILENAME)
    if not os.path.isfile(path):
        return None, ArtifactRegistry()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        registry = ArtifactRegistry.from_dict(data["registry"])
        resume_state = {
            "last_processed_part_id": data["last_processed_part_id"],
            "pending": data["pending"],
            "turns": data["turns"],
            "orphans": data["orphans"],
            "resolution_edges": data["resolution_edges"],
        }
        return resume_state, registry
    except (OSError, ValueError, KeyError, TypeError) as e:
        logger.warning(
            f"[{tag}] Чекпойнт {CHECKPOINT_FILENAME} повреждён/несовместим ({e}) — "
            f"пересобираю с нуля."
        )
        return None, ArtifactRegistry()


def _save_checkpoint(out_dir: str, registry: ArtifactRegistry, checkpoint: dict, tag: str):
    """Сохраняет чекпойнт. Ошибка записи — не повод ронять всю генерацию
    (артефакты/дерево уже посчитаны и записаны к этому моменту) — просто
    предупреждение: следующий вызов generate() для этой сессии сделает
    холодную пересборку вместо инкрементальной."""
    path = os.path.join(out_dir, CHECKPOINT_FILENAME)
    data = {**checkpoint, "registry": registry.to_dict()}
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except OSError as e:
        logger.warning(f"[{tag}] Не удалось сохранить чекпойнт {CHECKPOINT_FILENAME}: {e}")


_PART_ID_SUFFIX_RE = re.compile(r"-(\d+)(?:-\d+)?$")


def _extract_part_id(name: str):
    """Вытаскивает part_id, зашитый в имя артефакта ("toolcall-116402" или
    "toolcall-116402-1" при коллизии дедупликации имён) — для фильтрации
    по странице (см. _filter_for_page). None, если имя не подходит под
    паттерн (Start/Finish/Superseded/SessionTitle и т.п. — у них номера
    части нет, и по странице их не фильтруют явно, вызывающий сам решает,
    что с ними делать)."""
    m = _PART_ID_SUFFIX_RE.search(name)
    return int(m.group(1)) if m else None


def _filter_for_page(
    turns,
    orphans,
    resolution_edges,
    inline_labels,
    superseded_targets,
    title_targets,
    request_answers,
    start_pid,
    end_pid,
    page_uname,
):
    """Возвращает срез всего состояния сборки, относящийся к ОДНОЙ странице
    (одному реальному запросу пользователя) — ob_part_id хода в [start_pid,
    end_pid). Страница — самостоятельный, самодостаточный граф (та же
    структура, что у полного дерева, просто по одному логическому запросу
    за раз) — то, ради чего и делалась пагинация: не резать историю, а
    просто не показывать всё разом.

    Рёбра/имена без part_id в самом имени (resolution_edges,
    superseded_targets, title_targets) фильтруются по part_id ОДНОЙ ИЗ
    сторон, попадающей в диапазон страницы — этого достаточно, так как эти
    связи почти всегда локальны внутри одного запроса (мы это уже
    проверяли на реальных данных); редкий случай связи, пересекающей
    границу страницы, на этой отдельной странице просто не покажется —
    сама связь всё равно видна на артефакте через его собственный файл."""
    page_turns = [t for t in turns if start_pid <= t["ob_part_id"] < end_pid]
    page_orphans = [o for o in orphans if start_pid <= o["part_id"] < end_pid]

    def in_range(name):
        pid = _extract_part_id(name)
        return pid is not None and start_pid <= pid < end_pid

    page_resolution_edges = [(c, r) for c, r in resolution_edges if in_range(c) or in_range(r)]
    page_superseded = [n for n in superseded_targets if in_range(n)]
    page_title_targets = [n for n in title_targets if in_range(n)]
    page_request_answers = (
        {page_uname: request_answers[page_uname]} if page_uname in request_answers else {}
    )

    return (
        page_turns,
        page_orphans,
        page_resolution_edges,
        page_superseded,
        page_title_targets,
        page_request_answers,
        inline_labels,  # общий словарь, безвредно передавать целиком (лишние ключи не используются)
    )


def _generate_pages(
    parts_dir,
    out_dir,
    turns,
    orphans,
    resolution_edges,
    start_target,
    inline_labels,
    superseded_targets,
    title_targets,
    request_answers,
    page_boundaries,
    registry,
    log,
    warn,
):
    """Строит ОТДЕЛЬНЫЙ комплект tree.puml/tree.png/tree.html НА КАЖДУЮ
    страницу (один реальный запрос пользователя — границы уже посчитаны в
    build_turns()/_finalize(), см. page_boundaries) в
    artefacts/pages/<N>/. Плюс маленький index.html со списком страниц.
    Полный (все страницы сразу) tree.* на верхнем уровне artefacts/
    остаётся КАК ЕСТЬ — это дополнительный, а не заменяющий вид."""
    if not page_boundaries:
        return
    pages_dir = os.path.join(out_dir, "pages")
    os.makedirs(pages_dir, exist_ok=True)

    index_rows = []
    for i, (start_pid, end_pid, uname) in enumerate(page_boundaries, start=1):
        page_dir = os.path.join(pages_dir, str(i))
        os.makedirs(page_dir, exist_ok=True)

        (
            p_turns,
            p_orphans,
            p_resolution_edges,
            p_superseded,
            p_title_targets,
            p_request_answers,
            p_inline_labels,
        ) = _filter_for_page(
            turns,
            orphans,
            resolution_edges,
            inline_labels,
            superseded_targets,
            title_targets,
            request_answers,
            start_pid,
            end_pid,
            uname,
        )
        # На отдельной странице своя Start/Finish-семантика теряет смысл
        # (это не начало/не конец ВСЕЙ сессии) — единственная опорная
        # точка страницы — сам запрос (uname) и её собственный ответ
        # (p_request_answers[uname], если уже есть).
        p_start = uname
        p_finish = p_request_answers.get(uname)
        p_next_request_edges: list = []  # межстраничные — не показываем внутри одной страницы

        p_puml = render_plantuml(
            p_turns,
            p_orphans,
            p_resolution_edges,
            p_start,
            p_inline_labels,
            p_finish,
            p_superseded,
            p_title_targets,
            p_request_answers,
            p_next_request_edges,
        )
        puml_path = os.path.join(page_dir, "tree.puml")
        with open(puml_path, "w", encoding="utf-8") as f:
            f.write(p_puml)

        png_path = os.path.join(page_dir, "tree.png")
        if not render_png_via_plantuml(puml_path):
            render_png_via_graphviz_fallback(
                p_turns,
                p_orphans,
                p_resolution_edges,
                p_start,
                p_inline_labels,
                p_finish,
                p_superseded,
                p_title_targets,
                p_request_answers,
                p_next_request_edges,
                png_path,
            )

        p_model = build_graph_model(
            p_turns,
            p_orphans,
            p_resolution_edges,
            p_start,
            p_inline_labels,
            p_finish,
            p_superseded,
            p_title_targets,
            p_request_answers,
            p_next_request_edges,
            registry,
        )
        p_layout = run_dot_plain_layout(p_model)
        html_path = os.path.join(page_dir, "tree.html")
        render_html(p_model, p_layout, html_path)

        index_rows.append((i, uname, len(p_turns)))

    index_html = (
        "<!DOCTYPE html><html lang='ru'><head><meta charset='utf-8'>"
        "<title>Страницы сессии</title><style>"
        "body{font-family:-apple-system,Segoe UI,Arial,sans-serif;margin:24px}"
        "table{border-collapse:collapse}td{padding:6px 12px;border:1px solid #ddd}"
        "</style></head><body><h2>Страницы (по одному реальному запросу на страницу)</h2>"
        "<p><a href='../tree.html'>← полное дерево сессии целиком</a></p><table>"
        "<tr><th>#</th><th>Запрос (артефакт)</th><th>Ходов</th><th></th></tr>"
        + "".join(
            f"<tr><td>{i}</td><td><code>{uname}</code></td><td>{n}</td>"
            f"<td><a href='{i}/tree.html'>открыть →</a></td></tr>"
            for i, uname, n in index_rows
        )
        + "</table></body></html>"
    )
    with open(os.path.join(pages_dir, "index.html"), "w", encoding="utf-8") as f:
        f.write(index_html)

    log(f"Страниц собрано: {len(page_boundaries)} -> {pages_dir}/<N>/tree.{{puml,png,html}}")


def generate(parts_dir: str, verbose: bool = True) -> str:
    """Полный цикл генерации для одной *.parts директории: артефакты
    (.yaml/.txt) + tree.puml + tree.png (PlantUML, либо Graphviz-fallback)
    + tree.html (интерактивная версия). Возвращает путь к tree.html.

    Вынесено из main() в отдельную функцию, чтобы её можно было ВЫЗЫВАТЬ
    ПРОГРАММНО (см. session_viewer.py — веб-сервер вызывает generate() "на
    лету" для тех сессий, где artefacts/tree.html ещё не создан или устарел,
    вместо того чтобы дублировать эту логику или дёргать скрипт через
    subprocess).

    verbose=False понижает обычные прогресс-сообщения до уровня DEBUG (не
    видны при стандартной настройке логирования на INFO) — полезно при
    вызове из сервера на каждый запрос, чтобы не спамить лог рутинными
    строками. Предупреждения (осиротевшие fetch_raw, недоступный
    plantuml/graphviz) логируются как WARNING всегда, независимо от
    verbose — это не рутинный прогресс, а сигнал, который не должен
    потеряться в потоке.

    Имя директории добавляется в начало каждого сообщения — при вызове из
    session_viewer.py генерация нескольких сессий может идти в разных
    запросах почти одновременно (ThreadingHTTPServer), и без этого префикса
    в общем логе было бы не различить, какая строка к какой сессии
    относится."""
    progress_level = logging.INFO if verbose else logging.DEBUG
    tag = os.path.basename(os.path.normpath(parts_dir))

    def log(msg):
        logger.log(progress_level, f"[{tag}] {msg}")

    def warn(msg):
        logger.warning(f"[{tag}] {msg}")

    out_dir = os.path.join(parts_dir, "artefacts")
    os.makedirs(out_dir, exist_ok=True)

    resume_state, registry = _load_checkpoint(out_dir, tag)
    was_incremental = resume_state is not None
    prev_processed = resume_state["last_processed_part_id"] if resume_state else -1

    (
        turns,
        orphans,
        resolution_edges,
        start_target,
        inline_labels,
        finish_source,
        superseded_targets,
        title_targets,
        request_answers,
        next_request_edges,
        page_boundaries,
        checkpoint,
    ) = build_turns(parts_dir, registry, resume_state)

    new_files_count = sum(1 for t in turns if t["ob_part_id"] > prev_processed) + sum(
        1 for o in orphans if o["part_id"] > prev_processed
    )
    if was_incremental:
        log(
            f"Инкрементально: продолжаю с part_id > {prev_processed}, "
            f"новых ходов/сирот в этом запуске: {new_files_count}"
        )

    # Ссылки из заголовка панели хода на исходные (не извлечённые)
    # openai_body/fetch_raw файлы — без копирования, см. _raw_file_href.
    # Идемпотентно: у ходов, унаследованных из чекпойнта, эти поля уже в
    # виде "../имя" — функция их не трогает повторно.
    for t in turns:
        t["ob_raw_file"] = _raw_file_href(parts_dir, t["ob_raw_file"])
        t["fr_raw_file"] = _raw_file_href(parts_dir, t["fr_raw_file"])

    registry.write_all(out_dir)
    _save_checkpoint(out_dir, registry, checkpoint, tag)
    ext = "yaml" if YAML_AVAILABLE else "txt"
    log(f"Артефактов: {len(registry.by_hash)} -> {out_dir}/*.{ext}")
    log(f"Ходов сопоставлено: {len(turns)}")
    log(f"Связей toolcall->tool_result: {len(resolution_edges)}")
    log(f"Реальных запросов пользователя в сессии: {len(request_answers)}")
    if inline_labels:
        log(f"Вписано в блоки Ход вместо отдельных узлов: {inline_labels}")
    if request_answers:
        log(f"Запрос -> ответ: {request_answers}")
    if superseded_targets:
        log(
            f"Вытесненных повторным запросом финальных ответов: {len(superseded_targets)} -> {superseded_targets}"
        )
    if orphans:
        warn(
            f"Осиротевших fetch_raw (нить началась до окна дампов): {len(orphans)} "
            f"-> part_id {[o['part_id'] for o in orphans]}"
        )

    puml_text = render_plantuml(
        turns,
        orphans,
        resolution_edges,
        start_target,
        inline_labels,
        finish_source,
        superseded_targets,
        title_targets,
        request_answers,
        next_request_edges,
    )
    puml_path = os.path.join(out_dir, "tree.puml")
    with open(puml_path, "w", encoding="utf-8") as f:
        f.write(puml_text)
    log(f"PlantUML: {puml_path}")

    png_path = os.path.join(out_dir, "tree.png")
    if render_png_via_plantuml(puml_path):
        log(f"PNG (настоящий PlantUML): {png_path}")
    elif render_png_via_graphviz_fallback(
        turns,
        orphans,
        resolution_edges,
        start_target,
        inline_labels,
        finish_source,
        superseded_targets,
        title_targets,
        request_answers,
        next_request_edges,
        png_path,
    ):
        warn(
            f"Локальный plantuml/java не найден — PNG отрендерен запасным способом "
            f"через Graphviz: {png_path} (структура та же, но это НЕ PlantUML-рендер)"
        )
    else:
        warn(
            "Ни plantuml, ни graphviz (dot) не найдены — PNG не создан. "
            "tree.puml можно отрендерить на любой другой машине с Java "
            "(`plantuml tree.puml`) или через https://plantuml.com (свой сервер)."
        )

    # Интерактивная HTML-версия — для сессий, где статичная картинка (PNG/
    # PlantUML) уже нечитаема из-за плотности графа. Открывается напрямую в
    # браузере (file://), без сервера и без внешних библиотек.
    model = build_graph_model(
        turns,
        orphans,
        resolution_edges,
        start_target,
        inline_labels,
        finish_source,
        superseded_targets,
        title_targets,
        request_answers,
        next_request_edges,
        registry,
    )
    layout = run_dot_plain_layout(model)
    html_path = os.path.join(out_dir, "tree.html")
    render_html(model, layout, html_path)
    if layout:
        log(f"HTML (интерактивный, укладка Graphviz): {html_path}")
    else:
        warn(
            f"HTML сгенерирован, но dot не найден — укладка посчитана в браузере "
            f"(грубее настоящего Graphviz): {html_path}"
        )

    # Пагинация: тот же граф, но разбитый по страницам (одна страница =
    # один реальный запрос пользователя) — для сессий, где даже
    # интерактивный HTML целиком уже перегружен для изучения. Полный вид
    # (весь код выше) остаётся как есть — страницы дополняют, не заменяют.
    _generate_pages(
        parts_dir,
        out_dir,
        turns,
        orphans,
        resolution_edges,
        start_target,
        inline_labels,
        superseded_targets,
        title_targets,
        request_answers,
        page_boundaries,
        registry,
        log,
        warn,
    )

    return html_path


def main():
    """CLI-запуск: python -m backend_adapter.artifact_tree <parts_dir>.
    Формат логов наследуется от того, кто запустил процесс (вручную —
    стандартный запасной хендлер logging)."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("parts_dir", help="Путь к директории *.parts с дампами адаптера")
    args = ap.parse_args()

    if not os.path.isdir(args.parts_dir):
        logger.error(f"Не найдена директория: {args.parts_dir}")
        sys.exit(1)

    generate(args.parts_dir, verbose=True)


if __name__ == "__main__":
    main()

__all__ = [
    "generate",
    "main",
    "YAML_AVAILABLE",
    "DOMAIN_COLOR",
    "KIND_COLOR",
    "ANCHOR_COLOR",
    "SINK_COLOR",
    "ORPHAN_COLOR",
    "HTML_TEMPLATE",
    "ArtifactRegistry",
    "discover_parts",
    "load_json",
    "process_openai_body",
    "process_fetch_raw",
    "classify_kind",
    "fetch_raw_has_tool_calls",
    "looks_like_structured_output",
    "determine_fetch_raw_kind",
    "compute_inline_labels",
    "build_turns",
    "render_plantuml",
    "puml_id",
    "render_png_via_plantuml",
    "render_png_via_graphviz_fallback",
    "_category_color",
    "artifact_filename",
    "build_graph_model",
    "run_dot_plain_layout",
    "render_html",
]
