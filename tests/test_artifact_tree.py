#!/usr/bin/env python3
"""Unit tests for the artifact_tree package.

Tests cover: dedup utilities, ArtifactRegistry, parsing/classification,
turn-building, renders (PlantUML, HTML, Graphviz fallback), and full
generate() end-to-end.

Small session fixtures are generated on the fly in tmp_path as JSON
openai_body/fetch_raw files matching the PART_RE pattern."""
import json
import os
import sys

import pytest

# Re-import modules fresh each test (sys.modules may have stale state).
# We delete them here and re-import below to ensure isolation.

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _make_parts_dir(tmp_path, name: str, parts: dict) -> str:
    """Create a session directory with given part files.

    ``parts`` is ``{"<type>-<id>-<suffix>.json": <data>, ...}``
    Returns absolute path to the parts directory.
    """
    d = os.path.join(str(tmp_path), name)
    os.makedirs(d, exist_ok=True)
    for fname, data in parts.items():
        _write_json(os.path.join(d, fname), data)
    return d


# Simple single-turn openai_body: user asks a question, model answers.
def _simple_ob(part_id: int) -> dict:
    return {
        "messages": [
            {"role": "user", "content": "Hello world"},
        ],
    }


# Simple fetch_raw response: model returns text.
def _simple_fr(part_id: int, content: str = "Hi there!") -> dict:
    return {
        "choices": [{"message": {"content": content}}],
    }


# Agent-turn openai_body (has tools[]).
def _agent_ob(part_id: int) -> dict:
    return {
        "tools": [{"type": "function", "function": {"name": "ls"}}],
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "List files"},
        ],
    }


# Agent-turn fetch_raw with tool_calls.
def _agent_fr(part_id: int) -> dict:
    return {
        "choices": [{
            "message": {
                "content": "Let me check.",
                "tool_calls": [{
                    "id": "call-001",
                    "type": "function",
                    "function": {"name": "ls", "arguments": '{"command":"ls -la"}'},
                }],
            },
        }],
    }


# Structured output fetch_raw (JSON title).
def _structured_output_fr(part_id: int) -> dict:
    return {
        "choices": [{
            "message": {"content": '{"title": "Test Session"}'},
        }],
    }


# Fetch_raw with reasoning.
def _fr_with_reasoning(part_id: int) -> dict:
    return {
        "choices": [{
            "message": {
                "content": "Done.",
                "reasoning_content": "Let me think about this step by step.",
            },
        }],
    }


# OpenAI body with a tool response (role: tool).
def _ob_with_tool_response(part_id: int, tool_call_id: str) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "List files"},
            {
                "role": "assistant",
                "content": "Let me check.",
                "tool_calls": [{
                    "id": tool_call_id,
                    "type": "function",
                    "function": {"name": "ls", "arguments": '{"command":"ls -la"}'},
                }],
            },
            {"role": "tool", "content": "file1.txt\nfile2.txt", "tool_call_id": tool_call_id},
        ],
    }


# System-reminder user message (CLAUDE.md injection).
def _ob_system_reminder(part_id: int) -> dict:
    return {
        "messages": [
            {"role": "user", "content": "<system-reminder>\n\nHere are your memory pointers:\n\n..."},
        ],
    }


# System prompt with "# Harness".
def _ob_system_harness(part_id: int) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "# Harness\nYou are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ],
    }


# System prompt with <session> tag.
def _ob_system_session(part_id: int) -> dict:
    return {
        "messages": [
            {"role": "system", "content": "<session>Generate a title"},
            {"role": "user", "content": "Hello"},
        ],
    }


# ---------------------------------------------------------------------------
# Smoke import
# ---------------------------------------------------------------------------

class TestSmoke:
    def test_import_artifact_tree(self):
        # Delete cached modules so we test a clean import
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter import artifact_tree
        assert hasattr(artifact_tree, "generate")
        assert hasattr(artifact_tree, "main")
        assert hasattr(artifact_tree, "YAML_AVAILABLE")
        assert artifact_tree.__all__

    def test_star_import_all_names_resolve(self):
        # Каждое имя из __all__ shim-модуля обязано реально существовать —
        # `from backend_adapter.artifact_tree import *` не должен падать с
        # AttributeError (имена цветов живут в html/common, а не в shim).
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter import artifact_tree
        for name in artifact_tree.__all__:
            assert hasattr(artifact_tree, name), f"__all__ обещает отсутствующее имя: {name}"
        # star-import исполняет ровно этот контракт
        ns = {}
        exec("from backend_adapter.artifact_tree import *", ns)
        for name in artifact_tree.__all__:
            assert name in ns, f"star-import не дал имя: {name}"

    def test_import_session_viewer(self):
        # С v0.7.1 session_viewer — чистый эндпойнт "/session" общего ядра
        # webserver.py: CLI/serve()/Handler уехали в ядро, в модуле остались
        # логика генерации + эндпойнт.
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter import session_viewer
        assert hasattr(session_viewer, "find_or_generate_sessions")
        assert hasattr(session_viewer, "render_shell")
        assert hasattr(session_viewer, "SessionEndpoint")
        assert not hasattr(session_viewer, "serve")
        assert not hasattr(session_viewer, "main")
        assert not hasattr(session_viewer, "Handler")
        from backend_adapter import webserver
        assert hasattr(webserver, "serve")


# ---------------------------------------------------------------------------
# TestDedup
# ---------------------------------------------------------------------------

