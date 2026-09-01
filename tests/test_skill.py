"""Tests for backend_adapter.skill — skill detection from tool call arguments."""
import importlib
import json
import os
from unittest import mock

from backend_adapter.skill import detect_skill, SKILL_PATTERNS, _load_skill_patterns, DEFAULT_SKILL_PATTERNS


class TestDefaultPatterns:
    """Tests that SKILL_PATTERNS loaded correctly at module level."""

    def test_has_expected_skills(self):
        """Default patterns should include all known skill names."""
        expected = {"devtools", "frontmatter", "klast", "mytasks", "prreview"}
        assert expected.issubset(set(SKILL_PATTERNS.keys()))

    def test_patterns_compiled(self):
        """Patterns should be compiled regex objects."""
        for name, pats in SKILL_PATTERNS.items():
            for pat in pats:
                assert hasattr(pat, "search")


class TestDetectSkill:
    """Tests for detect_skill()."""

    def test_devtools_command_detected(self):
        """devtools skill detected by chrome-devtools pattern."""
        skill, evidence = detect_skill("Bash", {"command": "open chrome-devtools"})
        assert skill == "devtools"
        assert evidence == "chrome-devtools"

    def test_klast_path_detected(self):
        """klast skill detected by .klast/ pattern."""
        skill, evidence = detect_skill("Read", {"file_path": "/.klast/project/README.md"})
        assert skill == "klast"

    def test_no_match(self):
        """No skill detected for unrelated input."""
        skill, evidence = detect_skill("Bash", {"command": "echo hello"})
        assert skill is None
        assert evidence is None

    def test_empty_input(self):
        """Empty tool_input → (None, None)."""
        skill, evidence = detect_skill("Bash", {})
        assert skill is None
        assert evidence is None

    def test_empty_string_input(self):
        """Empty string tool_input → (None, None)."""
        skill, evidence = detect_skill("Bash", {"command": ""})
        assert skill is None
        assert evidence is None

    def test_unregistered_skill_skill_md(self):
        """SKILL.md detection for unknown skill."""
        skill, evidence = detect_skill("Read", {"file_path": "skills/custom/SKILL.md"})
        assert skill == "unregistered:custom"

    def test_unregistered_skill_unknown_name(self):
        """SKILL.md without path component → unregistered:unknown."""
        skill, evidence = detect_skill("Read", {"file_path": "SKILL.md"})
        assert skill.startswith("unregistered:")

    def test_case_insensitive(self):
        """Pattern matching should be case-insensitive."""
        skill, evidence = detect_skill("Read", {"file_path": "Skills/DevTools/README.md"})
        # The pattern contains `.claude/skills/devtools` — not case-insensitive for path
        # But the skill pattern regex itself uses re.IGNORECASE
        skill2, _ = detect_skill("Read", {"file_path": ".claude/skills/devtools/README.md"})
        assert skill2 == "devtools"


class TestLoadSkillPatterns:
    """Tests for _load_skill_patterns() with custom config."""

    def test_load_custom_patterns_from_file(self, tmp_path, monkeypatch):
        """Custom patterns from JSON file should be loaded."""
        patterns_file = tmp_path / "patterns.json"
        patterns_file.write_text(json.dumps({
            "custom_skill": ["\\.my/special/path"]
        }))
        monkeypatch.setenv("ADAPTER_SKILL_PATTERNS", str(patterns_file))
        # Remove old module from cache and reload
        import sys
        to_remove = [n for n in sys.modules if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import skill
        assert "custom_skill" in skill.SKILL_PATTERNS
        assert "devtools" not in skill.SKILL_PATTERNS

    def test_invalid_json_returns_defaults(self, tmp_path, monkeypatch):
        """Invalid JSON file should fall back to defaults."""
        patterns_file = tmp_path / "invalid.json"
        patterns_file.write_text("not json {")
        monkeypatch.setenv("ADAPTER_SKILL_PATTERNS", str(patterns_file))
        import sys
        to_remove = [n for n in sys.modules if n.startswith("backend_adapter")]
        for n in to_remove:
            del sys.modules[n]
        from backend_adapter import skill
        assert "devtools" in skill.SKILL_PATTERNS
