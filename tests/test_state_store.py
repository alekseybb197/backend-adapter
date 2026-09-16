#!/usr/bin/env python3
"""Unit tests for backend_adapter.state_store — перманентное состояние
runtime-пула в ``state.yaml`` (v0.9.6).

Проверяем жизненный цикл файла: нет файла → создаётся из env; файл есть →
применяется ПОВЕРХ env; битый/невалидный → [WARN] и работа на env;
фильтр ключей пула; атомарность (os.replace); колбек записи при изменении
через ``config.set_runtime_config``.

Модуль берётся фикстурой (не импортом на уровне файла): autouse isolate_logs
(→ fresh_env) пересоздаёт backend_adapter-модули перед каждым тестом —
верхнеуровневая ссылка указывала бы на устаревший экземпляр (тот же паттерн,
что tests/test_session_registry.py).

Путь файла задаётся через tmp_path: ``config.ADAPTER_DATA_ROOT`` —
корень (state_store.state_path берёт из него ``var/``), ``config.ADAPTER_STATE``
— имя (state_store.state_path читает их живьём).
"""

import os

import pytest
import yaml


@pytest.fixture
def store():
    """Свежий (после fresh_env) экземпляр state_store."""
    from backend_adapter import state_store

    return state_store


@pytest.fixture
def cfg():
    """Свежий экземпляр config (живая связь с пулом)."""
    from backend_adapter import config

    return config


def _point_at(cfg, tmp_path, name="state.yaml"):
    """Направить state_store в tmp_path (DATA_ROOT=dir, STATE=имя файла).

    Папка состояния ``var/`` создаётся сразу — тесты пишут в файл напрямую
    (в проде её создаёт старт backend-adapter.py)."""
    cfg.ADAPTER_DATA_ROOT = str(tmp_path)
    cfg.ADAPTER_STATE = name
    var_dir = os.path.join(str(tmp_path), "var")
    os.makedirs(var_dir, exist_ok=True)
    return os.path.join(var_dir, name)


class TestStatePath:
    def test_joins_var_dir_and_name(self, store, cfg, tmp_path):
        cfg.ADAPTER_DATA_ROOT = str(tmp_path)
        cfg.ADAPTER_STATE = "custom.yaml"
        assert store.state_path() == os.path.join(str(tmp_path), "var", "custom.yaml")

    def test_reads_config_live(self, store, cfg, tmp_path):
        cfg.ADAPTER_DATA_ROOT = str(tmp_path / "a")
        cfg.ADAPTER_STATE = "s.yaml"
        first = store.state_path()
        cfg.ADAPTER_DATA_ROOT = str(tmp_path / "b")
        assert store.state_path() != first


