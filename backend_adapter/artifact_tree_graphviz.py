"""artifact_tree_graphviz — PNG-рендеры (PlantUML и Graphviz-fallback)."""

import os
import shutil
import subprocess

from .artifact_tree_common import DOMAIN_COLOR, KIND_COLOR, logger

# ==================== РЕНДЕР PNG ====================


def render_png_via_plantuml(puml_path: str) -> bool:
    """ВАЖНО: у PlantUML `-o <dir>` интерпретируется КАК ПУТЬ ОТНОСИТЕЛЬНО
    ДИРЕКТОРИИ ИСХОДНОГО .puml-файла, а не относительно текущей рабочей
    директории — это известная особенность PlantUML. Раньше здесь
    передавался `-o out_dir`, где puml_path УЖЕ лежит внутри out_dir — в
    результате PNG уезжал во вложенную artefacts/artefacts/tree.png вместо
    artefacts/tree.png. Раз puml_path и так уже лежит там, где нужно — не
    передаём -o вообще, PlantUML по умолчанию кладёт результат рядом с
    исходником."""
    plantuml_bin = shutil.which("plantuml")
    if not plantuml_bin:
        return False
    try:
        subprocess.run(
            [plantuml_bin, "-tpng", os.path.abspath(puml_path)],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return True
    except Exception as e:
        logger.warning(f"plantuml завершился с ошибкой: {e}")
        return False


def render_png_via_graphviz_fallback(
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
    out_path: str,
) -> bool:
    """Запасной рендер ТОЙ ЖЕ структуры через Graphviz, если локального
    PlantUML (Java) нет. Не является PlantUML-рендером — используется только
    чтобы иметь превью PNG в окружениях без Java/plantuml."""
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return False
    lines = ["digraph tree {", "  rankdir=TB;", "  node [shape=box, style=filled, fontsize=10];"]
    emitted = set()

    def emit(name):
        if name in emitted:
            return
        emitted.add(name)
        domain = name.split("-")[0]
        color = DOMAIN_COLOR.get(domain, "#FFFFFF").replace("#", "")
        lines.append(f'  "{name}" [fillcolor="#{color}"];')
        if start_target and name == start_target:
            lines.append(f'  "Start" -> "{name}";')

    if start_target:
        lines.append('  "Start" [shape=circle, fillcolor="#FFFFFF"];')

    def composition_bullet(name: str) -> str:
        if name in inline_labels:
            return f"• {name}:{inline_labels[name]}"
        return f"• {name}"

    prev = None
    for t in turns:
        node = f"turn_{t['ob_part_id']}"
        label_lines = (
            [
                f"Ход {t['ob_part_id']} ({t['kind']})",
                f"openai_body {t['ob_part_id']} -> fetch_raw {t['fr_part_id']}",
                "----",
                "состав запроса:",
            ]
            + [composition_bullet(name) for name in t["input_names"]]
            + [
                "----",
                "производит:",
            ]
            + ([f"• {t['reasoning_name']}"] if t["reasoning_name"] else [])
        )
        label = "\\l".join(label_lines) + "\\l"
        color = KIND_COLOR.get(t["kind"], "#FFFFFF").replace("#", "")
        lines.append(f'  "{node}" [label="{label}", fillcolor="#{color}", shape=box];')

        for name in t["input_names"]:
            if name in inline_labels:
                continue
            emit(name)
            lines.append(f'  "{name}" -> "{node}";')

        if t["reasoning_name"]:
            emit(t["reasoning_name"])
            lines.append(f'  "{node}" -> "{t["reasoning_name"]}" [label="produces"];')
            for name in t["decision_names"]:
                emit(name)
                lines.append(f'  "{t["reasoning_name"]}" -> "{name}" [label="leads to"];')
        else:
            for name in t["decision_names"]:
                emit(name)
                lines.append(f'  "{node}" -> "{name}" [label="produces"];')

        if prev:
            lines.append(f'  "{prev}" -> "{node}" [style=dashed, label="next"];')
        prev = node

    for rec in orphans:
        node = f"orphan_fr_{rec['part_id']}"
        lines.append(
            f'  "{node}" [label="ОСИРОТЕВШИЙ fetch_raw {rec["part_id"]}", fillcolor="#FF6B6B", shape=ellipse];'
        )
        all_output_names = ([rec["reasoning_name"]] if rec["reasoning_name"] else []) + rec[
            "decision_names"
        ]
        for name in all_output_names:
            emit(name)
            lines.append(f'  "{node}" -> "{name}" [label="produces"];')

    for caller_name, result_name in resolution_edges:
        emit(caller_name)
        emit(result_name)
        lines.append(
            f'  "{caller_name}" -> "{result_name}" [style=dashed, penwidth=2, color="#228833", label="resolves"];'
        )

    for uname, response_name in request_answers.items():
        emit(response_name)
        emit(uname)
        lines.append(
            f'  "{response_name}" -> "{uname}" [color="#0066CC", penwidth=2.2, label="answers"];'
        )

    for answer_name, next_uname in next_request_edges:
        emit(answer_name)
        emit(next_uname)
        lines.append(
            f'  "{answer_name}" -> "{next_uname}" [color="#CC6600", penwidth=2.2, label="next request"];'
        )

    if finish_source:
        emit(finish_source)
        lines.append('  "Finish" [shape=circle, fillcolor="#FFFFFF"];')
        lines.append(f'  "{finish_source}" -> "Finish";')
    if superseded_targets:
        lines.append(
            '  "Superseded" [fillcolor="#FFD6D6", label="Superseded\\l(вытеснено повторным запросом)\\l"];'
        )
        for name in superseded_targets:
            emit(name)
            lines.append(f'  "{name}" -> "Superseded";')
    if title_targets:
        lines.append(
            '  "SessionTitle" [fillcolor="#D6EAFF", label="SessionTitle\\l(заголовок сессии, UI)\\l"];'
        )
        for name in title_targets:
            emit(name)
            lines.append(f'  "{name}" -> "SessionTitle";')

    lines.append("}")
    dot_src = "\n".join(lines)
    dot_file = out_path.replace(".png", ".fallback.dot")
    with open(dot_file, "w", encoding="utf-8") as f:
        f.write(dot_src)
    try:
        subprocess.run(
            [dot_bin, "-Tpng", dot_file, "-o", out_path],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return True
    except Exception as e:
        logger.warning(f"graphviz fallback тоже не сработал: {e}")
        return False
