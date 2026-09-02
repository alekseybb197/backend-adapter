"""artifact_tree_html — интерактивная HTML-визуализация дерева артефактов."""

import json
import logging
import shlex
import subprocess

from .artifact_tree_common import DOMAIN_COLOR, KIND_COLOR, YAML_AVAILABLE, logger

logger = logging.getLogger("artifact_tree")

# ==================== HTML-ВИЗУАЛИЗАЦИЯ ====================
#
# Статичный PNG/PlantUML на графе такой плотности (десятки узлов, 6 типов
# рёбер: next/produces/leads_to/resolves/answers/next_request) неизбежно
# превращается в клубок пересекающихся линий — сколько бы мы ни улучшали
# укладку. Выход — не пытаться уместить всё сразу на одной картинке, а дать
# смотреть интерактивно: полное содержимое каждого узла уходит в popup по
# клику (а не впихивается в сам узел текстом, как раньше), рёбра можно
# фильтровать по типу, а укладку по-прежнему считает Graphviz (`dot`) — он
# с этим справляется хорошо, теряется только СТАТИЧНОСТЬ картинки, а не
# качество самой укладки.
#
# Внешних JS-библиотек тут НЕТ ВООБЩЕ (Cytoscape.js/Mermaid и т.п. даже не
# получилось скачать в этой песочнице — npm/cdnjs заблокированы egress-
# прокси) — вся интерактивность (pan/zoom, popup, фильтры, поиск) на
# ванильном JS, без сети. Файл открывается напрямую через file://, без
# локального сервера.

ANCHOR_COLOR = "#FFFFFF"
SINK_COLOR = "#FFD6D6"
ORPHAN_COLOR = "#FF6B6B"


def _category_color(category: str) -> str:
    if category.startswith("artifact:"):
        return DOMAIN_COLOR.get(category.split(":", 1)[1], "#FFFFFF")
    if category.startswith("turn:"):
        return KIND_COLOR.get(category.split(":", 1)[1], "#FFFFFF")
    if category == "anchor":
        return ANCHOR_COLOR
    if category == "sink":
        return SINK_COLOR
    if category == "orphan":
        return ORPHAN_COLOR
    return "#FFFFFF"


def artifact_filename(name: str) -> str:
    """Имя файла артефакта на диске — то же расширение, что реально
    выбрал ArtifactRegistry.write_all() (.yaml, если доступен PyYAML,
    иначе .txt-фолбэк). Используется для гиперссылок в HTML-детали узла
    Ход на составляющие его артефакты."""
    ext = "yaml" if YAML_AVAILABLE else "txt"
    return f"{name}.{ext}"