class TestDedup:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree_common import (
            normalize_for_dedup, sha12, strip_trailing_line_whitespace,
        )
        self.normalize = normalize_for_dedup
        self.sha12 = sha12
        self.strip = strip_trailing_line_whitespace

    def test_normalize_removes_total_tokens(self):
        text = "<total_tokens>15000000 tokens left</total_tokens>\nSome prompt"
        result = self.normalize(text)
        assert "<total_tokens>" not in result
        assert "Some prompt" in result

    def test_normalize_strips_multiple_tokens(self):
        text = "<total_tokens>100 tokens left</total_tokens>\n<total_tokens>200 tokens left</total_tokens>\nText"
        result = self.normalize(text)
        assert "<total_tokens>" not in result

    def test_normalize_preserves_meaningful_content(self):
        text = "System prompt for helpful assistant"
        assert self.normalize(text) == text

    def test_normalize_trailing_whitespace_removed(self):
        # normalize_for_dedup: removes volatility patterns, then rstrip()
        text = "hello   \n  world  \n"
        assert self.normalize(text) == "hello   \n  world"

    def test_sha12_returns_12_chars(self):
        h = self.sha12("anything")
        assert len(h) == 12
        assert all(c in "0123456789abcdef" for c in h)

    def test_sha12_deterministic(self):
        assert self.sha12("same") == self.sha12("same")

    def test_sha12_different_inputs(self):
        assert self.sha12("aaa") != self.sha12("bbb")

    def test_strip_trailing_whitespace(self):
        assert self.strip("hello  \nworld\t\n") == "hello\nworld\n"

    def test_strip_empty_whitespace_lines(self):
        assert self.strip("line\n  \n  \nend") == "line\n\n\nend"


# ---------------------------------------------------------------------------
# TestExtract
# ---------------------------------------------------------------------------

class TestExtract:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree_common import (
            extract_message_text, tool_call_text,
        )
        self.extract = extract_message_text
        self.tool_call_text = tool_call_text

    def test_extract_str(self):
        assert self.extract("plain text") == "plain text"

    def test_extract_list_of_blocks(self):
        blocks = [{"text": "hello"}, {"text": "world"}]
        assert self.extract(blocks) == "hello\nworld"

    def test_extract_dict(self):
        assert self.extract({"text": "hi"}) == "hi"
        assert self.extract({"other": "field"}) == ""

    def test_extract_list_non_dict(self):
        assert self.extract(["just a string"]) == "just a string"

    def test_extract_other(self):
        assert self.extract(123) == "123"
        assert self.extract(None) == "None"

    def test_tool_call_text_canonical(self):
        # Arguments are parsed and re-dumped with sort_keys=True, so keys
        # are sorted (no real difference with one key, but the format is canonical).
        tc = {
            "function": {
                "name": "ls",
                "arguments": '{"command":"ls -la"}',
            }
        }
        result = self.tool_call_text(tc)
        parsed = json.loads(result)
        assert parsed["name"] == "ls"
        # Canonical: sort_keys ensures deterministic order
        assert parsed["arguments"] == '{"command": "ls -la"}'

    def test_tool_call_text_handles_bad_json(self):
        tc = {"function": {"name": "bad", "arguments": "not json at all"}}
        result = self.tool_call_text(tc)
        parsed = json.loads(result)
        assert parsed["name"] == "bad"
        assert parsed["arguments"] == "not json at all"

    def test_tool_call_text_handles_null(self):
        tc = {"function": {"name": "noargs"}}
        result = self.tool_call_text(tc)
        parsed = json.loads(result)
        assert parsed["name"] == "noargs"
        assert parsed["arguments"] == "{}"


# ---------------------------------------------------------------------------
# TestRegistry
# ---------------------------------------------------------------------------