class TestLoad:
    def test_missing_file_empty(self, store, cfg, tmp_path):
        _point_at(cfg, tmp_path)
        assert store.load() == {}

    def test_reads_valid_file(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"ADAPTER_DEBUG": True, "ADAPTER_DEBUG_TRIM": 500}, f)
        assert store.load() == {"ADAPTER_DEBUG": True, "ADAPTER_DEBUG_TRIM": 500}

    def test_unknown_key_skipped(self, store, cfg, tmp_path, capsys):
        path = _point_at(cfg, tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"NOT_A_SETTING": 1, "ADAPTER_DEBUG": True}, f)
        assert store.load() == {"ADAPTER_DEBUG": True}
        assert "NOT_A_SETTING" in capsys.readouterr().out

    def test_wrong_type_skipped(self, store, cfg, tmp_path, capsys):
        # Строка в bool-настройку — невалидно, пропускается.
        path = _point_at(cfg, tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"ADAPTER_DEBUG": "yes"}, f)
        assert store.load() == {}
        assert "ADAPTER_DEBUG" in capsys.readouterr().out

    def test_broken_yaml_warns(self, store, cfg, tmp_path, capsys):
        path = _point_at(cfg, tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("ADAPTER_DEBUG: [unclosed\n")
        assert store.load() == {}
        assert "[WARN]" in capsys.readouterr().out

    def test_non_dict_root_warns(self, store, cfg, tmp_path, capsys):
        path = _point_at(cfg, tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump(["not", "a", "dict"], f)
        assert store.load() == {}
        assert "[WARN]" in capsys.readouterr().out

    def test_empty_file_empty(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("")
        assert store.load() == {}


class TestSave:
    def test_writes_only_pool_keys(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        store.save({"ADAPTER_DEBUG": True, "NOT_A_SETTING": 42})
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data == {"ADAPTER_DEBUG": True}

    def test_creates_missing_directory(self, store, cfg, tmp_path):
        cfg.ADAPTER_DATA_ROOT = str(tmp_path / "deep" / "data")
        cfg.ADAPTER_STATE = "state.yaml"
        store.save({"ADAPTER_DEBUG": True})
        assert os.path.isfile(os.path.join(cfg.var_dir(), "state.yaml"))

    def test_atomic_no_tmp_left(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        store.save({"ADAPTER_DEBUG": True})
        assert os.path.isfile(path)
        assert not os.path.exists(f"{path}.tmp")

    def test_same_payload_skips_rewrite(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        store.save({"ADAPTER_DEBUG": True})
        # Помечаем файл «чужим» содержимым: если повторный save с тем же
        # payload реально пишет диск, метка исчезнет. Так проверяем пропуск
        # надёжнее, чем по mtime (мог бы совпасть при быстрой перезаписи).
        with open(path, "w", encoding="utf-8") as f:
            f.write("sentinel: true\n")
        store.save({"ADAPTER_DEBUG": True})
        with open(path, encoding="utf-8") as f:
            assert f.read() == "sentinel: true\n"

    def test_different_payload_rewrites(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        store.save({"ADAPTER_DEBUG": True})
        store.save({"ADAPTER_DEBUG": False})
        with open(path, encoding="utf-8") as f:
            assert yaml.safe_load(f) == {"ADAPTER_DEBUG": False}

    def test_oserror_does_not_raise(self, store, cfg, tmp_path, capsys):
        # Путь-родитель — файл, а не директория: makedirs упадёт → [WARN],
        # исключение наружу не выходит (канал наблюдательный).
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        cfg.ADAPTER_DATA_ROOT = str(blocker)
        cfg.ADAPTER_STATE = "state.yaml"
        store.save({"ADAPTER_DEBUG": True})
        assert "[WARN]" in capsys.readouterr().out


class TestApplyOnStartup:
    def test_no_file_creates_from_env(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        cfg.ADAPTER_DEBUG = True
        cfg.ADAPTER_DEBUG_TRIM = 123
        store.apply_on_startup()
        assert os.path.isfile(path)
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        # В файл попал весь пул (снимок env-значений).
        assert data["ADAPTER_DEBUG"] is True
        assert data["ADAPTER_DEBUG_TRIM"] == 123
        assert set(data) == set(cfg.RUNTIME_CONFIG_POOL)

    def test_file_overrides_env(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        cfg.ADAPTER_DEBUG = False
        cfg.ADAPTER_DEBUG_TRIM = 3000
        with open(path, "w", encoding="utf-8") as f:
            yaml.safe_dump({"ADAPTER_DEBUG": True, "ADAPTER_DEBUG_TRIM": 777}, f)
        store.apply_on_startup()
        # Файл победил env.
        assert cfg.ADAPTER_DEBUG is True
        assert cfg.ADAPTER_DEBUG_TRIM == 777

    def test_broken_file_keeps_env(self, store, cfg, tmp_path, capsys):
        path = _point_at(cfg, tmp_path)
        cfg.ADAPTER_DEBUG = True
        cfg.ADAPTER_DEBUG_TRIM = 3000
        with open(path, "w", encoding="utf-8") as f:
            f.write("ADAPTER_DEBUG: [unclosed\n")
        store.apply_on_startup()
        assert cfg.ADAPTER_DEBUG is True
        assert cfg.ADAPTER_DEBUG_TRIM == 3000
        assert "[WARN]" in capsys.readouterr().out

    def test_registers_change_callback(self, store, cfg, tmp_path):
        _point_at(cfg, tmp_path)
        store.apply_on_startup()
        assert store._on_config_change in cfg._ON_CHANGE

    def test_change_persisted_via_callback(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        store.apply_on_startup()
        # Изменение пула через config → колбек → файл.
        cfg.set_runtime_config(ADAPTER_DEBUG_TRIM=999)
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["ADAPTER_DEBUG_TRIM"] == 999

    def test_callback_ignores_unknown_only_change(self, store, cfg, tmp_path):
        path = _point_at(cfg, tmp_path)
        store.apply_on_startup()
        with open(path, encoding="utf-8") as f:
            before = yaml.safe_load(f)
        # Ключ вне пула не применяется → changed=False → записи нет.
        cfg.set_runtime_config(NOT_A_SETTING=1)
        with open(path, encoding="utf-8") as f:
            assert yaml.safe_load(f) == before
