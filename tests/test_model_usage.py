#!/usr/bin/env python3
"""Unit tests for backend_adapter.model_usage — used-models table.

Tests cover:
  - record: first call creates row + increments; repeats never reprobe
  - probe model selection: resolved name used for all 4 ENDPOINT_PROBES;
    collision prefix stripped; model not on backend → no probe; resolve/probe
    exceptions swallowed (record kept)
  - flag off: accounting kept, no probe
  - concurrency: two first calls to one model → one probe; distinct models
    both recorded
  - add_usage_bytes: accumulates traffic counters (bytes_sent/bytes_recv),
    flag off / zero / missing row → no-op
  - snapshot is a copy in insertion order; reset clears
  - persistence (YAML in the WEBUI root, on tmp_path): disabled ("") does no
    file I/O; auto path = LOGPATH or ./tmp/webui; record writes the file with
    {"version": 1, "models": ...} in insertion order; probing rows never hit
    disk; dirty file is normalized on load; loaded rows are never reprobed;
    usage_snapshot triggers the load; broken YAML → empty table without an
    exception and is overwritten by the next save; reset_model removes a row
    from memory and from the file; flush_table and the save interval gate
    periodic saves
"""
import os
import threading
import time
from unittest import mock

import pytest
import yaml


def _fresh():
    """Reload config + model_usage with clean state (conftest defaults:
    ADAPTER_MODEL_USAGE_ENABLE="0"). Returns (config, model_usage).

    Persistence is OFF by default ("" — no file I/O); persistence tests
    enable it themselves on tmp_path via set_persist_path."""
    import sys

    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]
    from backend_adapter import config, model_usage

    model_usage.set_persist_path("")
    return config, model_usage


def _persist(tmp_path, config, mu):
    """Включить персистентность на tmp_path (явный путь к YAML-файлу) и
    вернуть путь к файлу."""
    persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
    mu.set_persist_path(persist_file)
    return persist_file


