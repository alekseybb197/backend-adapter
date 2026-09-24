"""Tests for backend_adapter.session_settings — пер-сессионные переопределения
настроек (v0.9.5, снимок Log — v0.9.8, один флаг — v0.9.10).

Модуль — лист DAG: на верхнем уровне импортирует только ``config``. Хранит
таблицу ``session_id → {имя_настройки: значение}`` в памяти процесса.

**Две модели наследования (v0.9.8).** Log (``_SNAPSHOT_NAMES``) — СНИМОК:
``ensure_session`` копирует в сессию текущий общий тумблер, дальше сессия
живёт своим значением, а общий служит шаблоном для НОВЫХ сессий; состояния
``"inherit"`` у него нет, «сброс» = свежий снимок. TARGET-поля — ЖИВОЕ
наследование: два состояния (НЕ ЗАДАНА / конкретное значение); v0.9.9 —
«вернуться к общему» = снять запись (``clear``). v0.9.10: второй флаг
логирования (``ADAPTER_DEBUG_PARTS``) снят — части протокола собираются по
тому же единственному Log.

Поэтому ``set_config``/``ensure_session`` ВСЕГДА материализуют ключ Log:
после любого обращения к сессии её строка содержит этот ключ (в тестах
это отражено ожидаемым ``_snapshot()``), а «пустая строка» возможна только у
сессии, не прошедшей ``ensure_session``.

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


def _snapshot(cfg) -> dict:
    """Снимок общего Log на текущий момент — то, что ensure_session
    кладёт в строку сессии (дефолт fresh_env: выключен)."""
    return {
        "ADAPTER_DEBUG": bool(cfg.ADAPTER_DEBUG),
    }


class TestEnsureSession:
    """ensure_session() — образование сессии: снимок общего флага Log
    (v0.9.8, один флаг — v0.9.10)."""

    def test_seeds_snapshot(self, ss, cfg):
        cfg.ADAPTER_DEBUG = True
        assert ss.ensure_session("sess1") is True
        assert ss.session_overrides("sess1") == {
            "ADAPTER_DEBUG": True,
        }

    def test_idempotent(self, ss):
        assert ss.ensure_session("sess1") is True
        assert ss.ensure_session("sess1") is False

    def test_empty_session_id_noop(self, ss):
        assert ss.ensure_session("") is False
        assert ss.session_overrides("") == {}

    def test_does_not_overwrite_explicit_value(self, ss, cfg):
        # Сессия успела задать Log явно (страница /sessions) до первого
        # запроса — снимок дополняет, а не переписывает.
        cfg.ADAPTER_DEBUG = True
        ss.set_config("sess1", {"ADAPTER_DEBUG": False})
        assert ss.effective("sess1", "ADAPTER_DEBUG") is False

    def test_global_change_after_seed_does_not_affect_session(self, ss, cfg):
        # Ключевое свойство снимка: глобальные тумблеры — шаблон для НОВЫХ
        # сессий, уже образованную сессию их смена не трогает.
        cfg.ADAPTER_DEBUG = False
        ss.ensure_session("sess1")
        cfg.ADAPTER_DEBUG = True
        assert ss.effective("sess1", "ADAPTER_DEBUG") is False
        # ...а новая сессия получает уже новое значение шаблона.
        assert ss.ensure_session("sess2") is True
        assert ss.effective("sess2", "ADAPTER_DEBUG") is True

    def test_snapshot_has_only_log_key(self, ss, cfg):
        # v0.9.10: снимок состоит из ОДНОГО ключа — второй флаг снят;
        # ad-hoc «равновесие» пары больше не нужно.
        cfg.ADAPTER_DEBUG = True
        ss.ensure_session("sess1")
        assert ss.session_overrides("sess1") == {"ADAPTER_DEBUG": True}
        assert ss.override("sess1", "ADAPTER_DEBUG_PARTS") is None


class TestOverride:
    """override() — сырое хранимое значение."""

    def test_unset_returns_none(self, ss):
        assert ss.override("sess1", "ADAPTER_DEBUG") is None

    def test_empty_session_id_returns_none(self, ss):
        assert ss.override("", "ADAPTER_DEBUG") is None

    def test_returns_stored_value(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        assert ss.override("sess1", "ADAPTER_DEBUG") is True

    def test_returns_none_after_clear(self, ss):
        # v0.9.9: у TARGET «вернуться к общему» — запись СНИМАЕТСЯ, override
        # отдаёт None (трактовку «взять общее» делает вызывающий).
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        ss.set_config("sess1", clear=("ADAPTER_MESSAGES_TARGET",))
        assert ss.override("sess1", "ADAPTER_MESSAGES_TARGET") is None

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

    def test_unset_uses_config(self, ss, cfg):
        # Переопределения нет — действует общая настройка (живое наследование).
        cfg.ADAPTER_MESSAGES_TARGET = "passthrough"
        assert ss.effective("sess1", "ADAPTER_MESSAGES_TARGET") == "passthrough"

    def test_reads_config_live(self, ss, cfg):
        # Общая настройка меняется ПОСЛЕ образования сессии — сессия без
        # переопределения видит новое значение (живое чтение атрибута config).
        cfg.ADAPTER_MESSAGES_TARGET = "passthrough"
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

    def test_applies_valid_bool(self, ss, cfg):
        result = ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        # Строка сессии материализована целиком: снимок Log + правка.
        assert result == {**_snapshot(cfg), "ADAPTER_DEBUG": True}
        assert ss.override("sess1", "ADAPTER_DEBUG") is True

    def test_applies_valid_enum(self, ss):
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        assert ss.override("sess1", "ADAPTER_MESSAGES_TARGET") == "passthrough"

    def test_inherit_not_in_target_domain(self, ss, cfg):
        # v0.9.9: "inherit" — не значение домена TARGET, запись игнорируется
        # (в таблице остаётся только снимок Log).
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "inherit"})
        assert ss.override("sess1", "ADAPTER_MESSAGES_TARGET") is None
        assert "ADAPTER_MESSAGES_TARGET" not in ss.session_overrides("sess1")

    def test_name_outside_pool_ignored(self, ss, cfg):
        # Имя вне config.SESSION_CONFIG_POOL молча игнорируется (снимок
        # Log при этом материализуется — сессия образована).
        result = ss.set_config("sess1", {"ADAPTER_DEBUG_TRIM": 500})
        assert result == _snapshot(cfg)
        assert "ADAPTER_DEBUG_TRIM" not in ss.session_overrides("sess1")

    def test_wrong_type_ignored(self, ss, cfg):
        # Строка в bool-настройку не проходит accepts_value → игнор.
        result = ss.set_config("sess1", {"ADAPTER_DEBUG": "yes"})
        assert result == _snapshot(cfg)

    def test_wrong_enum_value_ignored(self, ss, cfg):
        # Значение вне домена TARGET молча игнорируется.
        result = ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "bogus"})
        assert result == _snapshot(cfg)

    def test_bool_not_accepted_for_enum(self, ss, cfg):
        result = ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": True})
        assert result == _snapshot(cfg)

    def test_mixed_valid_and_invalid(self, ss, cfg):
        # Невалидное имя/значение игнорируются, валидное — применяется.
        result = ss.set_config(
            "sess1",
            {"ADAPTER_DEBUG": True, "NOT_A_SETTING": 1, "ADAPTER_DEBUG_TRIM": "no"},
        )
        assert result == {**_snapshot(cfg), "ADAPTER_DEBUG": True}

    def test_clear_log_returns_fresh_snapshot(self, ss, cfg):
        # У снимка Log «снять» нельзя: clear переинициализирует из
        # текущего общего тумблера (сессия как новая), а не удаляет запись.
        cfg.ADAPTER_DEBUG = False
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        ss.set_config("sess1", clear=("ADAPTER_DEBUG",))
        assert ss.override("sess1", "ADAPTER_DEBUG") is False
        assert ss.effective("sess1", "ADAPTER_DEBUG") is False
        # А смена общего тумблера ПОСЛЕ этого сессию снова не трогает.
        cfg.ADAPTER_DEBUG = True
        assert ss.effective("sess1", "ADAPTER_DEBUG") is False

    def test_clear_target_removes_override(self, ss, cfg):
        # У TARGET-поля модель наследования — живая: clear удаляет запись,
        # и сессия снова видит общую настройку (в т.ч. её будущие смены).
        cfg.ADAPTER_MESSAGES_TARGET = "completions"
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        ss.set_config("sess1", clear=("ADAPTER_MESSAGES_TARGET",))
        assert ss.override("sess1", "ADAPTER_MESSAGES_TARGET") is None
        assert ss.effective("sess1", "ADAPTER_MESSAGES_TARGET") == "completions"
        cfg.ADAPTER_MESSAGES_TARGET = "none"
        assert ss.effective("sess1", "ADAPTER_MESSAGES_TARGET") == "none"

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

    def test_row_never_empty_after_seed(self, ss, cfg):
        # Сессия, прошедшая set_config, образована — снимок Log в её
        # строке остаётся, поэтому «пустой строки» не бывает (v0.9.8).
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "none"})
        result = ss.set_config("sess1", clear=("ADAPTER_MESSAGES_TARGET",))
        assert result == _snapshot(cfg)
        assert ss.session_overrides("sess1") == _snapshot(cfg)


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

    def test_multiple_settings(self, ss, cfg):
        ss.set_config(
            "sess1",
            {"ADAPTER_DEBUG": True, "ADAPTER_MESSAGES_TARGET": "passthrough"},
        )
        assert ss.session_overrides("sess1") == {
            **_snapshot(cfg),
            "ADAPTER_DEBUG": True,
            "ADAPTER_MESSAGES_TARGET": "passthrough",
        }


class TestClearSession:
    def test_clears_all_and_returns_true(self, ss, cfg):
        # У образованной сессии снимок Log не удаляется — он
        # переинициализируется текущим общим тумблером; всё остальное
        # (TARGET, служебный ключ модели) снимается.
        ss.set_config("sess1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        assert ss.clear_session("sess1") is True
        assert ss.session_overrides("sess1") == _snapshot(cfg)

    def test_false_when_nothing_to_clear(self, ss):
        assert ss.clear_session("sess1") is False

    def test_empty_session_id_false(self, ss):
        assert ss.clear_session("") is False

    def test_does_not_touch_other_sessions(self, ss, cfg):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        ss.set_config("sess2", {"ADAPTER_DEBUG": True})
        ss.clear_session("sess1")
        assert ss.session_overrides("sess1") == _snapshot(cfg)
        assert ss.session_overrides("sess2") == {**_snapshot(cfg), "ADAPTER_DEBUG": True}


class TestReset:
    def test_wipes_table(self, ss):
        ss.set_config("sess1", {"ADAPTER_DEBUG": True})
        ss.set_config("sess2", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
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


class TestLogSingleFlag:
    """Один флаг логирования (v0.9.10): каскад Log/Parts снят вместе с
    ADAPTER_DEBUG_PARTS — у сессии остаётся единственный Log, а снятый ключ
    ведёт себя как любой ключ вне пула (молча игнорируется)."""

    def test_parts_key_ignored(self, ss, cfg):
        cfg.ADAPTER_DEBUG = False
        result = ss.set_config("sess1", {"ADAPTER_DEBUG_PARTS": True})
        # Снятый ключ не в пуле → игнор; Log не включается (каскада нет).
        assert result == _snapshot(cfg)
        assert ss.effective("sess1", "ADAPTER_DEBUG") is False
        assert ss.override("sess1", "ADAPTER_DEBUG_PARTS") is None

    def test_log_toggle_alone(self, ss, cfg):
        # Log переключается одиночно — никого за собой не тянет.
        cfg.ADAPTER_DEBUG = True
        ss.set_config("sess1", {"ADAPTER_DEBUG": False})
        assert ss.effective("sess1", "ADAPTER_DEBUG") is False
        ss.set_config("sess1", clear=("ADAPTER_DEBUG",))
        assert ss.effective("sess1", "ADAPTER_DEBUG") is True


