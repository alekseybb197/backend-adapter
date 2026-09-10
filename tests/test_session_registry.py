#!/usr/bin/env python3
"""Unit tests for backend_adapter.session_registry — таблица сессий WEBUI.

Tests cover:
  - touch: создаёт строку с полями схемы (agent/last_seen/calls=1/errors=0),
    повторный touch инкрементит calls и обновляет agent/last_seen
  - пустой/ложный session_id — no-op; выключенный лимит (<= 0) — no-op
  - эвикция: держим не больше _limit() строк, вытесняется самая старая по _ts
  - _limit читает config.ADAPTER_SESSIONS_TABLE живьём (переживает reload)
  - set_route/record_error: no-op для незарегистрированной сессии
  - sessions_snapshot: копии строк, порядок по _ts desc (новые сверху),
    всплытие старой сессии при новом обращении, без служебного _ts
  - снимок — копия: мутация выдачи не меняет реестр

Модуль берётся фикстурой (не импортом на уровне файла): autouse fresh_env
удаляет backend_adapter* из sys.modules и переимпортирует config перед каждым
тестом — верхнеуровневая ссылка указывала бы на устаревший экземпляр.
"""
import re
import time
from unittest import mock

import pytest


@pytest.fixture
def reg():
    """Свежий (после fresh_env) экземпляр session_registry."""
    from backend_adapter import session_registry

    return session_registry


class TestTouch:
    def test_creates_row_with_schema(self, reg):
        reg.touch("sess-1", "claude-cli/2.1.236")
        rows = reg.sessions_snapshot()
        assert len(rows) == 1
        row = rows[0]
        assert row["session"] == "sess-1"
        assert row["agent"] == "claude-cli/2.1.236"
        assert row["model"] == ""
        assert row["backend"] == ""
        assert row["route"] == ""
        assert row["calls"] == 1
        assert row["errors"] == 0
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["last_seen"])

    def test_repeat_touch_increments_calls_and_updates_agent(self, reg):
        reg.touch("sess-1", "agent-a")
        reg.touch("sess-1", "agent-b")
        rows = reg.sessions_snapshot()
        assert len(rows) == 1
        assert rows[0]["calls"] == 2
        assert rows[0]["agent"] == "agent-b"

    def test_empty_session_id_is_noop(self, reg):
        reg.touch("", "agent")
        assert reg.sessions_snapshot() == []

    def test_disabled_limit_is_noop(self, reg):
        with mock.patch("backend_adapter.config.ADAPTER_SESSIONS_TABLE", 0):
            reg.touch("sess-1", "agent")
        assert reg.sessions_snapshot() == []

    def test_limit_read_live(self, reg):
        # Лимит читается через атрибут модуля config при каждой регистрации —
        # смена значения действует на следующем же touch (как
        # routing.target_for_input читает config живьём, переживая reload).
        reg.touch("sess-0", "agent")
        time.sleep(0.01)
        with mock.patch("backend_adapter.config.ADAPTER_SESSIONS_TABLE", 1):
            reg.touch("sess-1", "agent")
        sessions = [r["session"] for r in reg.sessions_snapshot()]
        assert sessions == ["sess-1"]  # лимит 1 вытеснил sess-0

    def test_eviction_keeps_oldest_out(self, reg):
        with mock.patch("backend_adapter.config.ADAPTER_SESSIONS_TABLE", 2):
            for i in range(3):
                reg.touch(f"sess-{i}", "agent")
                time.sleep(0.01)  # разные _ts
        sessions = [r["session"] for r in reg.sessions_snapshot()]
        assert sessions == ["sess-2", "sess-1"]  # sess-0 вытеснена


class TestSetRouteAndErrors:
    def test_set_route_updates_row(self, reg):
        reg.touch("sess-1", "agent")
        reg.set_route(
            "sess-1", model="m", backend="be", route="passthrough messages→messages"
        )
        row = reg.sessions_snapshot()[0]
        assert row["model"] == "m"
        assert row["backend"] == "be"
        assert row["route"] == "passthrough messages→messages"

    def test_set_route_unknown_session_is_noop(self, reg):
        reg.touch("sess-1", "agent")
        reg.set_route("ghost", model="m", backend="be", route="r")
        assert reg.sessions_snapshot()[0]["model"] == ""

    def test_record_error_increments(self, reg):
        reg.touch("sess-1", "agent")
        reg.record_error("sess-1")
        reg.record_error("sess-1")
        assert reg.sessions_snapshot()[0]["errors"] == 2

    def test_record_error_unknown_session_is_noop(self, reg):
        reg.touch("sess-1", "agent")
        reg.record_error("ghost")
        assert reg.sessions_snapshot()[0]["errors"] == 0


class TestSnapshot:
    def test_empty_snapshot(self, reg):
        assert reg.sessions_snapshot() == []

    def test_newest_first(self, reg):
        reg.touch("old", "a")
        time.sleep(0.01)
        reg.touch("new", "a")
        sessions = [r["session"] for r in reg.sessions_snapshot()]
        assert sessions == ["new", "old"]

    def test_old_session_floats_up_on_new_call(self, reg):
        reg.touch("old", "a")
        time.sleep(0.01)
        reg.touch("new", "a")
        time.sleep(0.01)
        reg.touch("old", "a")  # всплытие
        sessions = [r["session"] for r in reg.sessions_snapshot()]
        assert sessions == ["old", "new"]
        assert reg.sessions_snapshot()[0]["calls"] == 2

    def test_no_service_ts_field(self, reg):
        reg.touch("sess-1", "agent")
        assert "_ts" not in reg.sessions_snapshot()[0]

    def test_snapshot_is_copy(self, reg):
        reg.touch("sess-1", "agent")
        rows = reg.sessions_snapshot()
        rows[0]["calls"] = 999
        rows[0]["agent"] = "mutated"
        fresh = reg.sessions_snapshot()[0]
        assert fresh["calls"] == 1
        assert fresh["agent"] == "agent"

    def test_no_exceptions_escape(self, reg):
        # Учёт не должен ронять запрос: исключения внутри глушатся.
        with mock.patch("backend_adapter.config.ADAPTER_SESSIONS_TABLE", None):
            reg.touch("sess-1", "agent")  # int(None) → TypeError
        with mock.patch.object(reg, "_TABLE", None):
            reg.touch("sess-2", "agent")
            reg.set_route("sess-2", model="m", backend="b", route="r")
            reg.record_error("sess-2")