class TestRecordBasic:
    def test_first_call_creates_row_and_increments(self):
        """record twice: row exists, calls==2, probe fired once."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(
                mu, "_probe_model_endpoints",
                return_value={"endpoints": {}, "errors": {}},
            ) as m_probe:
                mu.record_model_usage("m")
                mu.record_model_usage("m")
        rows = mu.usage_snapshot()
        assert len(rows) == 1
        assert rows[0]["model"] == "m"
        assert rows[0]["calls"] == 2
        assert rows[0]["backend"] == "AAA"
        assert m_probe.call_count == 1

    def test_repeat_never_reprobes(self):
        """5 calls → probe called exactly once."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(
                mu, "_probe_model_endpoints",
                return_value={"endpoints": {}, "errors": {}},
            ) as m_probe:
                for _ in range(5):
                    mu.record_model_usage("m")
        assert m_probe.call_count == 1
        assert mu.usage_snapshot()[0]["calls"] == 5

    def test_flag_off_records_without_probe(self):
        """ADAPTER_MODEL_USAGE_ENABLE=False → row kept, backend resolved, no probe."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")) as m_resolve:
            with mock.patch.object(mu, "_probe_model_endpoints") as m_probe:
                mu.record_model_usage("m")
                mu.record_model_usage("m")
        assert m_resolve.call_count == 1  # только при создании строки
        assert m_probe.call_count == 0
        rows = mu.usage_snapshot()
        assert len(rows) == 1
        assert rows[0]["calls"] == 2
        assert rows[0]["bytes_sent"] == 0
        assert rows[0]["bytes_recv"] == 0
        assert rows[0]["backend"] == "AAA"
        assert rows[0]["endpoints"] == {}
        assert rows[0]["probing"] is False


class TestAddUsageBytes:
    """add_usage_bytes: traffic counters accumulate on the existing row."""

    def test_accumulates_on_existing_row(self):
        """Two calls add up; counters live independently."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(
                mu, "_probe_model_endpoints",
                return_value={"endpoints": {}, "errors": {}},
            ):
                mu.record_model_usage("m")
        mu.add_usage_bytes("m", 100, 50)
        mu.add_usage_bytes("m", 20, 5)
        rows = mu.usage_snapshot()
        assert rows[0]["bytes_sent"] == 120
        assert rows[0]["bytes_recv"] == 55
        assert rows[0]["calls"] == 1  # трафик счётчик вызовов не трогает

    def test_zeros_are_noop(self):
        """sent=0/recv=0 → counters unchanged."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(
                mu, "_probe_model_endpoints",
                return_value={"endpoints": {}, "errors": {}},
            ):
                mu.record_model_usage("m")
        mu.add_usage_bytes("m", 0, 0)
        rows = mu.usage_snapshot()
        assert rows[0]["bytes_sent"] == 0
        assert rows[0]["bytes_recv"] == 0

    def test_flag_off_noop(self):
        """ADAPTER_MODEL_USAGE_ENABLE=False → nothing accumulates."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(
                mu, "_probe_model_endpoints",
                return_value={"endpoints": {}, "errors": {}},
            ):
                mu.record_model_usage("m")
        mu.add_usage_bytes("m", 100, 50)
        rows = mu.usage_snapshot()
        assert rows[0]["bytes_sent"] == 0
        assert rows[0]["bytes_recv"] == 0

    def test_missing_row_noop(self):
        """No row for the model → nothing created, nothing raised."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        mu.add_usage_bytes("ghost", 100, 50)  # must not raise
        assert mu.usage_snapshot() == []


class TestRecordProbeModel:
    def test_plain_model_probes_resolved(self):
        """probe receives resolved name + all 4 ENDPOINT_PROBES paths."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        captured = {}

        def recording_probe(backend, model):
            captured["backend"] = backend
            captured["model"] = model
            return {"endpoints": {}, "errors": {}}

        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(mu, "_probe_model_endpoints", side_effect=recording_probe):
                mu.record_model_usage("m")
        assert captured == {"backend": backend_cfg, "model": "m"}

    def test_collision_prefix_resolved_stripped(self):
        """client 'AAA.m' resolves to 'm' → probe uses stripped name 'm'."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        captured = {}

        def recording_probe(backend, model):
            captured["model"] = model
            return {"endpoints": {}, "errors": {}}

        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(mu, "_probe_model_endpoints", side_effect=recording_probe):
                mu.record_model_usage("AAA.m")
        assert captured["model"] == "m"

    def test_model_not_on_backend_no_probe(self):
        """resolved not among backend models → no probe, endpoints stay empty."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"other": ("AAA", backend_cfg)}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(mu, "_probe_model_endpoints") as m_probe:
                mu.record_model_usage("m")
        assert m_probe.call_count == 0
        rows = mu.usage_snapshot()
        assert rows[0]["endpoints"] == {}

    def test_resolve_raises_record_kept(self):
        """_resolve_backend raises RuntimeError → row kept, nothing escapes."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        with mock.patch.object(
            config, "_resolve_backend", side_effect=RuntimeError("no backend")
        ):
            mu.record_model_usage("m")  # must not raise
        rows = mu.usage_snapshot()
        assert len(rows) == 1
        assert rows[0]["model"] == "m"
        assert rows[0]["probing"] is False

    def test_probe_exception_swallowed(self):
        """probe raises → row kept, nothing escapes."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(
                mu, "_probe_model_endpoints", side_effect=RuntimeError("boom")
            ):
                mu.record_model_usage("m")  # must not raise
        rows = mu.usage_snapshot()
        assert rows[0]["endpoints"] == {}
        assert rows[0]["probing"] is False


class TestRecordConcurrency:
    def test_concurrent_first_calls_probe_once(self):
        """Two threads, same model: probe called once, calls==2."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
        barrier = threading.Barrier(2)

        def slow_probe(_backend, _model):
            barrier.wait(timeout=5)  # обе нити доходят до пробы
            time.sleep(0.05)
            return {"endpoints": {}, "errors": {}}

        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(mu, "_probe_model_endpoints", side_effect=slow_probe) as m_probe:
                threads = [
                    threading.Thread(target=mu.record_model_usage, args=("m",)),
                    threading.Thread(target=mu.record_model_usage, args=("m",)),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)
        assert m_probe.call_count == 1
        assert mu.usage_snapshot()[0]["calls"] == 2

    def test_concurrent_distinct_models_both_recorded(self):
        """Two threads, distinct models → both rows present."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        config._MODEL_TO_BACKEND = {"m1": ("AAA", backend_cfg), "m2": ("AAA", backend_cfg)}
        with mock.patch.object(
            config, "_resolve_backend", side_effect=lambda m: (backend_cfg, m)
        ):
            with mock.patch.object(
                mu, "_probe_model_endpoints",
                return_value={"endpoints": {}, "errors": {}},
            ):
                threads = [
                    threading.Thread(target=mu.record_model_usage, args=("m1",)),
                    threading.Thread(target=mu.record_model_usage, args=("m2",)),
                ]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(timeout=10)
        rows = {r["model"] for r in mu.usage_snapshot()}
        assert rows == {"m1", "m2"}