def build_graph_model(turns, orphans, resolution_edges, start_target, inline_labels,
                       finish_source, superseded_targets, title_targets,
                       request_answers, next_request_edges, registry):
    """Строит универсальную модель графа (узлы + рёбра + детали для popup)
    для HTML-визуализации. Узлы КОРОТКИЕ (просто имя) — весь состав хода,
    полный текст артефакта и т.п. лежит в node["detail"] и показывается по
    клику, а не впихивается в сам узел, как в PlantUML/Graphviz-версии."""
    from collections import OrderedDict
    nodes = OrderedDict()   # id -> {"label", "category", "detail"}
    edges = []              # {"source", "target", "type", "label"}

    def add_node(node_id, label, category, detail):
        if node_id not in nodes:
            nodes[node_id] = {"label": label, "category": category, "detail": detail}

    def add_edge(source, target, etype, label=""):
        edges.append({"source": source, "target": target, "type": etype, "label": label})

    def artifact_detail(name):
        entry = next((e for e in registry.by_hash.values() if e["name"] == name), None)
        if not entry:
            return {"title": name, "meta": "", "text": "(содержимое не найдено)", "file": None}
        return {
            "title": name,
            "meta": f"domain: {entry['domain']}   |   first_seen_part_id: {entry['first_part_id']}",
            "text": entry["text"],
            # Ссылка на СВОЙ ЖЕ файл в artefacts/ — тот же принцип, что и
            # для составляющих хода: полное содержимое доступно отдельным
            # файлом, а не только тем, что уместилось в панель.
            "file": artifact_filename(name),
        }

    def add_artifact_node(name):
        domain = name.split("-")[0]
        add_node(name, name, f"artifact:{domain}", artifact_detail(name))

    if start_target:
        add_node("Start", "Start", "anchor",
                  {"title": "Start", "meta": "", "text": "Начало сессии — самый ранний по part_id реальный запрос пользователя."})
        add_artifact_node(start_target)
        add_edge("Start", start_target, "start", "")

    for t in turns:
        turn_id = f"turn_{t['ob_part_id']}"
        # Ход — ЕДИНСТВЕННЫЙ композитный узел (собран из нескольких
        # артефактов сразу), поэтому его деталь описывается структурно
        # (composition/reasoning), а не одной строкой текста как у всех
        # остальных узлов — это даёт панели показать каждую составляющую
        # ОТДЕЛЬНОЙ гиперссылкой на её файл (см. showDetail() в JS), а не
        # просто перечислить имена в <pre>.
        composition = [
            {"label": (f"{n}:{inline_labels[n]}" if n in inline_labels else n), "file": artifact_filename(n)}
            for n in t["input_names"]
        ]
        reasoning = ({"label": t["reasoning_name"], "file": artifact_filename(t["reasoning_name"])}
                     if t["reasoning_name"] else None)
        add_node(turn_id, f"Ход {t['ob_part_id']}", f"turn:{t['kind']}", {
            "title": f"Ход {t['ob_part_id']} ({t['kind']})",
            # Гиперссылки на ИСХОДНЫЕ (не извлечённые) part-файлы этого
            # хода — тот же принцип, что и у composition/reasoning ниже,
            # только тут ссылка ведёт не на извлечённый артефакт, а на
            # полный сырой дамп запроса/ответа целиком (см. _stage_raw_file
            # в generate()). file=None, если файл не нашёлся вообще ни в
            # .yaml, ни в .json — тогда просто текст без ссылки.
            "meta_links": [
                {"label": f"openai_body {t['ob_part_id']}", "file": t["ob_raw_file"]},
                {"label": f"fetch_raw {t['fr_part_id']}", "file": t["fr_raw_file"]},
            ],
            "composition": composition,
            "reasoning": reasoning,
        })

        for name in t["input_names"]:
            if name in inline_labels:
                continue  # вписан текстом в состав хода — не отдельный узел
            add_artifact_node(name)
            add_edge(name, turn_id, "input", "")

        if t["reasoning_name"]:
            add_artifact_node(t["reasoning_name"])
            add_edge(turn_id, t["reasoning_name"], "produces", "produces")
            for name in t["decision_names"]:
                add_artifact_node(name)
                add_edge(t["reasoning_name"], name, "leads_to", "leads to")
        else:
            for name in t["decision_names"]:
                add_artifact_node(name)
                add_edge(turn_id, name, "produces", "produces")

    prev_turn_id = None
    for t in turns:
        turn_id = f"turn_{t['ob_part_id']}"
        if prev_turn_id is not None:
            add_edge(prev_turn_id, turn_id, "sequence", "next")
        prev_turn_id = turn_id

    for rec in orphans:
        node_id = f"orphan_{rec['part_id']}"
        add_node(node_id, f"orphan {rec['part_id']}", "orphan", {
            "title": f"Осиротевший fetch_raw {rec['part_id']}",
            "meta": "",
            "text": "Причинный openai_body не найден в видимом окне дампов — "
                    "нить, скорее всего, началась раньше начала записи.",
        })
        all_out = ([rec["reasoning_name"]] if rec["reasoning_name"] else []) + rec["decision_names"]
        for name in all_out:
            add_artifact_node(name)
            add_edge(node_id, name, "produces", "produces")

    for caller_name, result_name in resolution_edges:
        add_artifact_node(caller_name)
        add_artifact_node(result_name)
        add_edge(caller_name, result_name, "resolves", "resolves")

    for uname, response_name in request_answers.items():
        add_artifact_node(response_name)
        add_artifact_node(uname)
        add_edge(response_name, uname, "answers", "answers")

    for answer_name, next_uname in next_request_edges:
        add_artifact_node(answer_name)
        add_artifact_node(next_uname)
        add_edge(answer_name, next_uname, "next_request", "next request")

    if finish_source:
        add_artifact_node(finish_source)
        add_node("Finish", "Finish", "anchor",
                  {"title": "Finish", "meta": "", "text": "Финальный ответ на самый поздний реальный запрос сессии."})
        add_edge(finish_source, "Finish", "finish", "")

    if superseded_targets:
        add_node("Superseded", "Superseded", "sink", {
            "title": "Superseded", "meta": "",
            "text": "Ответы, вытесненные повторным (побайтово идентичным) запросом "
                    "внутри того же логического обращения пользователя.",
        })
        for name in superseded_targets:
            add_artifact_node(name)
            add_edge(name, "Superseded", "superseded", "")

    if title_targets:
        add_node("SessionTitle", "SessionTitle", "sink", {
            "title": "SessionTitle", "meta": "",
            "text": "Ответы сайдкара генерации заголовка сессии — свой законный "
                    "потребитель (UI), не часть основного диалога.",
        })
        for name in title_targets:
            add_artifact_node(name)
            add_edge(name, "SessionTitle", "title", "")

    return {"nodes": nodes, "edges": edges}


