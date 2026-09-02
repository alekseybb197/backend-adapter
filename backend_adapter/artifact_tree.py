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

# === Логика связывания openai_body <-> fetch_raw в "ходы" (turns) ===
# В самих файлах НЕТ явного поля, связывающего конкретный fetch_raw с породившим
# его openai_body (это HTTP request/response одного вызова, но дамп не хранит
# req_id внутри содержимого). Также, как мы обсуждали, разные "нити" одной
# сессии (основной agent_turn и, например, параллельный structured_output-
# сайдкар генерации заголовка) МОГУТ идти конкурентно, и их part_id-нумерация
# перемежается по факту прихода, а не по логической принадлежности.
#
# Используется чисто содержательная эвристика:
#   1. Каждый openai_body классифицируется по kind:
#        "agent_turn"       — есть непустой tools[]
#        "structured_output"— tools[] пуст
#   2. Каждый fetch_raw классифицируется по ФОРМЕ ответа: если есть
#      tool_calls — точно "agent_turn"; если ответ — голый JSON вида
#      {"title": "..."} (со снятием ```json обёртки при наличии) — это
#      "structured_output"; иначе — "agent_turn". Определять kind по
#      порядку/приоритету очередей, а не по форме ответа, оказалось
#      ошибкой — так ответ на сайдкар генерации заголовка был один раз
#      неверно приписан соседнему agent_turn-ходу просто потому, что тот
#      был добавлен в очередь позже.
#   3. Сопоставление — жадное: очередной fetch_raw берёт САМЫЙ СВЕЖИЙ ещё не
#      сопоставленный openai_body своего kind (LIFO); если такого нет —
#      пробует другой kind как запасной вариант.
#   4. Если подходящего кандидата вообще не нашлось (нить началась до начала
#      видимого окна дампов) — fetch_raw помечается как orphan и получает в
#      дереве отдельный, явно подписанный узел-заглушку, а не тихо
#      приписывается к чужому ходу.
#
# ЭТО ЭВРИСТИКА, не гарантированно точная в сложных случаях (несколько
# ПАРАЛЛЕЛЬНЫХ agent_turn-нитей одного kind одновременно, например от
# sub-agent'ов Task-тула). Для точной причинности используйте параллельно
# JSONL trace-лог адаптера (там она есть явно, через parent_req_id) — см.
# backend-adapter.v0.3.0.py и trace_stats.py из этого же обсуждения.
#
# === Вывод (всегда внутри <parts-dir>/artefacts/, никаких вложенных
#     поддиректорий) ===
#   artefacts/<domain>-<part_id>.yaml  — все уникальные артефакты (YAML: ключи
#                                         domain/first_seen_part_id/sha256_*
#                                         + content с исходным текстом;
#                                         требует PyYAML, иначе .txt-фолбэк)
#   artefacts/tree.puml                — PlantUML-исходник дерева
#   artefacts/tree.png                 — рендер (настоящий PlantUML, если
#                                         есть локальный `plantuml`+Java;
#                                         иначе — Graphviz-fallback той же
#                                         структуры, с явной пометкой)

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections import defaultdict, OrderedDict

# Модульный логгер — БЕЗ настройки хендлера/формата здесь. Этот файл может
# быть импортирован как библиотека (см. session_viewer.py, который вызывает
# generate() программно) — настраивать root-логгер из библиотечного модуля
# считается плохой практикой в Python: формат (в частности, единый префикс
# с датой/временем, который и был целью) задаётся ОДИН раз, в entry point
# того, кто реально запускается (адаптер или session_viewer.py вручную).
# Внутри пакета формат логов наследуется от адаптера (backend-adapter.py).
logger = logging.getLogger("artifact_tree")

try:
    import yaml
    YAML_AVAILABLE = True

    def _yaml_str_representer(dumper, data):
        # Многострочные значения (reasoning, tool_result, response и т.п.)
        # читаются заметно удобнее как block scalar (content: |), чем как
        # однострочная кавычная строка с буквальными \n внутри — при этом
        # содержимое остаётся тем же самым текстом, программный разбор через
        # yaml.safe_load не меняется.
        style = "|" if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    yaml.add_representer(str, _yaml_str_representer, Dumper=yaml.SafeDumper)
