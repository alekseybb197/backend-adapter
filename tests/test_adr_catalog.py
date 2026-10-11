#!/usr/bin/env python3
"""Целостность каталога архитектурных решений docs/adr/ и правил .claude/.

Каталог правят и человек, и агент-разработчик (skills adr-implement,
adr-propose). Тест ловит расхождения, которые иначе копятся незаметно:

- номер в имени файла и в заголовке ADR совпадают, номера уникальны;
- статус — одно из допустимых слов (первое слово строки «| Статус |»);
- каждый ADR есть в индексе docs/adr/README.md, а ссылки индекса живые;
- исходный журнал в archive/ не изменён (первоисточник);
- правила .claude/rules/*.md ссылаются на существующие ADR, а их шаблоны
  paths: совпадают хотя бы с одним файлом (правило не «умерло» после
  переименования модуля);
- у skills есть name и description.

Только stdlib + PyYAML (уже зависимость проекта). Сами проверки занимают
миллисекунды; время прогона (~2 с) — autouse-фикстуры conftest.py.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = ROOT / "docs" / "adr"
RULES_DIR = ROOT / ".claude" / "rules"
SKILLS_DIR = ROOT / ".claude" / "skills"

STATUSES = ("Предложено", "Принято", "Действует", "Заменено", "Отклонено")
ARCHIVE_SHA256 = {
    "2026-09-09-claude-md-adr-journal.md": (
        "0d6af69c8f65a162f42cd124aa1b8137af96b652d17e12da46c3d32bd4fbb717"
    ),
}

_ADR_FILE_RE = re.compile(r"^(\d{3})-[a-z0-9-]+\.md$")
_STATUS_RE = re.compile(r"^\| Статус \| ([^\s;:(—,]+)", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _adr_files() -> list[Path]:
    return sorted(p for p in ADR_DIR.glob("*.md") if _ADR_FILE_RE.match(p.name))


def _frontmatter(path: Path) -> dict:
    m = _FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    assert m, f"{path.relative_to(ROOT)}: нет YAML-frontmatter"
    data = yaml.safe_load(m.group(1))
    assert isinstance(data, dict), f"{path.relative_to(ROOT)}: frontmatter не словарь"
    return data


def _expand_braces(pattern: str) -> list[str]:
    """{a,b} → варианты (без вложенных скобок — в правилах их нет)."""
    parts = re.split(r"(\{[^{}]*\})", pattern)
    options = [p[1:-1].split(",") if p.startswith("{") else [p] for p in parts]
    return ["".join(combo) for combo in itertools.product(*options)]


def test_catalog_not_empty():
    assert len(_adr_files()) >= 40


@pytest.mark.parametrize("path", _adr_files(), ids=lambda p: p.name[:3])
def test_adr_header_and_status(path: Path):
    text = path.read_text(encoding="utf-8")
    num = path.name[:3]
    assert text.startswith(f"# ADR-{num}: "), f"{path.name}: заголовок не «# ADR-{num}: …»"
    m = _STATUS_RE.search(text)
    assert m, f"{path.name}: нет строки «| Статус | …»"
    assert m.group(1) in STATUSES, f"{path.name}: статус {m.group(1)!r} не из {STATUSES}"


def test_adr_numbers_unique():
    nums = [p.name[:3] for p in _adr_files()]
    dupes = sorted({n for n in nums if nums.count(n) > 1})
    assert not dupes, f"повторяющиеся номера ADR: {dupes}"


def test_every_adr_in_index_and_index_links_resolve():
    index = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    missing = [p.name for p in _adr_files() if f"]({p.name})" not in index]
    assert not missing, f"ADR нет в docs/adr/README.md: {missing}"
    links = re.findall(r"\]\(([^)#\s]+)\)", index)
    broken = [lnk for lnk in links if "://" not in lnk and not (ADR_DIR / lnk).exists()]
    assert not broken, f"битые ссылки в docs/adr/README.md: {broken}"


@pytest.mark.parametrize("name,sha", sorted(ARCHIVE_SHA256.items()))
def test_archive_is_unchanged(name: str, sha: str):
    data = (ADR_DIR / "archive" / name).read_bytes()
    assert hashlib.sha256(data).hexdigest() == sha, (
        f"archive/{name} изменён — это исторический первоисточник, не редактировать"
    )


def _rule_files() -> list[Path]:
    return sorted(RULES_DIR.glob("*.md"))


def test_rules_present():
    assert _rule_files(), ".claude/rules/*.md не найдены"


@pytest.mark.parametrize("path", _rule_files(), ids=lambda p: p.stem)
def test_rule_paths_match_files(path: Path):
    paths = _frontmatter(path).get("paths")
    assert isinstance(paths, list) and paths, f"{path.name}: пустой paths:"
    # Каждая альтернатива {a,b} проверяется отдельно: иначе переименование
    # одного модуля из группы прошло бы незамеченным (совпали бы остальные).
    dead = [
        alt for pat in paths for alt in _expand_braces(pat) if next(ROOT.glob(alt), None) is None
    ]
    assert not dead, f"{path.name}: шаблоны paths без единого совпадения: {dead}"


@pytest.mark.parametrize("path", _rule_files(), ids=lambda p: p.stem)
def test_rule_adr_references_exist(path: Path):
    body = _FRONTMATTER_RE.sub("", path.read_text(encoding="utf-8"))
    existing = {p.name[:3] for p in _adr_files()}
    referenced = set(re.findall(r"\b(0\d\d)\b", body))
    unknown = sorted(referenced - existing)
    assert not unknown, f"{path.name}: ссылки на несуществующие ADR: {unknown}"


@pytest.mark.parametrize("path", sorted(SKILLS_DIR.glob("*/SKILL.md")), ids=lambda p: p.parent.name)
def test_skill_frontmatter(path: Path):
    fm = _frontmatter(path)
    assert fm.get("name") == path.parent.name, f"{path}: name ≠ имени каталога"
    assert isinstance(fm.get("description"), str) and fm["description"].strip()
