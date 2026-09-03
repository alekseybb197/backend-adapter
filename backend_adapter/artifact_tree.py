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
import logging
import os
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


def _raw_file_href(parts_dir: str, preferred_yaml_name: str):
    """НЕ копирует ничего — только проверяет, что raw part-файл реально
    существует (предпочтительно .yaml, .json как честный фолбэк, если
    .yaml-соседа нет), и возвращает готовую ОТНОСИТЕЛЬНУЮ ссылку на него
    "снаружи" artefacts/ — "../<имя файла>". Само artefacts/ остаётся
    чисто производной директорией (вычислено из raw-дампов, можно стереть
    и пересчитать в любой момент) — копирование raw-файлов туда размыло бы
    эту границу и задублировало данные на диске. Единообразие ссылки что
    при открытии tree.html напрямую через file://, что через сервер
    достигается зеркалированием URL-схемы сервера на реальную структуру
    диска — см. session_viewer.py."""
    yaml_src = os.path.join(parts_dir, preferred_yaml_name)
    if os.path.isfile(yaml_src):
        return f"../{preferred_yaml_name}"
    json_name = preferred_yaml_name[: -len(".yaml")] + ".json"
    json_src = os.path.join(parts_dir, json_name)
    if os.path.isfile(json_src):
        return f"../{json_name}"
    return None


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

    registry = ArtifactRegistry()
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
    ) = build_turns(parts_dir, registry)

    # Ссылки из заголовка панели хода на исходные (не извлечённые)
    # openai_body/fetch_raw файлы — без копирования, см. _raw_file_href.
    for t in turns:
        t["ob_raw_file"] = _raw_file_href(parts_dir, t["ob_raw_file"])
        t["fr_raw_file"] = _raw_file_href(parts_dir, t["fr_raw_file"])

    registry.write_all(out_dir)
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