def run_dot_plain_layout(model: dict):
    """Считает укладку через `dot -Tplain` — простой построчный формат
    именно для передачи координат внешним инструментам (в отличие от
    -Tjson, не нужен JSON-парсер, только shlex.split на кавычко-safe
    токены). Лейблы узлов здесь короткие (см. build_graph_model) — полный
    контент в HTML идёт в popup, а не в сам узел. Возвращает None, если
    локального `dot` нет — тогда HTML сам посчитает грубую укладку по
    рангам в браузере (см. JS)."""
    import shutil as _shutil
    dot_bin = _shutil.which("dot")
    if not dot_bin:
        return None
    # width/height здесь ЯВНО совпадают с NODE_W/NODE_H в HTML_TEMPLATE
    # (150x34 SVG-единиц -> дюймы: /72). fixedsize=true — чтобы dot не
    # подгонял размер узла под длину текста лейбла: тогда его собственная
    # оценка "сколько места нужно этому узлу" будет РОВНО совпадать с тем,
    # как узел реально нарисован в SVG, и nodesep/ranksep гарантированно
    # не даст соседним узлам зайти друг на друга (без этого dot считал
    # расстояния по своей, обычно куда более узкой, оценке ширины текста).
    node_w_in = 150 / 72.0
    node_h_in = 34 / 72.0
    lines = ["digraph g {", "rankdir=TB; nodesep=0.4; ranksep=0.6;",
              f'node [shape=box, width={node_w_in:.4f}, height={node_h_in:.4f}, fixedsize=true];']
    for node_id, n in model["nodes"].items():
        safe_label = n["label"].replace('"', "'")
        lines.append(f'"{node_id}" [label="{safe_label}"];')
    for e in model["edges"]:
        lines.append(f'"{e["source"]}" -> "{e["target"]}";')
    lines.append("}")
    dot_src = "\n".join(lines)
    try:
        proc = subprocess.run([dot_bin, "-Tplain"], input=dot_src, capture_output=True,
                               text=True, timeout=60, check=True)
    except Exception as e:
        logger.warning(f"dot -Tplain не сработал, HTML посчитает укладку сам в браузере: {e}")
        return None

    positions = {}
    height = 0.0
    # `dot -Tplain` отдаёт координаты в ДЮЙМАХ (значения вида 0.4, 16.0 —
    # не путать с points/пикселями). Наши узлы в SVG нарисованы с размером
    # NODE_W=150/NODE_H=34 условных единиц — если оставить координаты как
    # есть, весь граф укладывается в область МЕНЬШЕ одного узла, и все
    # узлы визуально складываются в стопку друг на друга (ровно то, что
    # было на скриншоте). 72 — стандартный коэффициент Graphviz для
    # перевода дюймов в points, тем же масштабом, что использует сам dot
    # внутри для форматов вроде -Tps/-Tjson.
    SCALE = 72.0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if not parts:
            continue
        if parts[0] == "graph" and len(parts) >= 4:
            height = float(parts[3]) * SCALE
        elif parts[0] == "node" and len(parts) >= 4:
            name, x, y = parts[1], float(parts[2]) * SCALE, float(parts[3]) * SCALE
            positions[name] = (x, y)
    if not positions:
        return None
    return {"positions": positions, "height": height}


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Дерево артефактов сессии</title>
<style>
  html, body { margin: 0; padding: 0; height: 100%; font-family: -apple-system, Segoe UI, Arial, sans-serif; }
  #canvas-wrap { position: absolute; top: 0; left: 0; right: 340px; bottom: 0; overflow: hidden; background: #fafafa; cursor: grab; }
  #canvas-wrap.dragging { cursor: grabbing; }
  svg { width: 100%; height: 100%; display: block; }
  .node rect, .node ellipse { stroke: #333; stroke-width: 1; }
  .node text { font-size: 11px; pointer-events: none; }
  .node { cursor: pointer; }
  .node.highlight rect, .node.highlight ellipse { stroke: #d40000; stroke-width: 3; }
  .node:hover rect, .node:hover ellipse { stroke: #0066cc; stroke-width: 2; }
  .edge { fill: none; stroke: #888; stroke-width: 1.2; marker-end: url(#arrow); }
  .edge.type-answers { stroke: #0066CC; stroke-width: 1.8; }
  .edge.type-next_request { stroke: #CC6600; stroke-width: 1.8; }
  .edge.type-resolves { stroke: #228833; stroke-dasharray: 4 3; }
  .edge.type-sequence { stroke: #2a4d8f; stroke-width: 3; }
  .edge-outline { fill: none; stroke: #ffffff; stroke-width: 6; stroke-linecap: round; }
  .edge-label { font-size: 9px; fill: #555; pointer-events: none; }
  #panel { position: absolute; top: 0; right: 0; width: 340px; height: 100%; box-sizing: border-box;
           border-left: 1px solid #ccc; background: #fff; overflow-y: auto; padding: 12px; }
  #panel h2 { font-size: 14px; margin: 0 0 4px 0; }
  #panel .meta { font-size: 11px; color: #666; margin-bottom: 8px; white-space: pre-wrap; }
  #panel pre { font-size: 11.5px; white-space: pre-wrap; word-break: break-word; background: #f5f5f5;
               padding: 8px; border-radius: 4px; border: 1px solid #eee; }
  .section-label { font-size: 11px; font-weight: bold; color: #444; margin: 10px 0 4px 0; }
  .file-list { list-style: none; margin: 0 0 4px 0; padding: 0; }
  .file-list li { margin: 2px 0; }
  .file-list a { font-size: 12px; color: #0645ad; text-decoration: none; word-break: break-all; }
  .file-list a:hover { text-decoration: underline; }
  .file-list a::after { content: " ↗"; font-size: 10px; color: #999; }
  #controls { position: absolute; top: 8px; left: 8px; background: rgba(255,255,255,0.95); border: 1px solid #ccc;
              border-radius: 6px; padding: 8px 10px; font-size: 12px; max-width: 300px; z-index: 5; }
  .legend-swatch { display: inline-block; width: 10px; height: 10px; margin-right: 4px; border: 1px solid #999; vertical-align: middle; }
  #hint { color: #888; font-size: 11px; }
</style>
</head>
<body>
<div id="canvas-wrap">
  <svg id="svg">
    <defs>
      <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#888"></path>
      </marker>
      <marker id="arrow-hl" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
        <path d="M 0 0 L 10 5 L 0 10 z" fill="#222"></path>
      </marker>
    </defs>
    <g id="viewport">
      <g id="edges-bg"></g>
      <g id="nodes-layer"></g>
      <g id="edges-fg"></g>
    </g>
  </svg>
</div>
<div id="controls">
  <div id="hint">Колесо мыши — zoom, перетаскивание фона — pan, клик по узлу — детали.</div>
</div>
<div id="panel"><p id="hint">Кликните по узлу, чтобы увидеть его полное содержимое.</p></div>
<script>
const DATA = __DATA_JSON__;

const NODE_W = 150, NODE_H = 34;

function categoryColor(cat) {
  const domainColors = {system:"#FFF3CD", user:"#D1ECF1", tool_result:"#D4EDDA", toolcall:"#E0CFFC",
                         reasoning:"#F8D7DA", response:"#D6D8DB", other:"#FFFFFF"};
  const kindColors = {agent_turn:"#CFE2FF", structured_output:"#FFE5B4"};
  if (cat.startsWith("artifact:")) return domainColors[cat.slice(9)] || "#FFFFFF";
  if (cat.startsWith("turn:")) return kindColors[cat.slice(5)] || "#FFFFFF";
  if (cat === "anchor") return "#FFFFFF";
  if (cat === "sink") return "#FFD6D6";
  if (cat === "orphan") return "#FF6B6B";
  return "#FFFFFF";
}

// === Укладка ===
// Если dot посчитал позиции — используем их (переворачиваем Y: у dot ось
// растёт вверх, в SVG — вниз). Если нет (dot не нашёлся) — простая
// запасная укладка по рангам через BFS от узлов без входящих рёбер.
let positions = {};
if (DATA.hasLayout) {
  for (const n of DATA.nodes) positions[n.id] = {x: n.x, y: n.y};
} else {
  const incoming = {};
  DATA.nodes.forEach(n => incoming[n.id] = 0);
  DATA.edges.forEach(e => { if (e.target in incoming) incoming[e.target]++; });
  const rank = {};
  let frontier = DATA.nodes.filter(n => incoming[n.id] === 0).map(n => n.id);
  let seen = new Set(frontier);
  let r = 0;
  while (frontier.length) {
    frontier.forEach(id => rank[id] = r);
    const next = [];
    frontier.forEach(id => {
      DATA.edges.filter(e => e.source === id).forEach(e => {
        if (!seen.has(e.target)) { seen.add(e.target); next.push(e.target); }
      });
    });
    frontier = next; r++;
  }
  DATA.nodes.forEach(n => { if (!(n.id in rank)) rank[n.id] = r; });
  const byRank = {};
  DATA.nodes.forEach(n => { (byRank[rank[n.id]] = byRank[rank[n.id]] || []).push(n.id); });
  Object.keys(byRank).forEach(rk => {
    byRank[rk].forEach((id, i) => { positions[id] = {x: i * 200 + 100, y: rk * 100 + 60}; });
  });
}

// === Построение SVG ===
const svg = document.getElementById("svg");
const edgesBg = document.getElementById("edges-bg");
const nodesLayer = document.getElementById("nodes-layer");
const edgesFg = document.getElementById("edges-fg");
const nodeById = {};
DATA.nodes.forEach(n => nodeById[n.id] = n);

function escXml(s) { return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;"); }

// Причинно-значимые рёбра рисуются В ОТДЕЛЬНОМ слое ПОВЕРХ узлов —
// иначе на длинной вертикальной укладке такая линия неизбежно проходит
// ЗА множеством узлов на своём пути и становится практически невидимой
// (ровно так и было: у первого запроса ответ оказался рядом с началом
// диалога, почти без препятствий на линии, а у второго/третьего линия
// пряталась под стопкой промежуточных узлов). "sequence" (next между
// ходами) — сюда же: это хребет всей диаграммы, ход процесса, им тоже
// нужна гарантированная видимость.
const HIGHLIGHT_TYPES = new Set(["answers", "next_request", "resolves", "sequence"]);

// "next" между ходами дополнительно рисуется КОНТУРНО — сначала более
// толстая белая "подложка", поверх неё обычная цветная линия. Это, а не
// просто увеличенная толщина, и выделяет её на фоне остальных рёбер
// (тонкие серые/цветные линии рядом не спутать с хребтом процесса, даже
// когда они физически пересекаются).
const OUTLINE_TYPES = new Set(["sequence"]);

// Линия рисуется не от центра до центра узла, а обрезается по границе
// прямоугольника — иначе наконечник стрелки (marker-end) оказывается
// внутри узла, под его непрозрачной заливкой, и физически не виден.
function clipToBoxEdge(cx, cy, towardX, towardY, w, h) {
  const dx = towardX - cx, dy = towardY - cy;
  if (dx === 0 && dy === 0) return {x: cx, y: cy};
  const sx = dx !== 0 ? (w / 2) / Math.abs(dx) : Infinity;
  const sy = dy !== 0 ? (h / 2) / Math.abs(dy) : Infinity;
  const s = Math.min(sx, sy, 1);
  return {x: cx + dx * s, y: cy + dy * s};
}

DATA.edges.forEach((e, idx) => {
  const p1 = positions[e.source], p2 = positions[e.target];
  if (!p1 || !p2) return;
  const isHl = HIGHLIGHT_TYPES.has(e.type);
  const start = clipToBoxEdge(p1.x, p1.y, p2.x, p2.y, NODE_W, NODE_H);
  const end = clipToBoxEdge(p2.x, p2.y, p1.x, p1.y, NODE_W, NODE_H);
  const targetLayer = isHl ? edgesFg : edgesBg;
  if (OUTLINE_TYPES.has(e.type)) {
    const outline = document.createElementNS("http://www.w3.org/2000/svg", "path");
    outline.setAttribute("class", "edge-outline");
    outline.setAttribute("d", `M ${start.x} ${start.y} L ${end.x} ${end.y}`);
    targetLayer.appendChild(outline);
  }
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("class", "edge type-" + e.type);
  path.setAttribute("data-idx", idx);
  path.setAttribute("d", `M ${start.x} ${start.y} L ${end.x} ${end.y}`);
  // marker-end ставится явным атрибутом, а не только через CSS-класс —
  // на некоторых браузерах (Safari в частности) CSS marker-end на SVG
  // применяется ненадёжно.
  path.setAttribute("marker-end", isHl ? "url(#arrow-hl)" : "url(#arrow)");
  targetLayer.appendChild(path);
  if (e.label) {
    const lbl = document.createElementNS("http://www.w3.org/2000/svg", "text");
    lbl.setAttribute("class", "edge-label edge-label-" + e.type);
    lbl.setAttribute("x", (start.x + end.x) / 2);
    lbl.setAttribute("y", (start.y + end.y) / 2);
    lbl.textContent = e.label;
    targetLayer.appendChild(lbl);
  }
});

DATA.nodes.forEach(n => {
  const p = positions[n.id];
  if (!p) return;
  const g = document.createElementNS("http://www.w3.org/2000/svg", "g");
  g.setAttribute("class", "node");
  g.setAttribute("data-id", n.id);
  g.setAttribute("transform", `translate(${p.x - NODE_W/2}, ${p.y - NODE_H/2})`);
  const rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  rect.setAttribute("width", NODE_W); rect.setAttribute("height", NODE_H);
  rect.setAttribute("rx", 6);
  rect.setAttribute("fill", categoryColor(n.category));
  g.appendChild(rect);
  const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
  text.setAttribute("x", NODE_W/2); text.setAttribute("y", NODE_H/2 + 4);
  text.setAttribute("text-anchor", "middle");
  text.textContent = n.label.length > 22 ? n.label.slice(0, 20) + "…" : n.label;
  g.appendChild(text);
  g.addEventListener("click", () => showDetail(n));
  nodesLayer.appendChild(g);
});

function showDetail(n) {
  document.querySelectorAll(".node.highlight").forEach(el => el.classList.remove("highlight"));
  const el = document.querySelector(`.node[data-id="${CSS.escape(n.id)}"]`);
  if (el) el.classList.add("highlight");
  const panel = document.getElementById("panel");

  // item.file может быть null (raw part-файл не нашёлся ни в .yaml, ни в
  // .json — например, дамп неполный) — тогда просто текст без ссылки,
  // а не битый <a href="null">.
  const link = (item) => item.file
    ? `<a href="${encodeURI(item.file)}" target="_blank" rel="noopener">${escXml(item.label)}</a>`
    : escXml(item.label);
  const linkLi = (item) => `<li>${link(item)}</li>`;

  let metaHtml = "";
  if (n.detail.meta_links) {
    // Заголовок хода: ссылки на ИСХОДНЫЕ (не извлечённые) openai_body/
    // fetch_raw файлы этого хода целиком.
    metaHtml = `<div class="meta">${n.detail.meta_links.map(link).join("  →  ")}</div>`;
  } else if (n.detail.meta) {
    metaHtml = `<div class="meta">${escXml(n.detail.meta)}</div>`;
  }

  let bodyHtml;
  if (n.detail.composition) {
    // Ход — единственный СОСТАВНОЙ узел: каждая составляющая — реальный
    // файл на диске (см. artifact_filename() на стороне Python), поэтому
    // вместо простого текста рисуем список гиперссылок, открывающихся в
    // новой вкладке — сам артефакт со всем содержимым, а не только его
    // имя, как раньше.
    bodyHtml = '<div class="section-label">Состав запроса:</div><ul class="file-list">' +
      n.detail.composition.map(linkLi).join("") + '</ul>';
    if (n.detail.reasoning) {
      bodyHtml += '<div class="section-label">Производит (напрямую):</div><ul class="file-list">' +
        linkLi(n.detail.reasoning) + '</ul>';
    } else {
      bodyHtml += '<div class="section-label">Производит (напрямую): —</div>';
    }
  } else {
    // Обычный (не составной) узел артефакта — ссылка на его же файл
    // сверху, затем содержимое как раньше.
    const selfLink = n.detail.file
      ? `<div class="file-list" style="margin-bottom:8px">${link({label: "Открыть файл " + n.detail.file, file: n.detail.file})}</div>`
      : "";
    bodyHtml = selfLink + `<pre>${escXml(n.detail.text)}</pre>`;
  }

  panel.innerHTML = `<h2>${escXml(n.detail.title)}</h2>` + metaHtml + bodyHtml;
}

// === Pan / Zoom ===
let viewBox = {x: 0, y: 0, w: 1000, h: 1000};
(function initViewBox() {
  const xs = Object.values(positions).map(p => p.x), ys = Object.values(positions).map(p => p.y);
  if (!xs.length) return;
  const minX = Math.min(...xs) - 150, maxX = Math.max(...xs) + 150;
  const minY = Math.min(...ys) - 100, maxY = Math.max(...ys) + 100;
  viewBox = {x: minX, y: minY, w: maxX - minX, h: maxY - minY};
})();
function applyViewBox() {
  svg.setAttribute("viewBox", `${viewBox.x} ${viewBox.y} ${viewBox.w} ${viewBox.h}`);
}
applyViewBox();

const wrap = document.getElementById("canvas-wrap");
wrap.addEventListener("wheel", (ev) => {
  ev.preventDefault();
  const scale = ev.deltaY > 0 ? 1.1 : 0.9;
  const rect = wrap.getBoundingClientRect();
  const mx = viewBox.x + (ev.clientX - rect.left) / rect.width * viewBox.w;
  const my = viewBox.y + (ev.clientY - rect.top) / rect.height * viewBox.h;
  viewBox.x = mx - (mx - viewBox.x) * scale;
  viewBox.y = my - (my - viewBox.y) * scale;
  viewBox.w *= scale; viewBox.h *= scale;
  applyViewBox();
}, {passive: false});

let dragging = false, lastX = 0, lastY = 0;
wrap.addEventListener("mousedown", (ev) => {
  dragging = true; lastX = ev.clientX; lastY = ev.clientY; wrap.classList.add("dragging");
});
window.addEventListener("mouseup", () => { dragging = false; wrap.classList.remove("dragging"); });
window.addEventListener("mousemove", (ev) => {
  if (!dragging) return;
  const rect = wrap.getBoundingClientRect();
  viewBox.x -= (ev.clientX - lastX) / rect.width * viewBox.w;
  viewBox.y -= (ev.clientY - lastY) / rect.height * viewBox.h;
  lastX = ev.clientX; lastY = ev.clientY;
  applyViewBox();
});

</script>
</body>
</html>
"""


def render_html(model: dict, layout, out_path: str) -> None:
    """Пишет ОДИН самодостаточный HTML-файл: данные встроены прямо в
    документ (не подгружаются отдельным fetch — это принципиально, иначе
    открытие через file:// упрётся в CORS-блокировку локальных запросов),
    внешних библиотек нет вовсе. Открывается двойным кликом в любом
    браузере, сервер не нужен."""
    nodes = model["nodes"]
    if layout:
        positions = layout["positions"]
        height = layout["height"]
        node_payload = []
        for nid, n in nodes.items():
            x, y = positions.get(nid, (None, None))
            entry = {"id": nid, "label": n["label"], "category": n["category"], "detail": n["detail"]}
            if x is not None:
                entry["x"] = x
                entry["y"] = height - y  # переворот: у dot Y растёт вверх, в SVG — вниз
            node_payload.append(entry)
        has_layout = True
    else:
        node_payload = [{"id": nid, "label": n["label"], "category": n["category"], "detail": n["detail"]}
                         for nid, n in nodes.items()]
        has_layout = False

    payload = {"nodes": node_payload, "edges": model["edges"], "hasLayout": has_layout}
    html = HTML_TEMPLATE.replace("__DATA_JSON__", json.dumps(payload, ensure_ascii=False))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