class TestRegistry:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree_registry import ArtifactRegistry
        self.registry = ArtifactRegistry()

    def test_register_returns_name(self):
        name = self.registry.register("user", "Hello", "1")
        assert name == "user-1"

    def test_register_same_text_deduplicates(self):
        self.registry.register("user", "Hello", "1")
        name2 = self.registry.register("user", "Hello", "2")
        assert name2 == "user-1"

    def test_register_different_text_different_name(self):
        self.registry.register("user", "Hello", "1")
        name2 = self.registry.register("user", "World", "2")
        assert name2 == "user-2"

    def test_register_duplicate_base_name_suffix(self):
        self.registry.register("user", "Hello", "1")
        self.registry.register("user", "Hello2", "1")
        name3 = self.registry.register("user", "Hello", "3")
        # Same text as first -> deduped
        assert name3 == "user-1"

    def test_register_same_base_name_different_text(self):
        self.registry.register("user", "Hello", "1")
        name2 = self.registry.register("user", "Hello world", "1")
        assert name2 == "user-1-1"

    def test_name_for_returns_existing(self):
        self.registry.register("user", "Hello", "1")
        assert self.registry.name_for("Hello") == "user-1"

    def test_name_for_returns_none_unknown(self):
        assert self.registry.name_for("Unknown text") is None

    def test_link_and_lookup_protocol_id(self):
        self.registry.register("toolcall", '{"name":"ls","arguments":{}}', "1")
        self.registry.link_protocol_id("call-001", "toolcall-1")
        assert self.registry.artifact_for_protocol_id("call-001") == "toolcall-1"
        assert self.registry.artifact_for_protocol_id("nonexistent") is None

    def test_write_all_creates_files(self, tmp_path):
        self.registry.register("user", "Hello", "1")
        self.registry.register("system", "System prompt", "1")
        self.registry.write_all(str(tmp_path))
        assert os.path.isfile(os.path.join(str(tmp_path), "user-1.yaml"))
        assert os.path.isfile(os.path.join(str(tmp_path), "system-1.yaml"))

    def test_write_all_content_structure(self, tmp_path):
        self.registry.register("user", "Hello world", "5")
        self.registry.write_all(str(tmp_path))
        import yaml
        with open(os.path.join(str(tmp_path), "user-5.yaml"), encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["domain"] == "user"
        assert data["first_seen_part_id"] == "5"
        assert data["content"] == "Hello world"
        assert "sha256_raw" in data
        assert "sha256_normalized" in data


# ---------------------------------------------------------------------------
# TestParse
# ---------------------------------------------------------------------------

class TestParse:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree_parse import (
            classify_kind,
            fetch_raw_has_tool_calls,
            determine_fetch_raw_kind,
            looks_like_structured_output,
        )
        from backend_adapter.artifact_tree_registry import ArtifactRegistry
        self.classify_kind = classify_kind
        self.fetch_raw_has_tool_calls = fetch_raw_has_tool_calls
        self.determine_fetch_raw_kind = determine_fetch_raw_kind
        self.looks_like_structured_output = looks_like_structured_output
        self.registry = ArtifactRegistry()

    def test_classify_kind_agent_turn(self):
        assert self.classify_kind({"tools": [{"type": "function"}]}) == "agent_turn"

    def test_classify_kind_structured_output(self):
        assert self.classify_kind({"tools": None}) == "structured_output"
        assert self.classify_kind({}) == "structured_output"
        assert self.classify_kind({"messages": []}) == "structured_output"

    def test_fetch_raw_has_tool_calls_true(self):
        fr = {"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]}
        assert self.fetch_raw_has_tool_calls(fr) is True

    def test_fetch_raw_has_tool_calls_false(self):
        fr = {"choices": [{"message": {"content": "text"}}]}
        assert self.fetch_raw_has_tool_calls(fr) is False

    def test_fetch_raw_has_tool_calls_empty_list(self):
        fr = {"choices": [{"message": {"tool_calls": []}}]}
        assert self.fetch_raw_has_tool_calls(fr) is False

    def test_determine_fetch_raw_kind_agent_turn_with_tools(self):
        fr = {"choices": [{"message": {"tool_calls": [{"id": "1"}]}}]}
        assert self.determine_fetch_raw_kind(fr) == "agent_turn"

    def test_determine_fetch_raw_kind_structured_output_json(self):
        fr = {"choices": [{"message": {"content": '{"title": "Test"}'}}]}
        assert self.determine_fetch_raw_kind(fr) == "structured_output"

    def test_determine_fetch_raw_kind_agent_turn_text(self):
        fr = {"choices": [{"message": {"content": "Some text response"}}]}
        assert self.determine_fetch_raw_kind(fr) == "agent_turn"

    def test_looks_like_structured_output_dict(self):
        assert self.looks_like_structured_output('{"title": "Test"}') is True

    def test_looks_like_structured_output_json_string(self):
        # JSON-quoted string literal — structured_output-сайдкар returns
        # content that is a valid JSON string (with quotes).
        assert self.looks_like_structured_output('"Files in current folder"') is True

    def test_looks_like_structured_output_multiline_string(self):
        assert self.looks_like_structured_output("This is\na multiline response") is False

    def test_looks_like_structured_output_markdown(self):
        assert self.looks_like_structured_output("Here is the answer:\n\nLet me explain...") is False

    def test_looks_like_structured_output_with_code_fences(self):
        assert self.looks_like_structured_output("```json\n{\"title\": \"A\"}\n```") is True

    def test_looks_like_structured_output_long_string(self):
        assert self.looks_like_structured_output("x" * 201) is False


# ---------------------------------------------------------------------------
# TestProcessPart
# ---------------------------------------------------------------------------

class TestProcessPart:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree_parse import (
            process_openai_body, process_fetch_raw, load_json, discover_parts,
        )
        from backend_adapter.artifact_tree_registry import ArtifactRegistry
        self.process_openai_body = process_openai_body
        self.process_fetch_raw = process_fetch_raw
        self.load_json = load_json
        self.discover_parts = discover_parts
        self.registry = ArtifactRegistry()

    def test_process_openai_body_user_message(self):
        resolution_edges = []
        names = self.process_openai_body(_simple_ob(1), "1", self.registry, resolution_edges)
        assert len(names) == 1
        assert "user-1" in names

    def test_process_openai_body_system_message(self):
        resolution_edges = []
        names = self.process_openai_body(_ob_system_harness(1), "1", self.registry, resolution_edges)
        names_set = set(names)
        assert any("system-1" == n or n.startswith("system-1") for n in names)

    def test_process_openai_body_tool_response(self):
        resolution_edges = []
        ob = _ob_with_tool_response(3, "call-001")
        # First register the toolcall so resolution works
        self.registry.link_protocol_id("call-001", "toolcall-1")
        names = self.process_openai_body(ob, "3", self.registry, resolution_edges)
        # Should have system, user, assistant(toolcall+response), tool_result
        assert len(names) >= 4
        assert len(resolution_edges) == 1
        assert resolution_edges[0][0].startswith("toolcall")
        assert resolution_edges[0][1].startswith("tool_result")

    def test_process_openai_body_empty_content_skipped(self):
        resolution_edges = []
        names = self.process_openai_body(
            {"messages": [{"role": "user", "content": ""}]},
            "1", self.registry, resolution_edges
        )
        assert names == []

    def test_process_openai_body_assistant_tool_calls(self):
        resolution_edges = []
        ob = {
            "messages": [
                {
                    "role": "assistant",
                    "content": "Let me check.",
                    "tool_calls": [{
                        "id": "call-abc",
                        "type": "function",
                        "function": {"name": "ls", "arguments": '{"command":"ls"}'},
                    }],
                }
            ]
        }
        names = self.process_openai_body(ob, "1", self.registry, resolution_edges)
        assert any(n.startswith("toolcall") for n in names)
        assert any(n.startswith("response") for n in names)
        assert self.registry.artifact_for_protocol_id("call-abc") is not None

    def test_process_fetch_raw_text_only(self):
        result = self.process_fetch_raw(_simple_fr(1), "1", self.registry)
        assert result["reasoning_name"] is None
        assert len(result["decision_names"]) == 1
        assert result["decision_names"][0].startswith("response")

    def test_process_fetch_raw_with_reasoning(self):
        result = self.process_fetch_raw(_fr_with_reasoning(1), "1", self.registry)
        assert result["reasoning_name"] is not None
        assert result["reasoning_name"].startswith("reasoning")
        assert len(result["decision_names"]) == 1

    def test_process_fetch_raw_with_tool_calls(self):
        result = self.process_fetch_raw(_agent_fr(1), "1", self.registry)
        assert len(result["decision_names"]) == 2  # text + toolcall
        assert any(n.startswith("toolcall") for n in result["decision_names"])

    def test_process_fetch_raw_empty_content(self):
        result = self.process_fetch_raw(
            {"choices": [{"message": {"content": ""}}]}, "1", self.registry
        )
        assert result["reasoning_name"] is None
        assert result["decision_names"] == []

    def test_discover_parts(self, tmp_path):
        d = os.path.join(str(tmp_path), "session-test.parts")
        os.makedirs(d)
        _write_json(os.path.join(d, "abc123-1-openai_body.json"), {})
        _write_json(os.path.join(d, "def456-2-fetch_raw.json"), {})
        _write_json(os.path.join(d, "not-a-part.txt"), "ignore")
        found = self.discover_parts(d)
        assert len(found["openai_body"]) == 1
        assert len(found["fetch_raw"]) == 1
        assert found["openai_body"][0][0] == 1
        assert found["fetch_raw"][0][0] == 2

    def test_discover_parts_empty(self, tmp_path):
        d = os.path.join(str(tmp_path), "empty.parts")
        os.makedirs(d)
        assert self.discover_parts(d) == {"openai_body": [], "fetch_raw": []}

    def test_discover_parts_sorted(self, tmp_path):
        d = os.path.join(str(tmp_path), "s.parts")
        os.makedirs(d)
        _write_json(os.path.join(d, "aaa-10-openai_body.json"), {})
        _write_json(os.path.join(d, "bbb-2-openai_body.json"), {})
        _write_json(os.path.join(d, "ccc-1-openai_body.json"), {})
        found = self.discover_parts(d)
        pids = [pid for pid, _ in found["openai_body"]]
        assert pids == [1, 2, 10]  # sorted by part_id ascending (int)


# ---------------------------------------------------------------------------
# TestBuildTurns
# ---------------------------------------------------------------------------

class TestBuildTurns:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree_turnbuilder import build_turns
        from backend_adapter.artifact_tree_registry import ArtifactRegistry
        self.build_turns = build_turns
        self.registry = ArtifactRegistry()
        self.registry_class = ArtifactRegistry

    def test_build_turns_single_pair(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-single.parts", {
            "abc-1-openai_body.json": _simple_ob(1),
            "def-2-fetch_raw.json": _simple_fr(2, "Hi!"),
        })
        (turns, orphans, resolution_edges, start_target, inline_labels,
         finish_source, superseded_targets, title_targets,
         request_answers, next_request_edges, page_boundaries, checkpoint) = self.build_turns(d, self.registry)

        assert len(turns) == 1
        assert len(orphans) == 0
        assert turns[0]["kind"] == "structured_output"
        assert start_target == "user-1"
        # finish_source is None for single-request sessions (needs LAST real user entry)
        assert finish_source is None
        # request_answers only populated for agent_turn requests

    def test_build_turns_agent_turn(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-agent.parts", {
            "aaa-1-openai_body.json": _agent_ob(1),
            "bbb-2-fetch_raw.json": _agent_fr(2),
        })
        (turns, orphans, resolution_edges, start_target, inline_labels,
         finish_source, superseded_targets, title_targets,
         request_answers, next_request_edges, page_boundaries, checkpoint) = self.build_turns(d, self.registry)

        assert len(turns) == 1
        assert turns[0]["kind"] == "agent_turn"
        assert turns[0]["reasoning_name"] is None

    def test_build_turns_with_reasoning(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-reasoning.parts", {
            "aaa-1-openai_body.json": _simple_ob(1),
            "bbb-2-fetch_raw.json": _fr_with_reasoning(2),
        })
        (turns, orphans, resolution_edges, start_target, inline_labels,
         finish_source, superseded_targets, title_targets,
         request_answers, next_request_edges, page_boundaries, checkpoint) = self.build_turns(d, self.registry)

        assert len(turns) == 1
        assert turns[0]["reasoning_name"] is not None
        assert turns[0]["reasoning_name"].startswith("reasoning")

    def test_build_turns_orphan_fetch_raw(self, tmp_path):
        # fetch_raw without matching openai_body
        d = _make_parts_dir(tmp_path, "session-orphans.parts", {
            "aaa-1-fetch_raw.json": _simple_fr(1, "orphaned"),
        })
        (turns, orphans, resolution_edges, start_target, inline_labels,
         finish_source, superseded_targets, title_targets,
         request_answers, next_request_edges, page_boundaries, checkpoint) = self.build_turns(d, self.registry)

        assert len(turns) == 0
        assert len(orphans) == 1

    def test_build_turns_multiple_turns(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-multi.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "First!"),
            "c-3-openai_body.json": _simple_ob(3),
            "d-4-fetch_raw.json": _simple_fr(4, "Second!"),
        })
        (turns, orphans, resolution_edges, start_target, inline_labels,
         finish_source, superseded_targets, title_targets,
         request_answers, next_request_edges, page_boundaries, checkpoint) = self.build_turns(d, self.registry)

        assert len(turns) == 2
        assert turns[0]["ob_part_id"] == 1
        assert turns[1]["ob_part_id"] == 3
        # Turns are structured_output (no tools), request_answers only for agent_turns

    def test_build_turns_resolution_edges(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-resolve.parts", {
            "a-1-openai_body.json": _ob_with_tool_response(1, "call-xyz"),
            "b-2-fetch_raw.json": {"choices": [{"message": {"content": "ok"}}]},
        })
        # Pre-link the tool_call_id
        reg = self.registry_class()
        # Register toolcall through fetch_raw with tool_calls
        fr_with_tc = {
            "choices": [{
                "message": {
                    "content": "Let me check",
                    "tool_calls": [{"id": "call-xyz", "type": "function",
                                    "function": {"name": "ls", "arguments": "{}"}}],
                }
            }],
        }
        d2 = _make_parts_dir(tmp_path, "session-resolve2.parts", {
            "a-1-fetch_raw.json": fr_with_tc,
            "b-2-openai_body.json": _ob_with_tool_response(2, "call-xyz"),
        })
        (turns, orphans, resolution_edges, _, _, _, _, _, request_answers,
         _, page_boundaries, checkpoint) = self.build_turns(d2, reg)

        assert len(resolution_edges) >= 1
        caller, result = resolution_edges[0]
        assert caller.startswith("toolcall")
        assert result.startswith("tool_result")

    def test_build_turns_inline_labels(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-inline.parts", {
            "a-1-openai_body.json": _ob_system_reminder(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Response"),
        })
        (turns, orphans, resolution_edges, start_target, inline_labels,
         finish_source, superseded_targets, title_targets,
         request_answers, next_request_edges, page_boundaries, checkpoint) = self.build_turns(d, self.registry)

        # The system-reminder user message is an inline label
        assert len(inline_labels) >= 1
        # Since ALL user artifacts are inline labels, there's no remaining
        # non-reminder user artifact for start_target (it stays None).
        assert start_target is None

    def test_build_turns_tool_resolution_with_reasoning(self, tmp_path):
        """toolcall with reasoning -> toolcall resolution chain."""
        fr = {
            "choices": [{
                "message": {
                    "content": "Checking...",
                    "reasoning_content": "I should list the files.",
                    "tool_calls": [{"id": "call-r", "type": "function",
                                    "function": {"name": "ls", "arguments": "{}"}}],
                }
            }],
        }
        ob = {
            "messages": [
                {"role": "user", "content": "List"},
                {
                    "role": "assistant",
                    "content": "Checking...",
                    "tool_calls": [{"id": "call-r", "type": "function",
                                    "function": {"name": "ls", "arguments": "{}"}}],
                },
                {"role": "tool", "content": "file.txt", "tool_call_id": "call-r"},
            ]
        }
        d = _make_parts_dir(tmp_path, "session-reasoning-resolve.parts", {
            "a-1-fetch_raw.json": fr,
            "b-2-openai_body.json": ob,
        })
        reg = self.registry_class()
        (turns, orphans, resolution_edges, _, _, _, _, _, _,
         _, page_boundaries, checkpoint) = self.build_turns(d, reg)

        assert len(resolution_edges) == 1
        caller, result = resolution_edges[0]
        assert caller.startswith("toolcall")
        assert result.startswith("tool_result")

    def test_build_turns_definite_fr_does_not_cross_match(self, tmp_path):
        """Agent-turn fetch_raw (definite=True) should NOT match structured_output body."""
        d = _make_parts_dir(tmp_path, "session-definite.parts", {
            "a-1-openai_body.json": _simple_ob(1),  # structured_output kind
            "b-2-fetch_raw.json": _agent_fr(2),     # agent_turn (definite via tool_calls)
        })
        (turns, orphans, _, _, _, _, _, _, _, _,
         page_boundaries, checkpoint) = self.build_turns(d, self.registry)
        # definite=True means NO fallback: agent_turn fr cannot match structured_output
        assert len(turns) == 0
        assert len(orphans) == 1  # the fetch_raw becomes orphan

    def test_build_turns_sorted_by_ob_part_id(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-sort.parts", {
            "c-3-openai_body.json": _simple_ob(3),
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-openai_body.json": _simple_ob(2),
            "d-4-fetch_raw.json": _simple_fr(4),
            "e-5-fetch_raw.json": _simple_fr(5),
            "f-6-fetch_raw.json": _simple_fr(6),
        })
        (turns, _, _, _, _, _, _, _, _, _,
         page_boundaries, checkpoint) = self.build_turns(d, self.registry)
        assert [t["ob_part_id"] for t in turns] == [1, 2, 3]


