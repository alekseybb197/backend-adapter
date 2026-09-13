"""Tests for backend_adapter.session_settings — пер-сессионные переопределения
настроек (v0.9.5).

Модуль — лист DAG: на верхнем уровне импортирует только ``config``. Хранит
таблицу ``session_id → {имя_настройки: значение}`` в памяти процесса; три
состояния настройки — НЕ ЗАДАНА (наследует общее), ``"inherit"`` (только
TARGET-поля: запись есть, но трактуется как «взять общее») и конкретное
значение.

Модуль берётся фикстурой (не импортом на уровне файла): autouse isolate_logs
(→ fresh_env) пересоздаёт backend_adapter-модули перед каждым тестом —
верхнеуровневая ссылка указывала бы на устаревший экземпляр (тот же паттерн,
что tests/test_session_registry.py).
"""
from unittest import mock

import pytest


@pytest.fixture
def ss():
    """Свежий (после fresh_env) экземпляр session_settings."""
    from backend_adapter import session_settings

    return session_settings


@pytest.fixture
def cfg():
    """Свежий экземпляр config (для проверки живой связи с общими настройками)."""
    from backend_adapter import config

    return config


class TestOverride:
    """override() — сырое хранимое значение (включая "inherit")."""

    def test_unset_returns_none(self, ss):
        assert ss.override("sess1", "ADAPTER_DEBUG") is None

    def test_empty_session_id_returns_none(self, ss):
        assert ss.override("", "ADAPTER_DEBUG") is None

    def test_returns_stored_value(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        assert ss.override("sess1", "ADAPTER_DEBUG") is True

    def test_returns_inherit_as_stored(self, ss):
        # "inherit" у TARGET — полноценное хранимое значение: override его
        # отдаёт как есть, трактовку «взять общее» делает вызывающий.
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "inherit"})
        assert ss.override("sess1", "ADAPTER_MESSAGES_TARGET") == "inherit"

    def test_unknown_name_returns_none(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        assert ss.override("sess1", "NOT_A_SETTING") is None

    def test_isolated_between_sessions(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        assert ss.override("sess2", "ADAPTER_DEBUG") is None


class TestEffective:
    """effective() — переопределение поверх общей настройки (живое чтение)."""

    def test_falls_back_to_config(self, ss, cfg):
        cfg.ADAPTER_DEBUG = True
        assert ss.effective("sess1", "ADAPTER_DEBUG") is True

    def test_override_wins(self, ss, cfg):
        cfg.ADAPTER_DEBUG = False
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        assert ss.effective("sess1", "ADAPTER_DEBUG") is True
        # Соседняя сессия — общая настройка (изоляция).
        assert ss.effective("sess2", "ADAPTER_DEBUG") is False

    def test_inherit_uses_config(self, ss, cfg):
        cfg.ADAPTER_MESSAGES_TARGET = "passthrough"
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "inherit"})
        assert ss.effective("sess1", "ADAPTER_MESSAGES_TARGET") == "passthrough"

    def test_reads_config_live(self, ss, cfg):
        # Общая настройка меняется ПОСЛЕ записи "inherit" — сессия видит новое
        # значение (живое чтение атрибута config, не снимок).
        cfg.ADAPTER_MESSAGES_TARGET = "passthrough"
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "inherit"})
        cfg.ADAPTER_MESSAGES_TARGET = "none"
        assert ss.effective("sess1", "ADAPTER_MESSAGES_TARGET") == "none"

    def test_concrete_value_ignores_config_change(self, ss, cfg):
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        cfg.ADAPTER_MESSAGES_TARGET = "none"
        assert ss.effective("sess1", "ADAPTER_MESSAGES_TARGET") == "passthrough"

    def test_empty_session_id_uses_config(self, ss, cfg):
        cfg.ADAPTER_DEBUG = True
        assert ss.effective("", "ADAPTER_DEBUG") is True


