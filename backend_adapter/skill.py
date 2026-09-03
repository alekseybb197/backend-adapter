"""Skill detection from tool call arguments.

Parses tool inputs (command, file_path, path, pattern, notebook_path)
against regex patterns loaded from config or defaults.
"""

import re

from .config import ADAPTER_SKILL_PATTERNS

DEFAULT_SKILL_PATTERNS = {
    # skill_name -> список regex, которым может соответствовать
    # содержимое Bash-команды / путь к файлу в Read/Grep/Glob,
    # сигнализирующее об обращении к данному скиллу.
    "devtools": [r"\.claude/skills/devtools", r"\.qwen/skills/devtools", r"chrome-devtools"],
    "frontmatter": [r"\.claude/skills/frontmatter", r"\.qwen/skills/frontmatter"],
    "klast": [r"\.claude/skills/klast", r"\.qwen/skills/klast", r"\.klast/"],
    "mytasks": [r"\.claude/skills/mytasks", r"\.qwen/skills/mytasks"],
    "prreview": [r"\.claude/skills/prreview", r"\.qwen/skills/prreview"],
}


def _load_skill_patterns():
    if ADAPTER_SKILL_PATTERNS and __import__("os").path.isfile(ADAPTER_SKILL_PATTERNS):
        try:
            import json

            with open(ADAPTER_SKILL_PATTERNS) as f:
                raw = json.load(f)
            return {
                name: [re.compile(p, re.IGNORECASE) for p in pats] for name, pats in raw.items()
            }
        except Exception as e:
            # _d is not imported to avoid circular dep with logger;
            # import it lazily at call time.
            from .logger import _d

            _d(f"[SKILL_PATTERNS] Failed to load {ADAPTER_SKILL_PATTERNS}: {e}")
    return {
        name: [re.compile(p, re.IGNORECASE) for p in pats]
        for name, pats in DEFAULT_SKILL_PATTERNS.items()
    }


SKILL_PATTERNS = _load_skill_patterns()


def detect_skill(tool_name: str, tool_input: dict):
    """Пытается определить, к какому скиллу относится вызов инструмента.
    Возвращает (skill_name, evidence) или (None, None).
    Эвристика основана на путях/командах, а не на имени инструмента —
    харнесс обращается к скиллам через обычные Bash/Read/Grep/Glob,
    отдельного tool "Skill" в текущей связке не наблюдается (см. лог)."""
    haystack_parts = []
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "pattern", "notebook_path"):
            v = tool_input.get(key)
            if isinstance(v, str):
                haystack_parts.append(v)
    haystack = " ".join(haystack_parts)
    if not haystack:
        return None, None
    for skill_name, patterns in SKILL_PATTERNS.items():
        for pat in patterns:
            m = pat.search(haystack)
            if m:
                return skill_name, m.group(0)
    # Отдельно отмечаем именно чтение SKILL.md — это явный сигнал того,
    # что харнесс/модель обнаружила и загружает описание скилла, даже если
    # это скилл, не описанный в SKILL_PATTERNS (новый / незарегистрированный).
    if re.search(r"SKILL\.md", haystack, re.IGNORECASE):
        m = re.search(r"skills/([^/]+)/SKILL\.md", haystack, re.IGNORECASE)
        name = m.group(1) if m else "unknown"
        return f"unregistered:{name}", haystack
    return None, None