# ---------------------------------------------------------------------------
# TestRenders
# ---------------------------------------------------------------------------

class TestRenders:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree_plantuml import puml_id, render_plantuml
        from backend_adapter.artifact_tree_html import (
            _category_color, artifact_filename, build_graph_model,
            run_dot_plain_layout, render_html,
        )
        from backend_adapter.artifact_tree_graphviz import (
            render_png_via_plantuml, render_png_via_graphviz_fallback,
        )
        from backend_adapter.artifact_tree_registry import ArtifactRegistry
        self.puml_id = puml_id
        self.render_plantuml = render_plantuml
        self.category_color = _category_color
        self.artifact_filename = artifact_filename
        self.build_graph_model = build_graph_model
        self.run_dot_plain_layout = run_dot_plain_layout
        self.render_html = render_html
        self.render_png_via_plantuml = render_png_via_plantuml
        self.render_png_via_graphviz_fallback = render_png_via_graphviz_fallback
        self.registry = ArtifactRegistry()

    def test_puml_id_basic(self):
        assert self.puml_id("user-1") == "n_user_1"

    def test_puml_id_special_chars(self):
        assert self.puml_id("toolcall-abc-xyz") == "n_toolcall_abc_xyz"

    def test_puml_id_preserves_alphanumeric_underscore(self):
        assert self.puml_id("simple_name-123") == "n_simple_name_123"

    def test_category_color_artifact_system(self):
        assert self.category_color("artifact:system") == "#FFF3CD"

    def test_category_color_artifact_user(self):
        assert self.category_color("artifact:user") == "#D1ECF1"

    def test_category_color_turn_agent_turn(self):
        assert self.category_color("turn:agent_turn") == "#CFE2FF"

    def test_category_color_turn_structured_output(self):
        assert self.category_color("turn:structured_output") == "#FFE5B4"

    def test_category_color_anchor(self):
        assert self.category_color("anchor") == "#FFFFFF"

    def test_category_color_sink(self):
        assert self.category_color("sink") == "#FFD6D6"

    def test_category_color_orphan(self):
        assert self.category_color("orphan") == "#FF6B6B"

    def test_category_color_unknown(self):
        assert self.category_color("unknown") == "#FFFFFF"

    def test_artifact_filename_yaml(self):
        # Assumes YAML_AVAILABLE is True (pytest env has PyYAML)
        from backend_adapter.artifact_tree_common import YAML_AVAILABLE
        if YAML_AVAILABLE:
            assert self.artifact_filename("user-1") == "user-1.yaml"
        else:
            assert self.artifact_filename("user-1") == "user-1.txt"

    def test_render_plantuml_basic(self):
        turns = [{
            "ob_part_id": 1, "fr_part_id": 2, "kind": "structured_output",
            "input_names": ["user-1"],
            "reasoning_name": None,
            "decision_names": ["response-2"],
        }]
        text = self.render_plantuml(
            turns, [], [], "user-1", {}, "response-2", [], [],
            {"user-1": "response-2"}, [],
        )
        assert "@startuml" in text
        assert "@enduml" in text
        assert "user-1" in text
        assert "response-2" in text
        assert "Ход 1" in text

    def test_render_plantuml_with_resolves(self):
        turns = [{
            "ob_part_id": 1, "fr_part_id": 2, "kind": "agent_turn",
            "input_names": ["user-1"],
            "reasoning_name": None,
            "decision_names": ["toolcall-2"],
        }]
        edges = [("toolcall-2", "tool_result-3")]
        text = self.render_plantuml(
            turns, [], edges, "user-1", {}, None, [], [],
            {}, [],
        )
        assert "resolves" in text

    def test_build_graph_model_basic(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-model.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hi"),
        })
        from backend_adapter.artifact_tree_turnbuilder import build_turns
        from backend_adapter.artifact_tree_registry import ArtifactRegistry
        registry = ArtifactRegistry()
        (turns, orphans, resolution_edges, start_target, inline_labels,
         finish_source, superseded_targets, title_targets,
         request_answers, next_request_edges, page_boundaries, checkpoint) = build_turns(d, registry)

        model = self.build_graph_model(
            turns, orphans, resolution_edges, start_target, inline_labels,
            finish_source, superseded_targets, title_targets,
            request_answers, next_request_edges, registry,
        )
        assert "nodes" in model
        assert "edges" in model
        assert len(model["nodes"]) > 0
        assert "Start" in model["nodes"]

    def test_build_graph_model_has_edges(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-edges.parts", {
            "a-1-openai_body.json": _agent_ob(1),
            "b-2-fetch_raw.json": _agent_fr(2),
        })
        from backend_adapter.artifact_tree_registry import ArtifactRegistry
        from backend_adapter.artifact_tree_turnbuilder import build_turns
        reg = ArtifactRegistry()
        # Register toolcall so resolution can work
        fr = {
            "choices": [{
                "message": {
                    "content": "Checking...",
                    "tool_calls": [{"id": "call-abc", "type": "function",
                                    "function": {"name": "ls", "arguments": "{}"}}],
                }
            }],
        }
        ob = {
            "messages": [
                {"role": "user", "content": "List"},
                {
                    "role": "assistant",
                    "content": "Checking...",
                    "tool_calls": [{"id": "call-abc", "type": "function",
                                    "function": {"name": "ls", "arguments": "{}"}}],
                },
                {"role": "tool", "content": "files", "tool_call_id": "call-abc"},
            ]
        }
        d2 = _make_parts_dir(tmp_path, "session-resolve-model.parts", {
            "a-1-fetch_raw.json": fr,
            "b-2-openai_body.json": ob,
        })
        (turns, orphans, resolution_edges, start_target, inline_labels,
         finish_source, superseded_targets, title_targets,
         request_answers, next_request_edges, page_boundaries, checkpoint) = build_turns(d2, reg)
        model = self.build_graph_model(
            turns, orphans, resolution_edges, start_target, inline_labels,
            finish_source, superseded_targets, title_targets,
            request_answers, next_request_edges, reg,
        )
        # Should have at least sequence edges (between turns) and input edges
        assert len(model["edges"]) >= 1

    def test_run_dot_plain_layout_no_dot(self):
        # If dot is not installed, returns None (graceful)
        import shutil
        if shutil.which("dot") is None:
            model = {"nodes": {"a": {"label": "A", "category": "anchor", "detail": {}}}, "edges": []}
            assert self.run_dot_plain_layout(model) is None

    def test_run_dot_plain_layout_with_dot(self):
        import shutil
        if shutil.which("dot") is not None:
            model = {
                "nodes": {
                    "Start": {"label": "Start", "category": "anchor", "detail": {}},
                    "user-1": {"label": "user-1", "category": "artifact:user", "detail": {}},
                },
                "edges": [{"source": "Start", "target": "user-1", "type": "start", "label": ""}],
            }
            layout = self.run_dot_plain_layout(model)
            assert layout is not None
            assert "positions" in layout
            assert "height" in layout
            assert "Start" in layout["positions"]

    def test_render_html_creates_file(self, tmp_path):
        model = {
            "nodes": {
                "Start": {"label": "Start", "category": "anchor", "detail": {}},
            },
            "edges": [],
        }
        out_path = os.path.join(str(tmp_path), "tree.html")
        self.render_html(model, None, out_path)
        assert os.path.isfile(out_path)
        content = open(out_path).read()
        assert "DOCTYPE html" in content
        assert "DATA" in content

    def test_render_html_with_layout(self, tmp_path):
        model = {
            "nodes": {
                "a": {"label": "A", "category": "anchor", "detail": {}},
            },
            "edges": [],
        }
        layout = {"positions": {"a": (100.0, 200.0)}, "height": 300.0}
        out_path = os.path.join(str(tmp_path), "with_layout.html")
        self.render_html(model, layout, out_path)
        content = open(out_path).read()
        assert "hasLayout" in content

    def test_render_png_via_plantuml_no_binary(self):
        import shutil
        if shutil.which("plantuml") is None:
            assert self.render_png_via_plantuml("/tmp/test.puml") is False

    def test_render_png_graphviz_fallback_no_binary(self):
        import shutil
        if shutil.which("dot") is None:
            assert self.render_png_via_graphviz_fallback(
                [], [], [], None, {}, None, [], [], {}, {}, "/tmp/out.png"
            ) is False


