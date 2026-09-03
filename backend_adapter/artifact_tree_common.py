"""artifact_tree_common — общие утилиты, константы и цвета для artifact_tree."""

import hashlib
import json
import logging
import re

logger = logging.getLogger("artifact_tree")

try:
    import yaml

    YAML_AVAILABLE = True

    def _yaml_str_representer(dumper, data):
        # Многострочные значения (reasoning, tool_result, response и т.п.)
        # читаются заметно удобнее как block scalar (content: |), чем как
        # однострочная кавычная строка с буквальными \n внутри — при этом
        # содержимое остаётся тем же самым, программный разбор через
        # yaml.safe_load не меняется.
        style = "|" if "\n" in data else None
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)

    yaml.add_representer(str, _yaml_str_representer, Dumper=yaml.SafeDumper)
except ImportError:
    YAML_AVAILABLE = False

PART_RE = re.compile(r"^(?P<session>[0-9a-f]+)-(?P<id>\d+)-(?P<type>openai_body|fetch_raw)\.json$")

# Волатильные вставки, которые меняются от хода к ходу без изменения
# содержательного смысла артефакта (см. докстринг выше про
# <total_tokens>...tokens left</total_tokens>). ВЫРЕЗАЮТСЯ целиком (не
# заменяются плейсхолдером) перед вычислением ключа дедупликации — только
# для СРАВНЕНИЯ, исходный текст первого появления сохраняется как есть.
# Список осознанно небольшой и явный: расширяйте по мере обнаружения новых
# похожих случаев, не пытайтесь угадать все волатильные паттерны заранее.
VOLATILITY_PATTERNS = [
    re.compile(r"<total_tokens>\d+ tokens left</total_tokens>\n?"),
]


def normalize_for_dedup(text: str) -> str:
    normalized = text
    for pat in VOLATILITY_PATTERNS:
        normalized = pat.sub("", normalized)
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


# ==================== ЦВЕТА ====================

DOMAIN_COLOR = {
    "system": "#FFF3CD",
    "user": "#D1ECF1",
    "tool_result": "#D4EDDA",
    "toolcall": "#E0CFFC",
    "reasoning": "#F8D7DA",
    "response": "#D6D8DB",
    "other": "#FFFFFF",
}
KIND_COLOR = {"agent_turn": "#CFE2FF", "structured_output": "#FFE5B4"}


# ==================== ИЗВЛЕЧЕНИЕ ТЕКСТА ИЗ СООБЩЕНИЙ ====================


def extract_message_text(content) -> str:
    """Извлекает текстовое содержание из content-поля сообщения.

    content может быть строкой, списком block-объектов (type:text),
    словарём {"text": ...} или чем-то ещё. Всегда возвращает str."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(block.get("text", ""))
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if isinstance(content, dict):
        return content.get("text", "")
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
    return json.dumps(
        {"name": func.get("name"), "arguments": args_canonical}, ensure_ascii=False, sort_keys=True
    )