except ImportError:
    YAML_AVAILABLE = False

PART_RE = re.compile(r'^(?P<session>[0-9a-f]+)-(?P<id>\d+)-(?P<type>openai_body|fetch_raw)\.json$')

# Волатильные вставки, которые меняются от хода к ходу без изменения
# содержательного смысла артефакта (см. докстринг выше про
# <total_tokens>...tokens left</total_tokens>). ВЫРЕЗАЮТСЯ целиком (не
# заменяются плейсхолдером) перед вычислением ключа дедупликации — только
# для СРАВНЕНИЯ, исходный текст первого появления сохраняется как есть.
# Список осознанно небольшой и явный: расширяйте по мере обнаружения новых
# похожих случаев, не пытайтесь угадать все волатильные паттерны заранее.
VOLATILITY_PATTERNS = [
    re.compile(r'<total_tokens>\d+ tokens left</total_tokens>\n?'),
]


def normalize_for_dedup(text: str) -> str:
    normalized = text
    for pat in VOLATILITY_PATTERNS:
        normalized = pat.sub('', normalized)
    return normalized.rstrip()


def sha12(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def strip_trailing_line_whitespace(text: str) -> str:
    """PyYAML молча отказывается от читаемого block-scalar стиля (content: |)
    и откатывается на однострочную кавычную запись с буквальными \\n, если
    ХОТЯ БЫ В ОДНОЙ строке есть пробел/таб в конце — так эмиттер library
    подстраховывается от потери этого пробела при повторном парсинге
    (сам YAML это умеет, но эмиттер PyYAML не рискует). У модели в
    reasoning такие висячие пробелы перед переносом строки — чистый шум,
    не несущий смысла ("Done. \\n"), поэтому убираем только его, не трогая
    сам текст. Пустые строки (только из пробелов) тоже нормализуются."""
    return "\n".join(line.rstrip(" \t") for line in text.split("\n"))


# ==================== ИЗВЛЕЧЕНИЕ ТЕКСТА ИЗ СООБЩЕНИЙ ====================

def extract_message_text(content) -> str:
    """OpenAI chat-completions content обычно строка, но на всякий случай
    поддерживаем и блочный формат (список частей)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(json.dumps(block, ensure_ascii=False))
        return "\n".join(parts)
    return str(content)


def tool_call_text(tc: dict) -> str:
    """Канонический текст вызова инструмента для дедупликации/сравнения.
    ВАЖНО: harness при эхо-повторе решения модели в следующем openai_body
    пересериализует JSON внутри поля "arguments" СО СВОИМИ пробелами
    (замечено на реальных данных: бэкенд отдаёт компактно
    '{"command":"ls -la",...}', а харнесс в эхе — с пробелами
    '{"command": "ls -la", ...}') — та же самая по смыслу строка становится
    байтово другой. Раньше это ломало дедупликацию по контенту: эхо
    регистрировалось как НОВЫЙ, отдельный toolcall-артефакт и, что хуже,
    через link_protocol_id ПЕРЕЗАПИСЫВАЛО правильную связь protocol_id ->
    исходный toolcall на этот фантомный дубль — из-за чего настоящий вызов
    оставался без найденного tool_result (казался "оборванным"), а фантом
    получал чужое разрешение и "возникал из ниоткуда" на диаграмме (не имея
    входящего ребра reasoning->toolcall, только исходящее resolves).
    Поэтому parse+re-dump "arguments" КАНОНИЧЕСКИ (без лишних пробелов,
    отсортированные ключи), а не берём как есть."""
    func = tc.get("function", {})
    args_raw = func.get("arguments", "{}")
    try:
        args_canonical = json.dumps(json.loads(args_raw), ensure_ascii=False, sort_keys=True)
    except (json.JSONDecodeError, TypeError):
        args_canonical = args_raw  # не смогли распарсить — используем как есть
    return json.dumps({"name": func.get("name"), "arguments": args_canonical},
                       ensure_ascii=False, sort_keys=True)


# ==================== РЕЕСТР АРТЕФАКТОВ (ДЕДУПЛИКАЦИЯ) ====================

class ArtifactRegistry:
    def __init__(self):
        self.by_hash = OrderedDict()   # sha(normalized) -> {domain, name, first_part_id, text}
        self._name_counts = defaultdict(int)
        # Связь протокольного tool_call id (chatcmpl-tool-XXXX) -> имя
        # артефакта toolcall. Нужна для п.5: role:"tool" сообщение хранит
        # tool_call_id, а не текст вызова — только через эту связку можно
        # провести стрелку toolcall -> tool_result.
        self.protocol_id_to_artifact = {}

    def register(self, domain: str, text: str, part_id: str) -> str:
        text = strip_trailing_line_whitespace(text)
        h = sha12(normalize_for_dedup(text))
        if h in self.by_hash:
            return self.by_hash[h]["name"]
        base_name = f"{domain}-{part_id}"
        name = base_name
        if self._name_counts[base_name] > 0:
            name = f"{base_name}-{self._name_counts[base_name]}"
        self._name_counts[base_name] += 1
        self.by_hash[h] = {"domain": domain, "name": name, "first_part_id": part_id, "text": text}
        return name

    def name_for(self, text: str):
        return self.by_hash.get(sha12(normalize_for_dedup(text)), {}).get("name")

    def link_protocol_id(self, protocol_id: str, artifact_name: str):
        """Запоминает, что 'сырой' id вызова инструмента (chatcmpl-tool-...,
        уникальный per-issuance) соответствует данному (возможно,
        дедуплицированному по содержимому!) артефакту toolcall. ВАЖНО: если
        два РАЗНЫХ issuance (два разных protocol_id, например от повторного
        идентичного запроса) породили БУКВАЛЬНО одинаковый по содержимому
        tool_call, они схлопнутся в ОДИН артефакт toolcall-* — оба
        protocol_id будут указывать на одно и то же имя. Это осознанный
        побочный эффект дедупликации по содержимому, а не потеря данных:
        сами по себе два разных "Ход"-узла всё равно видны на диаграмме как
        два независимых источника, ведущих в один и тот же toolcall-узел."""
        if protocol_id:
            self.protocol_id_to_artifact[protocol_id] = artifact_name

    def artifact_for_protocol_id(self, protocol_id: str):
        return self.protocol_id_to_artifact.get(protocol_id)

    def write_all(self, out_dir: str):
        os.makedirs(out_dir, exist_ok=True)
        if not YAML_AVAILABLE:
            print("[WARN] PyYAML не установлен (pip install pyyaml --break-system-packages) — "
                  "артефакты сохранены как .txt со старой шапкой-комментарием вместо .yaml.",
                  file=sys.stderr)
        for entry in self.by_hash.values():
            if YAML_AVAILABLE:
                path = os.path.join(out_dir, f"{entry['name']}.yaml")
                # Все поля прежней текстовой шапки — теперь обычные YAML-ключи,
                # плюс отдельный ключ content для исходного текста артефакта.
                # sort_keys=False — чтобы порядок остался читаемым (сначала
                # метаданные, потом content), а не алфавитным.
                data = {
                    "domain": entry["domain"],
                    "first_seen_part_id": entry["first_part_id"],
                    "sha256_raw": sha12(entry["text"]),
                    "sha256_normalized": sha12(normalize_for_dedup(entry["text"])),
                    "content": entry["text"],
                }
                with open(path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False,
                                    default_flow_style=False, width=100000)
            else:
                path = os.path.join(out_dir, f"{entry['name']}.txt")
                with open(path, "w", encoding="utf-8") as f:
                    f.write(f"# domain: {entry['domain']}\n")
                    f.write(f"# first_seen_part_id: {entry['first_part_id']}\n")
                    f.write(f"# sha256[:12] (raw): {sha12(entry['text'])}\n")
                    f.write(f"# sha256[:12] (normalized, use for dedup identity): "
                            f"{sha12(normalize_for_dedup(entry['text']))}\n")
                    f.write("# ---\n")
                    f.write(entry["text"])


# ==================== ЗАГРУЗКА ЧАСТЕЙ ====================

def discover_parts(parts_dir: str):
    """Возвращает {'openai_body': [(part_id:int, path)], 'fetch_raw': [...]}"""
    found = {"openai_body": [], "fetch_raw": []}
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
                registry.link_protocol_id(tc.get("id"), name)  # на случай, если это первое появление в видимом окне
                names.append(name)
            # ВАЖНО: текст регистрируется НЕЗАВИСИМО от наличия tool_calls —
            # предыдущая версия теряла текст-преамбулу целиком, если в том
            # же сообщении assistant одновременно и говорил что-то, и
            # вызывал инструмент (условие `and not tool_calls` отбрасывало
            # такой текст без следа, ни как toolcall, ни как response).
            content_text = extract_message_text(msg.get("content"))
            if content_text.strip():
                name = registry.name_for(content_text) or registry.register("response", content_text, part_id)
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
        registry.link_protocol_id(tc.get("id"), name)  # п.5: запоминаем id для будущей связки с tool_result
        decision_names.append(name)
    content = msg.get("content") or ""
    if content.strip():
        decision_names.append(registry.register("response", content, part_id))

    return {"reasoning_name": reasoning_name, "decision_names": decision_names}


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
    agent_turn, ложно занимали чужой pending-слот в очереди сопоставления и
    запускали каскад неверных пар вплоть до конца сессии (обычный markdown-
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
            obj = json.loads(text[start:end + 1])
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


# ==================== СВЯЗЫВАНИЕ openai_body <-> fetch_raw В ХОДЫ ====================

def build_turns(parts_dir: str, registry: ArtifactRegistry):
    found = discover_parts(parts_dir)

    merged = sorted(
        [("ob", pid, path) for pid, path in found["openai_body"]] +
        [("fr", pid, path) for pid, path in found["fetch_raw"]],
        key=lambda t: t[1]
    )

    resolution_edges = []  # п.5: [(toolcall_name, tool_result_name), ...]
    ob_records = []   # {part_id, kind, input_names, raw_file}
    fr_records = []   # {part_id, kind, reasoning_name, decision_names, raw_file}
    for etype, part_id, path in merged:
        data = load_json(path)
        # Имя ИСХОДНОГО part-файла (в .yaml, а не в .json — так же, как
        # адаптер и так пишет оба варианта рядом) — для гиперссылки из
        # панели хода на "сырой" дамп в папке сессии, а не только на уже
        # извлечённые артефакты в artefacts/. Берём basename не самого
        # path (может быть .json), а его yaml-соседа по имени.
        raw_file = os.path.splitext(os.path.basename(path))[0] + ".yaml"
        if etype == "ob":
            kind = classify_kind(data)
            names = process_openai_body(data, str(part_id), registry, resolution_edges)
            ob_records.append({"part_id": part_id, "kind": kind, "input_names": names, "raw_file": raw_file})
        else:
            kind = determine_fetch_raw_kind(data)
            # tool_calls в ответе — ТОЧНЫЙ, а не эвристический признак: раз
            # они есть, это определённо agent_turn (structured_output-
            # запрос отправляется вообще без tools[], бэкенду физически
            # неоткуда взять tool_call на него). В отличие от классификации
            # по форме текста (looks_like_structured_output — тоже эвристика,
            # уже дважды чинили) здесь fallback на другой kind в матчинге
            # ниже не должен разрешаться вообще — иначе есть шанс отдать
            # реальный tool_call чужому 0-tools запросу (ровно так один раз
            # и произошло на реальных данных).
            definite = fetch_raw_has_tool_calls(data)
            fr_out = process_fetch_raw(data, str(part_id), registry)
            fr_records.append({"part_id": part_id, "kind": kind, "definite": definite, "raw_file": raw_file, **fr_out})

    # Жадное сопоставление: очередь непогашенных openai_body на каждый kind
    pending = {"agent_turn": [], "structured_output": []}
    turns = []
    orphans = []

    all_events = sorted(
        [("ob", r["part_id"], r) for r in ob_records] +
        [("fr", r["part_id"], r) for r in fr_records],
        key=lambda t: t[1]
    )

    for etype, part_id, rec in all_events:
        if etype == "ob":
            pending[rec["kind"]].append(rec)
        else:
            # Определённый по СОДЕРЖИМОМУ kind этого fetch_raw — сначала
            # ищем непогашенный openai_body ТОГО ЖЕ kind (наиболее свежий,
            # LIFO). Если такого нет (нить началась до окна дампов, либо
            # kind определён неверно из-за нетипичного ответа) — как
            # запасной вариант пробуем другой kind, ТОЛЬКО если исходная
            # классификация была эвристической (по форме текста), а не
            # точной (по наличию tool_calls) — см. комментарий выше.
            kind = rec["kind"]
            if rec["definite"]:
                search_order = [kind]
            else:
                search_order = [kind] + [k for k in ("agent_turn", "structured_output") if k != kind]
            match_kind = next((k for k in search_order if pending[k]), None)
            if match_kind is None:
                orphans.append(rec)
                continue
            ob_rec = pending[match_kind].pop()  # LIFO — самый свежий
            turns.append({
                "ob_part_id": ob_rec["part_id"],
                "fr_part_id": rec["part_id"],
                "kind": match_kind,
                "input_names": ob_rec["input_names"],
                "reasoning_name": rec["reasoning_name"],
                "decision_names": rec["decision_names"],
                "ob_raw_file": ob_rec["raw_file"],
                "fr_raw_file": rec["raw_file"],
            })

    turns.sort(key=lambda t: t["ob_part_id"])

    # Дедупликация resolution_edges: тот же tool_result (тот же tool_call_id)
    # ретранслируется в КАЖДОМ последующем openai_body по мере роста истории
    # (тот же эффект, что мы уже разбирали для JSONL-трейса адаптера) — без
    # дедупликации одна и та же связка toolcall->tool_result задваивалась бы
    # на каждый ход, где она просто "проезжает" в контексте.
    resolution_edges = list(dict.fromkeys(resolution_edges))

    # system-артефакты целиком классифицируются в inline_labels (Session/
    # Workflow) и вписываются в блок Ход — отдельного узла Harness для них
    # больше нет.
    inline_labels = compute_inline_labels(registry)

    # === Сегментация по РЕАЛЬНЫМ запросам пользователя ===
    # В сессии может быть НЕСКОЛЬКО настоящих обращений человека (не только
    # самое первое) — каждый неинлайненный user-артефакт (т.е. НЕ
    # "<system-reminder>"-инжекция) это отдельный, самостоятельный запрос.
    # Раньше это не учитывалось: единственный global Start указывал только
    # на самый ранний из них, а остальные повисали вообще без входящего
    # ребра ("орфанные" user-узлы); и единственный global Finish/Superseded
    # неверно считал ЛЮБОЙ не-последний финальный ответ "вытесненным
    # дублем" — что верно только ВНУТРИ одного запроса, но ошибочно
    # схлопывало ответы на РАЗНЫЕ последовательные запросы в одну кучу.
    #
    # Логика: сортируем реальные user-артефакты по part_id первого
    # появления — каждый открывает свой "сегмент" вплоть до part_id
    # следующего. Внутри сегмента отбираем agent_turn-ходы с финальным
    # текстом (response без сопутствующего toolcall, т.е. не промежуточный
    # ход) — последний по part_id это НАСТОЯЩИЙ ответ на этот запрос,
    # остальные (если есть) — вытеснены повторным запросом ВНУТРИ этого же
    # логического запроса (тот же эффект дублей, что мы уже находили).
    real_user_entries = sorted(
        (int(e["first_part_id"]), e["name"]) for e in registry.by_hash.values()
        if e["domain"] == "user" and e["name"] not in inline_labels
    )
    start_target = real_user_entries[0][1] if real_user_entries else None

    agent_turns = [t for t in turns if t["kind"] == "agent_turn"]
    final_text_turns = [
        t for t in agent_turns
        if any(n.startswith("response-") for n in t["decision_names"])
        and not any(n.startswith("toolcall-") for n in t["decision_names"])
    ]

    request_answers = {}       # user_artifact_name -> response, который на него отвечает
    superseded_targets = []
    boundaries = [pid for pid, _ in real_user_entries] + [float("inf")]
    for i, (pid, uname) in enumerate(real_user_entries):
        next_pid = boundaries[i + 1]
        segment = [t for t in final_text_turns if pid <= t["ob_part_id"] < next_pid]
        if not segment:
            continue  # запрос виден, но финального текстового ответа на него в окне дампов нет
        *earlier, last = segment
        request_answers[uname] = next(n for n in last["decision_names"] if n.startswith("response-"))
        for t in earlier:
            superseded_targets.append(next(n for n in t["decision_names"] if n.startswith("response-")))

    # Finish — ответ на САМЫЙ ПОЗДНИЙ по part_id реальный запрос (последнее
    # слово в сессии). Остальные запросы получают свою собственную стрелку
    # "answers" обратно к своему user-артефакту (см. рендер) — так видно,
    # чем закончился КАЖДЫЙ запрос, а не только сессия целиком.
    finish_source = None
    if real_user_entries:
        last_uname = real_user_entries[-1][1]
        finish_source = request_answers.get(last_uname)

    # "next request" рёбра: ответ на запрос i -> следующий реальный user-
    # артефакт (i+1). Заменяет прежний одиночный Start->единственный вход:
    # теперь видно, что второй и третий вопрос пользователя пришли ПОСЛЕ
    # ответа на предыдущий, в рамках одного и того же диалога, а не
    # появились ниоткуда.
    next_request_edges = []
    for i in range(len(real_user_entries) - 1):
        uname_i = real_user_entries[i][1]
        uname_next = real_user_entries[i + 1][1]
        answer_i = request_answers.get(uname_i)
        if answer_i:
            next_request_edges.append((answer_i, uname_next))

    title_targets = []
    for t in turns:
        if t["kind"] == "structured_output":
            title_targets += [n for n in t["decision_names"] if n.startswith("response-")]

    return (turns, orphans, resolution_edges, start_target, inline_labels, finish_source,
            superseded_targets, title_targets, request_answers, next_request_edges)


# ==================== РЕНДЕР PLANTUML ====================

DOMAIN_COLOR = {
    "system": "#FFF3CD", "user": "#D1ECF1", "tool_result": "#D4EDDA",
    "toolcall": "#E0CFFC", "reasoning": "#F8D7DA", "response": "#D6D8DB",
    "other": "#FFFFFF",
}
KIND_COLOR = {"agent_turn": "#CFE2FF", "structured_output": "#FFE5B4"}


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
        # user/system-артефакты, классифицированные как харнесс-инжекция
        # (CLAUDE.md-реминдер) или как один из двух типов system-промпта
        # (Session/Workflow), НЕ получают отдельного узла на диаграмме
        # вообще — вписываются прямо в состав хода этой короткой пометкой.
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

    # Явная связь "запрос -> ответ на НЕГО": для КАЖДОГО реального обращения
    # пользователя (их может быть несколько в одной сессии) рисуем
    # response --answers--> user-артефакт, к которому он относится — иначе
    # невозможно понять, чем именно завершился конкретный запрос, особенно
    # если их несколько подряд.
    for uname, response_name in request_answers.items():
        emit_artifact(response_name)
        emit_artifact(uname)
        lines.append(f'{puml_id(response_name)} -[#0066CC]-> {puml_id(uname)} : answers')

    # "next request": ответ на предыдущий запрос -> следующий реальный
    # user-артефакт, чтобы второй/третий вопрос пользователя не повисал без
    # единого входящего ребра, а был явно виден как продолжение диалога.
    for answer_name, next_uname in next_request_edges:
        emit_artifact(answer_name)
        emit_artifact(next_uname)
        lines.append(f'{puml_id(answer_name)} -[#CC6600]-> {puml_id(next_uname)} : next request')

    # П.4: три разные судьбы response вместо одного "уходит в никуда":
    #   Finish — реально показанный пользователю финальный ответ;
    #   Superseded — более ранние финальные ответы, вытесненные повторным
    #     (побайтово идентичным) запросом — та же природа дублей, что и
    #     с "повисшим" toolcall в прошлый раз;
    #   SessionTitle — ответы сайдкара генерации заголовка, у них свой
    #     законный потребитель вне основного диалога.
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
        subprocess.run([plantuml_bin, "-tpng", os.path.abspath(puml_path)],
                        check=True, capture_output=True, timeout=60)
        return True
    except Exception as e:
        print(f"[WARN] plantuml завершился с ошибкой: {e}", file=sys.stderr)
        return False


def render_png_via_graphviz_fallback(turns, orphans, resolution_edges, start_target, inline_labels,
                                      finish_source, superseded_targets, title_targets,
                                      request_answers, next_request_edges,
                                      out_path: str) -> bool:
    """Запасной рендер ТОЙ ЖЕ структуры через Graphviz, если локального
    PlantUML (Java) нет. Не является PlantUML-рендером — используется только
    чтобы иметь превью PNG в окружениях без Java/plantuml."""
    dot_bin = shutil.which("dot")
    if not dot_bin:
        return False
    lines = ["digraph tree {", "  rankdir=TB;", '  node [shape=box, style=filled, fontsize=10];']
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
        label_lines = [
            f"Ход {t['ob_part_id']} ({t['kind']})",
            f"openai_body {t['ob_part_id']} -> fetch_raw {t['fr_part_id']}",
            "----",
            "состав запроса:",
        ] + [composition_bullet(name) for name in t["input_names"]] + [
            "----",
            "производит:",
        ] + ([f"• {t['reasoning_name']}"] if t["reasoning_name"] else [])
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
        lines.append(f'  "{node}" [label="ОСИРОТЕВШИЙ fetch_raw {rec["part_id"]}", fillcolor="#FF6B6B", shape=ellipse];')
        all_output_names = ([rec["reasoning_name"]] if rec["reasoning_name"] else []) + rec["decision_names"]
        for name in all_output_names:
            emit(name)
            lines.append(f'  "{node}" -> "{name}" [label="produces"];')

    for caller_name, result_name in resolution_edges:
        emit(caller_name)
        emit(result_name)
        lines.append(f'  "{caller_name}" -> "{result_name}" [style=dashed, penwidth=2, color="#228833", label="resolves"];')

    for uname, response_name in request_answers.items():
        emit(response_name)
        emit(uname)
        lines.append(f'  "{response_name}" -> "{uname}" [color="#0066CC", penwidth=2.2, label="answers"];')

    for answer_name, next_uname in next_request_edges:
        emit(answer_name)
        emit(next_uname)
        lines.append(f'  "{answer_name}" -> "{next_uname}" [color="#CC6600", penwidth=2.2, label="next request"];')

    if finish_source:
        emit(finish_source)
        lines.append('  "Finish" [shape=circle, fillcolor="#FFFFFF"];')
        lines.append(f'  "{finish_source}" -> "Finish";')
    if superseded_targets:
        lines.append('  "Superseded" [fillcolor="#FFD6D6", label="Superseded\\l(вытеснено повторным запросом)\\l"];')
        for name in superseded_targets:
            emit(name)
            lines.append(f'  "{name}" -> "Superseded";')
    if title_targets:
        lines.append('  "SessionTitle" [fillcolor="#D6EAFF", label="SessionTitle\\l(заголовок сессии, UI)\\l"];')
        for name in title_targets:
            emit(name)
            lines.append(f'  "{name}" -> "SessionTitle";')

    lines.append("}")
    dot_src = "\n".join(lines)
    dot_file = out_path.replace(".png", ".fallback.dot")
    with open(dot_file, "w", encoding="utf-8") as f:
        f.write(dot_src)
    try:
        subprocess.run([dot_bin, "-Tpng", dot_file, "-o", out_path], check=True,
                        capture_output=True, timeout=60)
        return True
    except Exception as e:
        print(f"[WARN] graphviz fallback тоже не сработал: {e}", file=sys.stderr)
        return False


# ==================== HTML-ВИЗУАЛИЗАЦИЯ (интерактивная) ====================
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
    dot_bin = shutil.which("dot")
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
        print(f"[WARN] dot -Tplain не сработал, HTML посчитает укладку сам в браузере: {e}", file=sys.stderr)
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
// ходами) — сюда же: это хребет всей диаграммы, ход процесса, ему тоже
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
    json_name = preferred_yaml_name[:-len(".yaml")] + ".json"
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
    (turns, orphans, resolution_edges, start_target, inline_labels, finish_source,
     superseded_targets, title_targets, request_answers, next_request_edges) = build_turns(parts_dir, registry)

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
        log(f"Вытесненных повторным запросом финальных ответов: {len(superseded_targets)} -> {superseded_targets}")
    if orphans:
        warn(f"Осиротевших fetch_raw (нить началась до окна дампов): {len(orphans)} "
             f"-> part_id {[o['part_id'] for o in orphans]}")

    puml_text = render_plantuml(turns, orphans, resolution_edges, start_target, inline_labels,
                                 finish_source, superseded_targets, title_targets,
                                 request_answers, next_request_edges)
    puml_path = os.path.join(out_dir, "tree.puml")
    with open(puml_path, "w", encoding="utf-8") as f:
        f.write(puml_text)
    log(f"PlantUML: {puml_path}")

    png_path = os.path.join(out_dir, "tree.png")
    if render_png_via_plantuml(puml_path):
        log(f"PNG (настоящий PlantUML): {png_path}")
    elif render_png_via_graphviz_fallback(turns, orphans, resolution_edges, start_target, inline_labels,
                                           finish_source, superseded_targets, title_targets,
                                           request_answers, next_request_edges,
                                           png_path):
        warn(f"Локальный plantuml/java не найден — PNG отрендерен запасным способом "
             f"через Graphviz: {png_path} (структура та же, но это НЕ PlantUML-рендер)")
    else:
        warn("Ни plantuml, ни graphviz (dot) не найдены — PNG не создан. "
             "tree.puml можно отрендерить на любой другой машине с Java "
             "(`plantuml tree.puml`) или через https://plantuml.com (свой сервер).")

    # Интерактивная HTML-версия — для сессий, где статичная картинка (PNG/
    # PlantUML) уже нечитаема из-за плотности графа. Открывается напрямую в
    # браузере (file://), без сервера и без внешних библиотек.
    model = build_graph_model(turns, orphans, resolution_edges, start_target, inline_labels,
                               finish_source, superseded_targets, title_targets,
                               request_answers, next_request_edges, registry)
    layout = run_dot_plain_layout(model)
    html_path = os.path.join(out_dir, "tree.html")
    render_html(model, layout, html_path)
    if layout:
        log(f"HTML (интерактивный, укладка Graphviz): {html_path}")
    else:
        warn(f"HTML сгенерирован, но dot не найден — укладка посчитана в браузере "
             f"(грубее настоящего Graphviz): {html_path}")

    return html_path


def main():
    """CLI-запуск: python -m backend_adapter.artifact_tree <parts_dir>.
    Формат логов наследуется от того, кто запустил процесс (вручную —
    стандартный запасной хендлер logging)."""
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("parts_dir", help="Путь к директории *.parts с дампами адаптера")
    args = ap.parse_args()

    if not os.path.isdir(args.parts_dir):
        logger.error(f"Не найдена директория: {args.parts_dir}")
        sys.exit(1)

    generate(args.parts_dir, verbose=True)


if __name__ == "__main__":
    main()
