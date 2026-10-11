"""Инкрементальный распознаватель текстовых вызовов инструментов Qwen
(``<tool_call>{json}</tool_call>``) в SSE-потоке. Прототип для ADR-038.

Текст пропускается клиенту сразу, кроме минимального «хвоста», который может
оказаться началом тега: он придерживается до следующего фрагмента. Содержимое
тега копится целиком; на закрывающем теге JSON разбирается и превращается в
событие вызова. Неразбираемый тег и незакрытый к концу потока тег отдаются
клиенту как обычный текст — ничего не теряется молча.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field

OPEN = "<tool_call>"
CLOSE = "</tool_call>"


@dataclass
class TextEvent:
    text: str


@dataclass
class ToolCallEvent:
    id: str
    name: str
    arguments: dict
    raw: str = field(repr=False)


Event = TextEvent | ToolCallEvent


def _partial_prefix_len(s: str, tag: str) -> int:
    """Длина самого длинного суффикса s, являющегося собственным префиксом tag."""
    for n in range(min(len(s), len(tag) - 1), 0, -1):
        if s.endswith(tag[:n]):
            return n
    return 0


def _parse_call(body: str) -> ToolCallEvent | None:
    try:
        data = json.loads(body.strip())
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    fn = data.get("function") if isinstance(data.get("function"), dict) else {}
    name = data.get("name") or fn.get("name")
    args = data.get("arguments", fn.get("arguments", data.get("parameters", {})))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(name, str) or not name or not isinstance(args, dict):
        return None
    # uuid, а не hash(): одинаковые вызовы в одном ответе обязаны получить
    # разные tool_use_id (требование протокола Anthropic).
    return ToolCallEvent(id=f"call_{uuid.uuid4().hex[:24]}", name=name, arguments=args, raw=body)


class TextToolCallSniffer:
    def __init__(self, max_tag_bytes: int = 1 << 20) -> None:
        self._pending = ""  # придержанный хвост вне тега (возможное начало OPEN)
        self._inside = False
        self._body: list[str] = []
        self._body_len = 0
        self._max = max_tag_bytes  # защита памяти от бесконечного «тега»
        self.calls = 0

    def feed(self, piece: str) -> list[Event]:
        out: list[Event] = []
        s = self._pending + piece
        self._pending = ""
        while s:
            if not self._inside:
                i = s.find(OPEN)
                if i < 0:
                    keep = _partial_prefix_len(s, OPEN)
                    if len(s) > keep:
                        out.append(TextEvent(s[: len(s) - keep]))
                    self._pending = s[len(s) - keep :]
                    break
                if i:
                    out.append(TextEvent(s[:i]))
                s = s[i + len(OPEN) :]
                self._inside, self._body, self._body_len = True, [], 0
            else:
                j = s.find(CLOSE)
                if j < 0:
                    keep = _partial_prefix_len(s, CLOSE)
                    self._append(s[: len(s) - keep])
                    self._pending = s[len(s) - keep :]
                    if self._body_len > self._max:  # не тег, а мусор — отдать как текст
                        out.append(TextEvent(OPEN + "".join(self._body) + self._pending))
                        self._inside, self._body, self._body_len, self._pending = False, [], 0, ""
                    break
                self._append(s[:j])
                body = "".join(self._body)
                s = s[j + len(CLOSE) :]
                self._inside, self._body, self._body_len = False, [], 0
                call = _parse_call(body)
                if call is None:
                    out.append(TextEvent(OPEN + body + CLOSE))
                else:
                    self.calls += 1
                    out.append(call)
        return out

    def finish(self) -> list[Event]:
        """Конец потока: всё придержанное и незакрытое — обычным текстом."""
        rest = (OPEN + "".join(self._body) if self._inside else "") + self._pending
        self._pending, self._inside, self._body, self._body_len = "", False, [], 0
        return [TextEvent(rest)] if rest else []

    def _append(self, part: str) -> None:
        if part:
            self._body.append(part)
            self._body_len += len(part)
