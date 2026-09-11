#!/usr/bin/env python3
"""Unit tests for backend_adapter.session_registry — таблица сессий WEBUI.

Строка = кортеж (session, agent, model, backend, route); смена модели или
обработчика создаёт НОВУЮ строку, возврат к прежнему кортежу — ту же
(calls+1).

Tests cover:
  - register: создаёт строку с полями схемы (calls=1/errors=0), повторный
    register тем же кортежем инкрементит calls и обновляет last_seen
  - составной ключ: смена model / route / backend / agent → отдельная строка
  - возврат к прежнему кортежу → та же строка, calls+1 (upsert)
  - пустой/ложный session_id — no-op (None); выключенный лимит (<= 0) — None
  - эвикция: держим не больше _limit() СТРОК, вытесняется самая старая по _ts
  - _limit читает config.ADAPTER_SESSIONS_TABLE живьём (переживает reload)
  - record_error: инкремент по ключу; None/чужой ключ — no-op
  - sessions_snapshot: копии строк, порядок по _ts desc (новые сверху),
    всплытие строки при новом обращении, без служебного _ts, поле "key"
  - снимок — копия: мутация выдачи не меняет реестр
  - delete_key: удаляет существующую строку (True), отсутствующую — False,
    не трогает соседние строки; повторное удаление — False

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


def _reg(reg, session="sess-1", agent="agent", model="m", backend="be", route="r"):
    """Короткая обёртка: register с явными model/backend/route."""
    return reg.register(session, agent, model=model, backend=backend, route=route)


class TestRegister:
    def test_creates_row_with_schema(self, reg):
        key = _reg(reg, model="", backend="", route="")
        rows = reg.sessions_snapshot()
        assert len(rows) == 1
        row = rows[0]
        assert row["session"] == "sess-1"
        assert row["agent"] == "agent"
        assert row["model"] == ""
        assert row["backend"] == ""
        assert row["route"] == ""
        assert row["calls"] == 1
        assert row["errors"] == 0
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", row["last_seen"])
        assert key == ("sess-1", "agent", "", "", "")

    def test_repeat_same_tuple_increments_calls(self, reg):
        _reg(reg)
        _reg(reg)
        rows = reg.sessions_snapshot()
        assert len(rows) == 1
        assert rows[0]["calls"] == 2

    def test_changed_model_creates_new_row(self, reg):
        # Смена модели агентом — новое событие → новая строка.
        _reg(reg, model="m1")
        _reg(reg, model="m2")
        rows = reg.sessions_snapshot()
        assert len(rows) == 2
        assert {r["model"] for r in rows} == {"m1", "m2"}

    def test_changed_route_creates_new_row(self, reg):
        # Смена обработчика (правила TARGET-маршрутизации) — новая строка.
        _reg(reg, route="convert messages→completions")
        _reg(reg, route="passthrough messages→messages")
        rows = reg.sessions_snapshot()
        assert len(rows) == 2
        assert {r["route"] for r in rows} == {
            "convert messages→completions",
            "passthrough messages→messages",
        }

    def test_changed_backend_creates_new_row(self, reg):
        _reg(reg, backend="be1")
        _reg(reg, backend="be2")
        assert len(reg.sessions_snapshot()) == 2

    def test_changed_agent_creates_new_row(self, reg):
        # Agent входит в ключ: смена версии CLI — тоже новая строка.
        _reg(reg, agent="claude-cli/2.1.236")
        _reg(reg, agent="claude-cli/2.1.240")
        rows = reg.sessions_snapshot()
        assert len(rows) == 2
        assert {r["agent"] for r in rows} == {"claude-cli/2.1.236", "claude-cli/2.1.240"}

    def test_return_to_previous_tuple_reuses_row(self, reg):
        # m1 → m2 → m1: возврат к прежнему кортежу — ТА ЖЕ строка, calls+1.
        _reg(reg, model="m1")
        _reg(reg, model="m2")
        _reg(reg, model="m1")
        rows = reg.sessions_snapshot()
        assert len(rows) == 2
        m1 = next(r for r in rows if r["model"] == "m1")
        assert m1["calls"] == 2
        assert next(r for r in rows if r["model"] == "m2")["calls"] == 1

    def test_returns_key_for_record_error(self, reg):
        key = _reg(reg)
        reg.record_error(key)
        assert reg.sessions_snapshot()[0]["errors"] == 1

    def test_empty_session_id_is_noop(self, reg):
        assert _reg(reg, session="") is None
        assert reg.sessions_snapshot() == []

    def test_disabled_limit_is_noop(self, reg):
        with mock.patch("backend_adapter.config.ADAPTER_SESSIONS_TABLE", 0):
            assert _reg(reg) is None
        assert reg.sessions_snapshot() == []

    def test_limit_read_live(self, reg):
        # Лимит читается через атрибут модуля config при каждой регистрации —
        # смена значения действует на следующем же register (как
        # routing.target_for_input читает config живьём, переживая reload).
        _reg(reg, session="sess-0")
        time.sleep(0.01)
        with mock.patch("backend_adapter.config.ADAPTER_SESSIONS_TABLE", 1):
            _reg(reg, session="sess-1")
        sessions = [r["session"] for r in reg.sessions_snapshot()]
        assert sessions == ["sess-1"]  # лимит 1 вытеснил sess-0

    def test_eviction_counts_rows_not_sessions(self, reg):
        # Лимит считает строки (пары), а не сессии: три строки одной сессии
        # при лимите 2 вытесняют самую старую строку той же сессии.
        with mock.patch("backend_adapter.config.ADAPTER_SESSIONS_TABLE", 2):
            for i in range(3):
                _reg(reg, session="sess-1", model=f"m{i}")
                time.sleep(0.01)  # разные _ts
        rows = reg.sessions_snapshot()
        assert [r["model"] for r in rows] == ["m2", "m1"]  # m0 вытеснена


class TestRecordError:
    def test_increments_by_key(self, reg):
        key = _reg(reg)
        reg.record_error(key)
        reg.record_error(key)
        assert reg.sessions_snapshot()[0]["errors"] == 2

    def test_none_key_is_noop(self, reg):
        _reg(reg)
        reg.record_error(None)
        assert reg.sessions_snapshot()[0]["errors"] == 0

    def test_unknown_key_is_noop(self, reg):
        _reg(reg)
        reg.record_error(("ghost", "a", "m", "b", "r"))
        assert reg.sessions_snapshot()[0]["errors"] == 0

    def test_error_targets_only_its_row(self, reg):
        # Ошибка ложится в строку своего кортежа, не задевая соседнюю.
        key1 = _reg(reg, model="m1")
        _reg(reg, model="m2")
        reg.record_error(key1)
        rows = {r["model"]: r for r in reg.sessions_snapshot()}
        assert rows["m1"]["errors"] == 1
        assert rows["m2"]["errors"] == 0


class TestDeleteKey:
    def test_deletes_existing_row(self, reg):
        key = _reg(reg, session="s-del")
        assert reg.sessions_snapshot()
        assert reg.delete_key(key) is True
        assert reg.sessions_snapshot() == []

    def test_missing_key_returns_false(self, reg):
        # Строки нет (или уже удалена) — False, исключений нет.
        assert reg.delete_key(("ghost", "a", "m", "b", "r")) is False

    def test_delete_is_not_idempotent(self, reg):
        # Повторное удаление той же строки — False (она уже исчезла).
        key = _reg(reg)
        assert reg.delete_key(key) is True
        assert reg.delete_key(key) is False

    def test_deletes_only_target_row(self, reg):
        # Удаляется ровно одна строка-кортеж, соседние (в т.ч. той же
        # сессии, но с другой моделью) остаются.
        keep = _reg(reg, session="s", model="m1")
        drop = _reg(reg, session="s", model="m2")
        assert reg.delete_key(drop) is True
        rows = reg.sessions_snapshot()
        assert [r["model"] for r in rows] == ["m1"]
        assert keep is not None and reg.delete_key(keep) is True
        assert reg.sessions_snapshot() == []

    def test_no_exceptions_escape(self, reg):
        # Удаление не должно ронять запрос: исключение внутри глушится → False.
        with mock.patch.object(reg, "_TABLE", None):
            assert reg.delete_key(("s", "a", "m", "b", "r")) is False


class TestSnapshot:
    def test_empty_snapshot(self, reg):
        assert reg.sessions_snapshot() == []

    def test_newest_first(self, reg):
        _reg(reg, session="old")
        time.sleep(0.01)
        _reg(reg, session="new")
        sessions = [r["session"] for r in reg.sessions_snapshot()]
        assert sessions == ["new", "old"]

    def test_row_floats_up_on_new_call(self, reg):
        _reg(reg, session="old")
        time.sleep(0.01)
        _reg(reg, session="new")
        time.sleep(0.01)
        _reg(reg, session="old")  # всплытие
        sessions = [r["session"] for r in reg.sessions_snapshot()]
        assert sessions == ["old", "new"]
        assert reg.sessions_snapshot()[0]["calls"] == 2

    def test_no_service_ts_field(self, reg):
        _reg(reg)
        assert "_ts" not in reg.sessions_snapshot()[0]

    def test_key_field_is_json_tuple(self, reg):
        # Служебное поле "key" — компактная JSON-строка кортежа (дискриминатор
        # строки для JS): одна и та же у data-key HTML и у поля снимка.
        _reg(reg, session="s", agent="a", model="m", backend="b", route="passthrough")
        row = reg.sessions_snapshot()[0]
        assert row["key"] == '["s","a","m","b","passthrough"]'
        assert reg.key_json(("s", "a", "m", "b", "passthrough")) == row["key"]

    def test_key_distinguishes_rows_of_same_session(self, reg):
        # Две строки одной сессии различимы по key (session не уникален).
        _reg(reg, session="s", model="m1")
        _reg(reg, session="s", model="m2")
        keys = [r["key"] for r in reg.sessions_snapshot()]
        assert len(set(keys)) == 2

    def test_snapshot_is_copy(self, reg):
        _reg(reg)
        rows = reg.sessions_snapshot()
        rows[0]["calls"] = 999
        rows[0]["agent"] = "mutated"
        fresh = reg.sessions_snapshot()[0]
        assert fresh["calls"] == 1
        assert fresh["agent"] == "agent"

    def test_no_exceptions_escape(self, reg):
        # Учёт не должен ронять запрос: исключения внутри глушатся.
        with mock.patch("backend_adapter.config.ADAPTER_SESSIONS_TABLE", None):
            _reg(reg)  # int(None) → TypeError
        with mock.patch.object(reg, "_TABLE", None):
            _reg(reg, session="sess-2")
            reg.record_error(("sess-2", "agent", "m", "be", "r"))
