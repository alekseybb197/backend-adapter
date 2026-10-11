import itertools
import json
import random

import pytest

from sniffer import TextEvent, TextToolCallSniffer, ToolCallEvent


def run(pieces):
    s = TextToolCallSniffer()
    events = []
    for p in pieces:
        events += s.feed(p)
    events += s.finish()
    text = "".join(e.text for e in events if isinstance(e, TextEvent))
    calls = [(e.name, e.arguments) for e in events if isinstance(e, ToolCallEvent)]
    return text, calls, events


CALL = '<tool_call>\n{"name": "Bash", "arguments": {"command": "ls -la"}}\n</tool_call>'
DOC = "Сейчас посмотрю файлы. " + CALL + " Готово."


def all_splits(s, max_cuts=2):
    """Все разбиения строки не более чем на max_cuts+1 кусков."""
    n = len(s)
    for k in range(max_cuts + 1):
        for cuts in itertools.combinations(range(1, n), k):
            bounds = (0, *cuts, n)
            yield [s[a:b] for a, b in zip(bounds, bounds[1:])]


def test_exhaustive_two_cut_splits_give_same_result():
    """Тег, разрезанный на любой границе (≤2 разрезов), распознаётся одинаково."""
    expected = ("Сейчас посмотрю файлы.  Готово.", [("Bash", {"command": "ls -la"})])
    count = 0
    for pieces in all_splits(DOC, max_cuts=2):
        text, calls, _ = run(pieces)
        assert (text, calls) == expected, pieces
        count += 1
    assert count > 5000


def test_random_char_level_streams():
    rng = random.Random(7)
    doc = "a<b " + CALL + " x <tool_c y" + CALL.replace("Bash", "Read") + "</tool_call> z"
    want_text, want_calls, _ = run([doc])
    assert [c[0] for c in want_calls] == ["Bash", "Read"]
    for _ in range(2000):
        pieces, i = [], 0
        while i < len(doc):
            j = i + rng.randint(1, 6)
            pieces.append(doc[i:j])
            i = j
        assert run(pieces)[:2] == (want_text, want_calls)


def test_plain_text_passes_through_immediately():
    s = TextToolCallSniffer()
    assert [e.text for e in s.feed("hello world")] == ["hello world"]
    # «<» в обычном тексте придерживается ровно на один фрагмент
    assert [e.text for e in s.feed("a < b")] == ["a < b"]
    assert [e.text for e in s.feed("x <tool")] == ["x "]
    assert [e.text for e in s.feed("box>")] == ["<toolbox>"]


@pytest.mark.parametrize(
    "bad",
    [
        "<tool_call>not json</tool_call>",
        '<tool_call>{"arguments": {}}</tool_call>',  # нет name
        '<tool_call>{"name": "X", "arguments": "{broken"}</tool_call>',
        '<tool_call>["list"]</tool_call>',
    ],
)
def test_unparseable_tag_is_returned_as_text(bad):
    text, calls, _ = run([bad])
    assert calls == [] and text == bad


def test_unterminated_tag_flushed_as_text_at_finish():
    text, calls, _ = run(["start <tool_call>{\"name\": \"X\""])
    assert calls == [] and text == 'start <tool_call>{"name": "X"'


def test_function_wrapper_and_string_arguments():
    body = {"function": {"name": "Edit", "arguments": json.dumps({"path": "a.py"})}}
    _, calls, _ = run([f"<tool_call>{json.dumps(body)}</tool_call>"])
    assert calls == [("Edit", {"path": "a.py"})]


def test_identical_calls_get_distinct_ids():
    _, _, events = run([CALL + CALL])
    ids = [e.id for e in events if isinstance(e, ToolCallEvent)]
    assert len(ids) == 2 and ids[0] != ids[1]


def test_oversized_tag_is_released_as_text():
    s = TextToolCallSniffer(max_tag_bytes=100)
    out = s.feed("<tool_call>" + "x" * 500)
    assert out and isinstance(out[0], TextEvent) and out[0].text.startswith("<tool_call>xxx")
    assert s.feed("tail") == [TextEvent("tail")]