class TestSnapshot:
    def test_snapshot_is_copy_insertion_order(self):
        """Mutating the snapshot does not affect the table; order is insertion."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        mu.record_model_usage("m1")
        mu.record_model_usage("m2")
        snap = mu.usage_snapshot()
        assert [r["model"] for r in snap] == ["m1", "m2"]
        snap[0]["calls"] = 999
        snap[0]["endpoints"]["completions"] = {"status": 200, "found": True}
        assert mu.usage_snapshot()[0]["calls"] == 1
        assert mu.usage_snapshot()[0]["endpoints"] == {}

    def test_reset_clears(self):
        """reset_model_usage empties the table."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        mu.record_model_usage("m1")
        mu.reset_model_usage()
        assert mu.usage_snapshot() == []


# ---------------------------------------------------------------------------
# Persistence (YAML in the WEBUI root) — enabled per test on tmp_path
# ---------------------------------------------------------------------------

def _persist_setup(config, mu, tmp_path):
    """Настроить один бэкенд (AAA, модель m, проба-мок) и персистентность
    на tmp_path. Возвращает (persist_file)."""
    persist_file = _persist(tmp_path, config, mu)
    backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
    config._MODEL_TO_BACKEND = {"m": ("AAA", backend_cfg)}
    return persist_file


def _with_probe_mock(mu, fn):
    """Запустить fn с мокнутой _probe_model_endpoints (без сети)."""
    with mock.patch.object(
        mu, "_probe_model_endpoints",
        return_value={"endpoints": {}, "errors": {}},
    ):
        fn()


class TestPersistPath:
    def test_disabled_no_file_written(self, tmp_path):
        """set_persist_path("") → record never touches the filesystem."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        mu.record_model_usage("m")  # no file, no exception
        assert mu.usage_persist_file() is None
        root = tmp_path / "webui"
        assert not (root / "model-usage.yaml").exists()

    def test_auto_root_uses_logpath_or_default(self, tmp_path):
        """No explicit path → ADAPTER_DEBUG_LOGPATH or ./tmp/webui."""
        config, mu = _fresh()
        mu.set_persist_path(None)  # авто-режим (прод): _fresh оставил "" (тесты)
        assert mu.usage_persist_file() == os.path.join("./tmp/webui", mu.MODEL_USAGE_FILE)
        config.ADAPTER_DEBUG_LOGPATH = str(tmp_path / "logs")
        assert mu.usage_persist_file() == str(tmp_path / "logs" / mu.MODEL_USAGE_FILE)

    def test_explicit_path_wins(self, tmp_path):
        """Explicit persist path is returned as-is (abspath not required)."""
        config, mu = _fresh()
        _persist(tmp_path, config, mu)
        expected = str(tmp_path / mu.MODEL_USAGE_FILE)
        assert mu.usage_persist_file() == expected

    def test_set_persist_path_resets_loaded(self, tmp_path):
        """Changing the path re-reads the file on the next call — but only while
        the table is empty (post-start the table itself is the source of truth,
        the file is read once at first use)."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        p1 = str(tmp_path / "a.yaml")
        p2 = str(tmp_path / "b.yaml")
        mu.set_persist_path(p1)
        with open(p1, "w", encoding="utf-8") as f:
            yaml.safe_dump({"models": {"m1": {"model": "m1", "calls": 7}}}, f)
        mu.usage_snapshot()          # loads p1 (empty table)
        mu.reset_model_usage()       # «как после рестарта»: память пуста
        mu.set_persist_path(p2)      # reset → next load reads p2, not p1
        with open(p2, "w", encoding="utf-8") as f:
            yaml.safe_dump({"models": {"m2": {"model": "m2", "calls": 3}}}, f)
        rows = mu.usage_snapshot()
        assert [r["model"] for r in rows] == ["m2"]


