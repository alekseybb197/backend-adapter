"""artifact_tree_plantuml — PlantUML-рендер дерева артефактов."""

import re

from .artifact_tree_common import DOMAIN_COLOR, KIND_COLOR

# ==================== PLANTUML ====================


def puml_id(name: str) -> str:
    return "n_" + re.sub(r"[^A-Za-z0-9_]", "_", name)


def render_plantuml(turns, orphans, resolution_edges, start_target, inline_labels,
                     finish_source, superseded_targets, title_targets,
                     request_answers, next_request_edges) -> str:
    lines = ["@startuml", "skinparam rectangle {", "  RoundCorner 12", "}",
             "skinparam defaultTextAlignment center", ""]

    emitted_artifacts = set()

    def emit_artifact(name):
        if name in emitted_artifacts:
            return
        emitted_artifacts.add(name)
        domain = name.split("-")[0]
        color = DOMAIN_COLOR.get(domain, "#FFFFFF")
        lines.append(f'rectangle "{name}" as {puml_id(name)} {color}')
        if start_target and name == start_target:
            lines.append(f'n_start --> {puml_id(name)}')

    if start_target:
        lines.append('circle "Start" as n_start #FFFFFF')

    def composition_bullet(name: str) -> str:
        if name in inline_labels:
            return f"• {name}:{inline_labels[name]}"
        return f"• {name}"

    prev_turn_id = None
    for t in turns:
        turn_node = f"turn_{t['ob_part_id']}"
        color = KIND_COLOR.get(t["kind"], "#FFFFFF")
        label_lines = [
            f"Ход {t['ob_part_id']} ({t['kind']})",
            f"openai_body {t['ob_part_id']} -> fetch_raw {t['fr_part_id']}",
            "----",
            "состав запроса:",
        ] + [composition_bullet(name) for name in t["input_names"]] + [
            "----",
            "производит:",
        ] + ([f"• {t['reasoning_name']}"] if t["reasoning_name"] else [])
        turn_label = "\\n".join(label_lines)
        lines.append(f'rectangle "{turn_label}" as {puml_id(turn_node)} {color}')

        for name in t["input_names"]:
            if name in inline_labels:
                continue  # вписан текстом в блок — ни узла, ни стрелки
            emit_artifact(name)
            lines.append(f'{puml_id(name)} --> {puml_id(turn_node)}')

        if t["reasoning_name"]:
            emit_artifact(t["reasoning_name"])
            lines.append(f'{puml_id(turn_node)} --> {puml_id(t["reasoning_name"])} : produces')
            for name in t["decision_names"]:
                emit_artifact(name)
                lines.append(f'{puml_id(t["reasoning_name"])} --> {puml_id(name)} : leads to')
        else:
            for name in t["decision_names"]:
                emit_artifact(name)
                lines.append(f'{puml_id(turn_node)} --> {puml_id(name)} : produces')

        if prev_turn_id is not None:
            lines.append(f'{puml_id(prev_turn_id)} ..> {puml_id(turn_node)} : next (by part_id)')
        prev_turn_id = turn_node

    for rec in orphans:
        node = f"orphan_fr_{rec['part_id']}"
        lines.append(f'rectangle "ОСИРОТЕВШИЙ fetch_raw {rec["part_id"]}\\n(нить началась до окна дампов)" as {puml_id(node)} #FF6B6B')
        all_output_names = ([rec["reasoning_name"]] if rec["reasoning_name"] else []) + rec["decision_names"]
        for name in all_output_names:
            emit_artifact(name)
            lines.append(f'{puml_id(node)} --> {puml_id(name)} : produces')

    # П.5: toolcall -> tool_result
    for caller_name, result_name in resolution_edges:
        emit_artifact(caller_name)
        emit_artifact(result_name)
        lines.append(f'{puml_id(caller_name)} ..> {puml_id(result_name)} : resolves')

    # Явная связь "запрос -> ответ на НЕГО"
    for uname, response_name in request_answers.items():
        emit_artifact(response_name)
        emit_artifact(uname)
        lines.append(f'{puml_id(response_name)} -[#0066CC]-> {puml_id(uname)} : answers')

    # "next request"
    for answer_name, next_uname in next_request_edges:
        emit_artifact(answer_name)
        emit_artifact(next_uname)
        lines.append(f'{puml_id(answer_name)} -[#CC6600]-> {puml_id(next_uname)} : next request')

    # П.4: три разные судьбы response
    if finish_source:
        emit_artifact(finish_source)
        lines.append('circle "Finish" as n_finish #FFFFFF')
        lines.append(f'{puml_id(finish_source)} --> n_finish')
    if superseded_targets:
        lines.append('rectangle "Superseded\\n(вытеснено повторным запросом)" as n_superseded #FFD6D6')
        for name in superseded_targets:
            emit_artifact(name)
            lines.append(f'{puml_id(name)} --> n_superseded')
    if title_targets:
        lines.append('rectangle "SessionTitle\\n(заголовок сессии, UI)" as n_title #D6EAFF')
        for name in title_targets:
            emit_artifact(name)
            lines.append(f'{puml_id(name)} --> n_title')

    lines.append("@enduml")
    return "\n".join(lines)