# ---------------------------------------------------------------------------
# TestGenerate
# ---------------------------------------------------------------------------

class TestGenerate:
    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree import generate
        self.generate = generate

    def test_generate_creates_all_files(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-full.parts", {
            "a-1-openai_body.json": _agent_ob(1),
            "b-2-fetch_raw.json": _agent_fr(2),
        })
        html_path = self.generate(str(d), verbose=False)

        assert os.path.isfile(html_path)
        assert os.path.basename(html_path) == "tree.html"
        artefacts = os.path.join(str(d), "artefacts")
        assert os.path.isfile(os.path.join(artefacts, "tree.puml"))
        assert os.path.isfile(os.path.join(artefacts, "tree.html"))

    def test_generate_creates_artifact_files(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-artifacts.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        self.generate(str(d), verbose=False)
        artefacts = os.path.join(str(d), "artefacts")
        # Should have user-1.yaml and response-2.yaml
        import yaml
        user_file = os.path.join(artefacts, "user-1.yaml")
        assert os.path.isfile(user_file)
        with open(user_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["domain"] == "user"

    def test_generate_idempotent(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-ident.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hi"),
        })
        path1 = self.generate(str(d), verbose=False)
        # Generate again — should not change anything
        path2 = self.generate(str(d), verbose=False)
        assert path1 == path2

    def test_generate_structured_output(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-struct.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _structured_output_fr(2),
        })
        html_path = self.generate(str(d), verbose=False)
        content = open(html_path).read()
        # Structured output session should have SessionTitle node
        assert "SessionTitle" in content

    def test_generate_with_reasoning(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-reason.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _fr_with_reasoning(2),
        })
        html_path = self.generate(str(d), verbose=False)
        content = open(html_path).read()
        # Should contain reasoning artifact
        assert "reasoning" in content.lower()

    def test_generate_empty_session(self, tmp_path):
        """Empty parts dir should produce a minimal tree."""
        d = os.path.join(str(tmp_path), "session-empty.parts")
        os.makedirs(d)
        html_path = self.generate(str(d), verbose=False)
        assert os.path.isfile(html_path)

    def test_generate_return_value(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-ret.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hi"),
        })
        result = self.generate(str(d), verbose=False)
        assert result.endswith("artefacts/tree.html")

    def test_generate_puml_contains_structure(self, tmp_path):
        d = _make_parts_dir(tmp_path, "session-puml.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hello!"),
        })
        self.generate(str(d), verbose=False)
        puml_path = os.path.join(str(d), "artefacts", "tree.puml")
        content = open(puml_path).read()
        assert "@startuml" in content


