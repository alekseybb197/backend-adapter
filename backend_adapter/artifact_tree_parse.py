"""artifact_tree_parse — обнаружение/загрузка частей, извлечение артефактов, классификация."""

import json
import os
import re

from .artifact_tree_common import (
    PART_RE,
    extract_message_text,
    tool_call_text,
)
from .artifact_tree_registry import ArtifactRegistry

# ==================== ЗАГРУЗКА ЧАСТЕЙ ====================


def discover_parts(parts_dir: str):
    """Возвращает {'openai_body': [(part_id:int, path)], 'fetch_raw': [...]}"""
    found: dict[str, list] = {"openai_body": [], "fetch_raw": []}
    for fname in os.listdir(parts_dir):
        m = PART_RE.match(fname)
        if not m:
            continue
        found[m.group("type")].append((int(m.group("id")), os.path.join(parts_dir, fname)))
    found["openai_body"].sort()
    found["fetch_raw"].sort()
    return found


def load_json(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ==================== ИЗВЛЕЧЕНИЕ АРТЕФАКТОВ ИЗ ОДНОЙ ЧАСТИ ====================


def process_openai_body(ob: dict, part_id: str, registry: ArtifactRegistry, resolution_edges: list):
    """Регистрирует артефакты входа этого запроса, возвращает упорядоченный
    список имён — это и есть композиция запроса из уже известных частей.
    Дополнительно (п.5) пополняет resolution_edges парами
    (toolcall_name, tool_result_name) там, где role:"tool" сообщение несёт
    tool_call_id, для которого уже известен породивший его toolcall-артефакт."""
    names = []
    for msg in ob.get("messages", []):
        role = msg.get("role")
        if role == "system":
            text = extract_message_text(msg.get("content"))
            if text.strip():
                names.append(registry.register("system", text, part_id))
        elif role == "user":
            text = extract_message_text(msg.get("content"))
            if text.strip():
                names.append(registry.register("user", text, part_id))
        elif role == "tool":
            text = extract_message_text(msg.get("content"))
            if text.strip():
                name = registry.register("tool_result", text, part_id)
                names.append(name)
                caller_name = registry.artifact_for_protocol_id(msg.get("tool_call_id"))
                if caller_name:
                    resolution_edges.append((caller_name, name))
        elif role == "assistant":
            # Сообщение-ЭХО прошлого ответа модели, растиражированное в
            # растущей истории. То, что протокол называет ролью "assistant",
            # мы намеренно НЕ складываем в один общий домен "assistant", а
            # раскладываем на три более информативных под-домена, зеркально
            # структуре самого сообщения модели: toolcall (решение вызвать
            # инструмент + аргументы), response (свободный текст — финальный
            # или, как здесь, текст-преамбула перед вызовом инструмента) и,
            # отдельно через process_fetch_raw, reasoning (chain-of-thought,
            # которого в этом эхе всё равно уже нет — см. докстринг).
            #
            # Здесь мы НЕ регистрируем эти артефакты заново — ищем уже
            # известные (они должны были появиться раньше через
            # process_fetch_raw, в момент своего рождения). Если почему-то
            # не найдены (например, окно дампов не захватывает самое начало
            # сессии) — регистрируем здесь как fallback.
            for tc in msg.get("tool_calls", []) or []:
                text = tool_call_text(tc)
                name = registry.name_for(text) or registry.register("toolcall", text, part_id)
                registry.link_protocol_id(
                    tc.get("id"), name
                )  # на случай, если это первое появление в видимом окне
                names.append(name)
            # ВАЖНО: текст регистрируется НЕЗАВИСИМО от наличия tool_calls —
            # предыдущая версия теряла текст-преамбулу целиком, если в том
            # же сообщении assistant одновременно и говорил что-то, и
            # вызывал инструмент (условие `and not tool_calls` отбрасывало
            # такой текст без следа, ни как toolcall, ни как response).
            content_text = extract_message_text(msg.get("content"))
            if content_text.strip():
                name = registry.name_for(content_text) or registry.register(
                    "response", content_text, part_id
                )
                names.append(name)
        else:
            text = extract_message_text(msg.get("content"))
            if text.strip():
                names.append(registry.register("other", text, part_id))
    return names


def process_fetch_raw(fr: dict, part_id: str, registry: ArtifactRegistry):
    """Регистрирует артефакты, ПРОИЗВЕДЁННЫЕ этим ответом бэкенда. Возвращает
    {"reasoning_name": str|None, "decision_names": [str, ...]} — разделяем
    сознательно (п.1): reasoning логически ПРЕДШЕСТВУЕТ и ПРИВОДИТ К решению
    (тексту ответа или вызову инструмента), это не два независимых
    производных одного хода, а причина и следствие. Порядок decision_names
    сохраняет порядок появления в content/tool_calls."""
    choices = fr.get("choices") or [{}]
    msg = choices[0].get("message", {})
    reasoning_name = None
    reasoning = msg.get("reasoning_content") or ""
    if reasoning.strip():
        reasoning_name = registry.register("reasoning", reasoning, part_id)

    decision_names = []
    for tc in msg.get("tool_calls", []) or []:
        name = registry.register("toolcall", tool_call_text(tc), part_id)
        registry.link_protocol_id(
            tc.get("id"), name
        )  # п.5: запоминаем id для будущей связки с tool_result
        decision_names.append(name)
    content = msg.get("content") or ""
    if content.strip():
        decision_names.append(registry.register("response", content, part_id))

    return {"reasoning_name": reasoning_name, "decision_names": decision_names}


# ==================== КЛАССИФИКАЦИЯ ====================


def classify_kind(ob: dict) -> str:
    return "agent_turn" if ob.get("tools") else "structured_output"


def fetch_raw_has_tool_calls(fr: dict) -> bool:
    choices = fr.get("choices") or [{}]
    return bool(choices[0].get("message", {}).get("tool_calls"))


def looks_like_structured_output(content: str) -> bool:
    """structured_output-сайдкар (генерация заголовка) отвечает голым JSON —
    ЛИБО объектом вида {"title": "..."} (иногда обёрнутым в ```json ... ```),
    ЛИБО (как выяснилось на реальных данных) просто голой JSON-строкой вида
    "Файлы в текущей папке" без обёртки-объекта вообще — тот же сайдкар,
    просто бэкенд в этот раз вернул заголовок не через объект, а как
    строковый литерал. Раньше распознавался только первый вариант, из-за
    чего "строковые" title-ответы ошибочно классифицировались как
    agent_turn, ложно занимая чужой pending-слот в очереди сопоставления и
    запуская каскад неверных матчей вплоть до конца сессии (обычный markdown-
    ответ агента, для сравнения, никогда не является валидным JSON — ни
    объектом, ни строкой — так что риска спутать его с этим нет).
    Обычный финальный ответ агента — это markdown-текст, не JSON."""
    text = content.strip()
    # снимаем ```json ... ``` обёртку, если есть
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        obj = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        obj = None
    if obj is None:
        # объект может быть не всем content целиком, а с шумом вокруг —
        # старый запасной путь: вырезать { ... } по крайним скобкам.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return False
        try:
            obj = json.loads(text[start : end + 1])
        except (json.JSONDecodeError, TypeError):
            return False
    if isinstance(obj, dict):
        return 1 <= len(obj) <= 3
    if isinstance(obj, str):
        return bool(obj) and "\n" not in obj and len(obj) <= 200
    return False


def determine_fetch_raw_kind(fr: dict) -> str:
    if fetch_raw_has_tool_calls(fr):
        return "agent_turn"
    choices = fr.get("choices") or [{}]
    content = choices[0].get("message", {}).get("content") or ""
    return "structured_output" if looks_like_structured_output(content) else "agent_turn"


def compute_inline_labels(registry: ArtifactRegistry) -> dict:
    """Определяет, какие артефакты доменов user/system НЕ показываются на
    диаграмме отдельным узлом вовсе, а вписываются прямо в список "состав
    запроса" ходового блока как "<имя>:<суффикс>". Возвращает
    {artifact_name: суффикс}.

    Правила (по договорённости):
      - user, содержимое начинается с тега <system-reminder> — это не
        реальный ввод человека, а харнесс-инжекция (CLAUDE.md, git-статус,
        список скиллов и т.п.) -> суффикс "CLAUDE.md". Единственный user-
        артефакт БЕЗ этого тега (в частности, самый ранний, к которому
        ведёт Start) отдельным узлом остаётся.
      - system — ровно два наблюдаемых типа:
          содержит "# Harness" -> суффикс "Workflow" (основной системный
              промпт харнесса: описывает Harness/Memory/Environment и т.д.)
          иначе, если содержит "<session>" -> суффикс "Session" (system-
              промпт structured_output-сайдкара генерации заголовка)
        Проверка порядка ("# Harness" раньше "<session>") важна: у
        "Workflow"-типа тоже может тегом что-то похожее встретиться, но
        именно "# Harness" — надёжный, специфичный маркер этого типа.
        Если content не подходит ни под один маркер (неизвестный третий
        тип system-промпта) — не классифицируем, оставляем обычным узлом,
        чтобы не потерять то, что мы ещё не научились узнавать.
    """
    labels = {}
    for entry in registry.by_hash.values():
        name, domain, text = entry["name"], entry["domain"], entry["text"]
        if domain == "user" and text.lstrip().startswith("<system-reminder>"):
            labels[name] = "CLAUDE.md"
        elif domain == "system":
            if "# Harness" in text:
                labels[name] = "Workflow"
            elif "<session>" in text:
                labels[name] = "Session"
    return labels