class TestPersistSave:
    def test_record_writes_versioned_yaml(self, tmp_path):
        """record → file exists: {"version": 1, "models": ...}, probing False,
        insertion order preserved."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        mu.record_model_usage("m1")
        mu.record_model_usage("m2")
        mu.flush_table()   # инкрементные мутации — по периоду; flush форсирует
        with open(persist_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["version"] == 1
        assert list(data["models"].keys()) == ["m1", "m2"]
        assert data["models"]["m1"]["probing"] is False
        assert data["models"]["m1"]["calls"] == 1

    def test_probing_row_not_serialized(self, tmp_path):
        """A row with probing=True (first call, probe in flight) never hits disk.

        Through record_model_usage the flag is always cleared (finally) before
        the forced save, so the probe-in-flight state is simulated directly:
        a probing=True row in the table must not be serialized by a save."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        with mu._TABLE_LOCK:
            mu._TABLE["m"] = {
                "model": "m",
                "backend": "",
                "calls": 1,
                "bytes_sent": 0,
                "bytes_recv": 0,
                "endpoints": {},
                "errors": {},
                "first_seen": "12:00:00",
                "probing": True,
            }
            mu._DIRTY = True
        mu.flush_table()
        with open(persist_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "m" not in data["models"]

    def test_save_interval_gates_periodic_save(self, tmp_path):
        """_save_table(False): before the interval elapses — no write; after —
        writes."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        config.ADAPTER_MODEL_USAGE_SAVE_INTERVAL = 1000
        mu.record_model_usage("m")   # создание → force-save (_LAST_SAVE ≈ now)
        mu._TABLE["m"]["calls"] = 2  # грязная мутация напрямую
        mu._DIRTY = True
        mu._save_table(force=False)  # до истечения периода — no-op
        with open(persist_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["models"]["m"]["calls"] == 1
        config.ADAPTER_MODEL_USAGE_SAVE_INTERVAL = 0  # период истёк
        mu._save_table(force=False)
        with open(persist_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["models"]["m"]["calls"] == 2


class TestPersistLoad:
    def test_usage_snapshot_loads_existing_file(self, tmp_path):
        """usage_snapshot on an empty table reads the file (past sessions show
        up without new requests)."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        mu.record_model_usage("m")   # создание → файл записан
        mu.reset_model_usage()       # память пуста (как после рестарта)
        rows = mu.usage_snapshot()   # лениво читает файл
        assert len(rows) == 1
        assert rows[0]["model"] == "m"

    def test_dirty_file_normalized_on_load(self, tmp_path):
        """Bad values in the file are normalized; probing forced False; unknown
        pnames dropped."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        raw = {
            "version": 1,
            "models": {
                "m1": {
                    "model": "m1",
                    "backend": "AAA",
                    "calls": "abc",
                    "bytes_sent": -5,
                    "bytes_recv": 3,
                    "endpoints": {
                        "completions": {"status": 200, "found": True},
                        "unknown-pname": {"status": 200, "found": True},
                    },
                    "errors": {"completions": "boom"},
                    "first_seen": "not-a-time",
                    "probing": True,
                    "extra": "dropped",
                }
            },
        }
        with open(persist_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f)
        rows = mu.usage_snapshot()
        assert len(rows) == 1
        r = rows[0]
        assert r["calls"] == 0 and r["bytes_sent"] == 0 and r["bytes_recv"] == 3
        assert r["endpoints"] == {"completions": {"status": 200, "found": True}}
        assert r["errors"] == {"completions": "boom"}
        assert r["probing"] is False
        assert "extra" not in r
        assert r["first_seen"]  # непустая строка (сейчас — текущее время)

    def test_loaded_rows_not_reprobed(self, tmp_path):
        """Loaded rows take the fast path — probe never fires, calls grow."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        with open(persist_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(
                {"models": {"m1": {"model": "m1", "calls": 1, "backend": ""}}},
                f,
            )
        with mock.patch.object(mu, "_probe_model_endpoints") as m_probe:
            mu.record_model_usage("m1")
            mu.record_model_usage("m1")
        assert m_probe.call_count == 0
        assert mu.usage_snapshot()[0]["calls"] == 3

    def test_broken_yaml_ignored_then_overwritten(self, tmp_path):
        """Corrupt file → empty table, no exception; the next save overwrites it."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        with open(persist_file, "w", encoding="utf-8") as f:
            f.write("not: [valid\n yaml: *anchor")
        assert mu.usage_snapshot() == []     # битый файл не роняет
        mu.record_model_usage("m")           # сохранение перезаписывает
        with open(persist_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert "m" in data["models"]


class TestResetModel:
    def test_reset_removes_row_memory_and_file(self, tmp_path):
        """reset_model → row gone from table and file; second reset False."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        mu.record_model_usage("m")
        assert mu.reset_model("m") is True
        with open(persist_file, encoding="utf-8") as f:
            assert "m" not in yaml.safe_load(f)["models"]
        assert mu.reset_model("m") is False

    def test_reset_row_in_file_only(self, tmp_path):
        """Row in the file but not in memory (post-restart) is still resettable."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        with open(persist_file, "w", encoding="utf-8") as f:
            yaml.safe_dump({"models": {"m1": {"model": "m1", "calls": 5}}}, f)
        assert mu.reset_model("m1") is True
        with open(persist_file, encoding="utf-8") as f:
            assert "m1" not in yaml.safe_load(f)["models"]

    def test_flush_writes_dirty_table(self, tmp_path):
        """flush_table after mutations writes the file."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        mu.record_model_usage("m1")
        mu.flush_table()
        with open(persist_file, encoding="utf-8") as f:
            assert "m1" in yaml.safe_load(f)["models"]