class TestSetConfig:
    """set_config() — запись/снятие переопределений (валидация молчаливая)."""

    def test_applies_valid_bool(self, ss):
        result = ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        assert result == {"ADAPTER_DEBUG": True}
        assert ss.override("sess1", "ADAPTER_DEBUG") is True

    def test_applies_valid_enum(self, ss):
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        assert ss.override("sess1", "ADAPTER_MESSAGES_TARGET") == "passthrough"

    def test_applies_inherit_for_target(self, ss):
        # "inherit" входит в домен TARGET-полей (SESSION_TARGET_VALUES).
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "inherit"})
        assert ss.override("sess1", "ADAPTER_MESSAGES_TARGET") == "inherit"

    def test_name_outside_pool_ignored(self, ss):
        # Имя вне config.SESSION_CONFIG_POOL молча игнорируется.
        result = ss.set_config("sess1", {"ADAPTER_DEBUG_TRIM": 500})
        assert result == {}
        assert ss.session_overrides("sess1") == {}

    def test_wrong_type_ignored(self, ss):
        # Строка в bool-настройку не проходит accepts_value → игнор.
        result = ss.set_config("sess1", {"ADAPTER_DEBUG": "yes"})
        assert result == {}

    def test_wrong_enum_value_ignored(self, ss):
        # Значение вне домена TARGET молча игнорируется.
        result = ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "bogus"})
        assert result == {}

    def test_bool_not_accepted_for_enum(self, ss):
        result = ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": True})
        assert result == {}

    def test_mixed_valid_and_invalid(self, ss):
        # Невалидное имя/значение игнорируются, валидное — применяется.
        result = ss.set_config(
            "sess1",
            {"ADAPTER_DEBUG": True, "NOT_A_SETTING": 1, "ADAPTER_DEBUG_PARTS": "no"},
        )
        assert result == {"ADAPTER_DEBUG": True}

    def test_clear_removes_override(self, ss, cfg):
        cfg.ADAPTER_DEBUG = False
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        ss.set_config("sess1", clear=("ADAPTER_DEBUG",))
        assert ss.override("sess1", "ADAPTER_DEBUG") is None
        assert ss.effective("sess1", "ADAPTER_DEBUG") is False  # снова наследует

    def test_clear_unknown_name_is_noop(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        ss.set_config("sess1", clear=("NOT_A_SETTING",))
        assert ss.override("sess1", "ADAPTER_DEBUG") is True

    def test_returns_copy_not_internal_dict(self, ss):
        # Возврат — КОПИЯ: мутация результата не трогает хранилище.
        result = ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        assert result is not None
        result["ADAPTER_DEBUG"] = False
        assert ss.override("sess1", "ADAPTER_DEBUG") is True

    def test_empty_session_id_returns_none(self, ss):
        # Сессия не идентифицирована — переопределять нечего.
        assert ss.set_config("", {"ADAPTER_DEBUG": True}) is None

    def test_empty_row_dropped(self, ss):
        # Все переопределения сняты → запись сессии удаляется целиком
        # (пустых строк в таблице не остаётся).
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        result = ss.set_config("sess1", clear=("ADAPTER_DEBUG",))
        assert result == {}
        assert ss.session_overrides("sess1") == {}


class TestSessionOverrides:
    def test_empty_for_unknown(self, ss):
        assert ss.session_overrides("nobody") == {}

    def test_empty_session_id(self, ss):
        assert ss.session_overrides("") == {}

    def test_returns_copy(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        snap = ss.session_overrides("sess1")
        snap["ADAPTER_DEBUG"] = False
        assert ss.override("sess1", "ADAPTER_DEBUG") is True

    def test_multiple_settings(self, ss):
        ss.set_config(
            "sess1",
            {"ADAPTER_DEBUG": True, "ADAPTER_MESSAGES_TARGET": "passthrough"},
        )
        assert ss.session_overrides("sess1") == {
            "ADAPTER_DEBUG": True,
            "ADAPTER_MESSAGES_TARGET": "passthrough",
        }


class TestClearSession:
    def test_clears_all_and_returns_true(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True, "ADAPTER_DEBUG_PARTS": True})
        assert ss.clear_session("sess1") is True
        assert ss.session_overrides("sess1") == {}

    def test_false_when_nothing_to_clear(self, ss):
        assert ss.clear_session("sess1") is False

    def test_empty_session_id_false(self, ss):
        assert ss.clear_session("") is False

    def test_does_not_touch_other_sessions(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        ss.set_config("sess2", {"ADAPTER_DEBUG": True})
        ss.clear_session("sess1")
        assert ss.session_overrides("sess1") == {}
        assert ss.session_overrides("sess2") == {"ADAPTER_DEBUG": True}


class TestReset:
    def test_wipes_table(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        ss.set_config("sess2", {"ADAPTER_DEBUG_PARTS": True})
        ss.reset()
        assert ss.session_overrides("sess1") == {}
        assert ss.session_overrides("sess2") == {}


class TestSurvivesRowEviction:
    """Переопределения адресуются session_id, а не строке-кортежу таблицы
    Sessions: вытеснение/удаление строки настройку не трогает."""

    def test_override_survives_registry_eviction(self, ss, cfg):
        from backend_adapter import session_registry

        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        with mock.patch.object(cfg, "ADAPTER_SESSIONS_TABLE", 1):
            session_registry.register("sess1", "agent", model="m", backend="b", route="r")
            session_registry.register("sess2", "agent", model="m", backend="b", route="r")
        # sess1 вытеснена из таблицы Sessions — но её настройка цела.
        assert [r["session"] for r in session_registry.sessions_snapshot()] == ["sess2"]
        assert ss.override("sess1", "ADAPTER_DEBUG") is True


class TestModelOverride:
    """set_model_override/model_override (v0.9.6) — служебный ключ активной
    модели сессии, задаётся командой "/model <имя>". Хранится в том же
    _OVERRIDES, но вне SESSION_CONFIG_POOL/set_config."""

    def test_set_and_get(self, ss):
        assert ss.model_override("sess1") is None
        ss.set_model_override("sess1", "qwen3-coder")
        assert ss.model_override("sess1") == "qwen3-coder"

    def test_overwrite(self, ss):
        ss.set_model_override("sess1", "a")
        ss.set_model_override("sess1", "b")
        assert ss.model_override("sess1") == "b"

    def test_empty_session_noop(self, ss):
        ss.set_model_override("", "qwen3-coder")
        assert ss.model_override("") is None

    def test_model_override_survives_config_clear_of_pool_keys(self, ss):
        # Служебный ключ не входит в SESSION_CONFIG_POOL — set_config/clear
        # не должны его задеть.
        ss.set_model_override("sess1", "qwen3-coder")
        ss.set_config("sess1", clear=("ADAPTER_DEBUG",))
        assert ss.model_override("sess1") == "qwen3-coder"

    def test_model_override_removed_by_clear_session(self, ss):
        ss.set_model_override("sess1", "qwen3-coder")
        assert ss.clear_session("sess1") is True
        assert ss.model_override("sess1") is None

    def test_model_override_removed_by_reset(self, ss):
        ss.set_model_override("sess1", "qwen3-coder")
        ss.reset()
        assert ss.model_override("sess1") is None

    def test_set_config_ignores_model_override_key(self, ss):
        # Если кто-то передаст _model_override через set_config (мимо
        # set_model_override), он не попадёт в SESSION_CONFIG_POOL —
        # молча проигнорируется, как и любой ключ вне пула.
        ss.set_config("sess1", {"_model_override": "qwen3-coder"})
        assert ss.model_override("sess1") is None


class TestLogPartsCascade:
    """Каскад Log/Parts пер-сессионных переопределений (v0.9.6, задача 6).

    Parts — подробная запись поверх логов: сессия не может иметь Parts без
    Log. Направление каскада задаёт явное намерение Parts."""

    def test_parts_on_while_log_off_enables_log(self, ss, cfg):
        # Parts=on при Log=off → Log включается (переопределением сессии).
        cfg.ADAPTER_DEBUG = False
        ss.set_config("sess1", {"ADAPTER_DEBUG_PARTS": True})
        assert ss.effective("sess1", "ADAPTER_DEBUG") is True
        assert ss.effective("sess1", "ADAPTER_DEBUG_PARTS") is True

    def test_log_off_disables_parts(self, ss, cfg):
        # Log=off при Parts=on → Parts гасится (записью False).
        cfg.ADAPTER_DEBUG = True
        ss.set_config("sess1", {"ADAPTER_DEBUG_PARTS": True})
        ss.set_config("sess1", {"ADAPTER_DEBUG": False})
        assert ss.effective("sess1", "ADAPTER_DEBUG") is False
        assert ss.effective("sess1", "ADAPTER_DEBUG_PARTS") is False
        # Запись Parts зафиксирована явно (не «наследовать» общий Parts=on).
        assert ss.override("sess1", "ADAPTER_DEBUG_PARTS") is False

    def test_log_off_with_parts_inherited_disables(self, ss, cfg):
        # Общий Parts=on, сессия выключает Log — Parts гасится, т.к. иначе
        # унаследовал бы общий on и остался бы активен без Log.
        cfg.ADAPTER_DEBUG = True
        cfg.ADAPTER_DEBUG_PARTS = True
        ss.set_config("sess1", {"ADAPTER_DEBUG": False})
        assert ss.effective("sess1", "ADAPTER_DEBUG_PARTS") is False

    def test_parts_on_with_log_on_both_explicit_wins(self, ss, cfg):
        # В одном вызове Log=off и Parts=on — побеждает явное намерение Parts.
        cfg.ADAPTER_DEBUG = True
        ss.set_config("sess1", {"ADAPTER_DEBUG": False, "ADAPTER_DEBUG_PARTS": True})
        assert ss.effective("sess1", "ADAPTER_DEBUG") is True
        assert ss.effective("sess1", "ADAPTER_DEBUG_PARTS") is True

    def test_clearing_log_off_falls_back_to_config(self, ss, cfg):
        # Снятие переопределения Log возвращает общую настройку; Parts
        # остаётся согласован (общий Log=on).
        cfg.ADAPTER_DEBUG = True
        cfg.ADAPTER_DEBUG_PARTS = False
        ss.set_config("sess1", {"ADAPTER_DEBUG": False})
        ss.set_config("sess1", clear=("ADAPTER_DEBUG",))
        assert ss.effective("sess1", "ADAPTER_DEBUG") is True
        assert ss.effective("sess1", "ADAPTER_DEBUG_PARTS") is False