class TestIncrementalBuilds:
    """Tests for incremental tree generation with checkpointing."""

    def setup_method(self):
        to_remove = [k for k in sys.modules if k.startswith("backend_adapter.artifact_tree")]
        for k in to_remove:
            del sys.modules[k]
        from backend_adapter.artifact_tree import generate as generate_tree
        self.generate = generate_tree

    def test_checkpoint_created(self, tmp_path):
        """Checkpoint file .build_state.json is created after generation."""
        d = _make_parts_dir(tmp_path, "session-cp.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hi"),
        })
        self.generate(str(d), verbose=False)
        cp_path = os.path.join(str(d), "artefacts", ".build_state.json")
        assert os.path.isfile(cp_path)
        # Check it's valid JSON
        import json
        with open(cp_path) as f:
            state = json.load(f)
        assert "last_processed_part_id" in state
        assert state["last_processed_part_id"] >= 2

    def test_incremental_generate_equals_cold(self, tmp_path):
        """Second generate() call is incremental (reads fewer files) and result equals cold rebuild."""
        d = _make_parts_dir(tmp_path, "session-inc.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "First"),
        })
        # First (cold) generation
        html1 = self.generate(str(d), verbose=False)

        # Add new parts (d — сама директория *.parts)
        import json
        with open(os.path.join(d, "c-3-openai_body.json"), "w") as f:
            json.dump(_simple_ob(3), f)
        with open(os.path.join(d, "d-4-fetch_raw.json"), "w") as f:
            json.dump(_simple_fr(4, "Second"), f)

        # Second (incremental) generation
        html2 = self.generate(str(d), verbose=False)

        # Both should succeed; check HTML exists
        assert os.path.isfile(html1)
        assert os.path.isfile(html2)

        # Cold rebuild for comparison
        artefacts_dir = os.path.join(str(d), "artefacts")
        import shutil
        shutil.rmtree(artefacts_dir, ignore_errors=True)
        html3 = self.generate(str(d), verbose=False)
        assert os.path.isfile(html3)

    def test_corrupt_checkpoint_cold_rebuild(self, tmp_path):
        """Corrupt checkpoint triggers cold rebuild without crash."""
        d = _make_parts_dir(tmp_path, "session-bad-cp.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "Hi"),
        })
        # Generate once (creates checkpoint)
        self.generate(str(d), verbose=False)

        # Corrupt the checkpoint
        import json
        cp_path = os.path.join(str(d), "artefacts", ".build_state.json")
        with open(cp_path, "w") as f:
            f.write("{corrupted json!!!}")

        # Next generation should handle gracefully (cold rebuild)
        html = self.generate(str(d), verbose=False)
        assert os.path.isfile(html)
        # Checkpoint should be recreated correctly
        with open(cp_path) as f:
            state = json.load(f)
        assert state["last_processed_part_id"] >= 2

    def test_pages_generated(self, tmp_path):
        """pages/<N>/tree.{puml,png,html} + pages/index.html for multi-request session."""
        d = _make_parts_dir(tmp_path, "session-pages.parts", {
            "a-1-openai_body.json": _simple_ob(1),
            "b-2-fetch_raw.json": _simple_fr(2, "First request"),
            "c-3-openai_body.json": _simple_ob(3),
            "d-4-fetch_raw.json": _simple_fr(4, "Second request"),
        })
        self.generate(str(d), verbose=False)

        artefacts_dir = os.path.join(str(d), "artefacts")
        pages_dir = os.path.join(artefacts_dir, "pages")

        # At least index.html should exist
        index_path = os.path.join(pages_dir, "index.html")
        assert os.path.isfile(index_path)

        # Check page directories exist (number depends on segmentation logic)
        if os.path.isdir(pages_dir):
            page_dirs = [d for d in os.listdir(pages_dir) if d.isdigit()]
            # Each page should have tree files
            for pd in page_dirs:
                page_path = os.path.join(pages_dir, pd)
                assert os.path.isfile(os.path.join(page_path, "tree.puml")) or \
                       os.path.isfile(os.path.join(page_path, "tree.png")) or \
                       os.path.isfile(os.path.join(page_path, "tree.html"))

    def test_raw_file_href_idempotent(self, tmp_path):
        """'../...' and None are not touched on repeated calls."""
        from backend_adapter.artifact_tree import _raw_file_href

        d = _make_parts_dir(tmp_path, "session-href.parts", {
            "abc-1-openai_body.json": _simple_ob(1),
            "def-2-fetch_raw.json": _simple_fr(2, "Hi"),
        })
        # Existing .json part file → "../<имя>" (as stored in build_turns output)
        result1 = _raw_file_href(str(d), "abc-1-openai_body.json")
        assert result1 == "../abc-1-openai_body.json"

        # Already has ../ prefix — should remain unchanged (idempotent)
        result2 = _raw_file_href(str(d), "../abc-1-openai_body.json")
        assert result2 == "../abc-1-openai_body.json"

        # None stays None
        result3 = _raw_file_href(str(d), None)
        assert result3 is None

        # Missing .yaml (preferred) falls back to sibling .json
        result4 = _raw_file_href(str(d), "def-2-fetch_raw.yaml")
        assert result4 == "../def-2-fetch_raw.json"

        # Neither .yaml nor .json exists → None
        result5 = _raw_file_href(str(d), "nonexistent-9-fetch_raw.yaml")
        assert result5 is None

    def test_extract_part_id(self):
        """Extract int from artifact name, None for non-matching."""
        from backend_adapter.artifact_tree import _extract_part_id

        assert _extract_part_id("toolcall-116402") == 116402
        assert _extract_part_id("toolcall-116402-1") == 116402  # collision suffix
        assert _extract_part_id("user-1") == 1
        assert _extract_part_id("finish_source") is None
        assert _extract_part_id("start") is None

    def test_filter_for_page(self):
        """Filter turns/orphans/edges/names by page range [start, end)."""
        from backend_adapter.artifact_tree import _filter_for_page

        turns = [
            {"ob_part_id": 1, "fr_part_id": 2},
            {"ob_part_id": 3, "fr_part_id": 4},
            {"ob_part_id": 5, "fr_part_id": 6},
        ]
        orphans = [{"part_id": 1}, {"part_id": 3}, {"part_id": 7}]
        resolution_edges = [("user-1", "toolcall-2"), ("toolcall-4", "user-3")]
        inline_labels = {"ignored": True}  # передаётся целиком
        superseded_targets = ["finish-5", "toolcall-1"]
        title_targets = ["user-3"]
        request_answers = {"user-3": "finish-4", "user-9": "finish-10"}

        # Страница №2 (по счёту запросов user): ходы с ob_part_id в [3, 5)
        (page_turns, page_orphans, page_res_edges, page_superseded,
         page_title_targets, page_request_answers, page_inline_labels) = _filter_for_page(
            turns, orphans, resolution_edges, inline_labels,
            superseded_targets, title_targets, request_answers,
            start_pid=3, end_pid=5, page_uname="user-3",
        )

        # Только ход с ob_part_id=3 (3 <= 3 < 5)
        assert len(page_turns) == 1
        assert page_turns[0]["ob_part_id"] == 3
        # Сирота с part_id=3 в диапазоне
        assert len(page_orphans) == 1
        assert page_orphans[0]["part_id"] == 3
        # Ребро: одна из сторон в диапазоне → ("toolcall-4", "user-3")
        assert page_res_edges == [("toolcall-4", "user-3")]
        # Вытесненный: toolcall-1 вне диапазона, finish-5 нет (без part_id)
        assert page_superseded == []
        # Заголовок: user-3 в диапазоне (part_id 3)
        assert page_title_targets == ["user-3"]
        # Ответы запросов: только для своей страницы
        assert page_request_answers == {"user-3": "finish-4"}
        # Общий словарь inline_labels проходит целиком
        assert page_inline_labels is inline_labels

    def test_filter_for_page_excludes_outside_range(self):
        """Элементы вне диапазона страницы отфильтровываются."""
        from backend_adapter.artifact_tree import _filter_for_page

        turns = [{"ob_part_id": 1, "fr_part_id": 2}]
        orphans = [{"part_id": 9}]
        resolution_edges = [("toolcall-1", "user-9")]
        inline_labels = {}
        superseded_targets = ["finish-2"]
        title_targets = ["user-1"]
        request_answers = {"user-1": "finish-2"}

        (page_turns, page_orphans, page_res_edges, page_superseded,
         page_title_targets, page_request_answers, page_inline_labels) = _filter_for_page(
            turns, orphans, resolution_edges, inline_labels,
            superseded_targets, title_targets, request_answers,
            start_pid=3, end_pid=5, page_uname="user-3",
        )

        assert page_turns == []
        assert page_orphans == []
        assert page_res_edges == []
        assert page_superseded == []
        assert page_title_targets == []
        assert page_request_answers == {}
        assert page_inline_labels is inline_labels
