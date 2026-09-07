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
  - add_usage_tokens: accumulates input/output usage tokens (input_tokens /
    output_tokens), flag off / zero / missing row → no-op
  - snapshot is a copy in insertion order; reset clears
  - persistence (YAML in the WEBUI root, on tmp_path): disabled ("") does no
    file I/O; auto path = LOGPATH or ./tmp/webui; record writes the file with
    {"version": 2, "models": ...} in insertion order; probing rows never hit
    disk; dirty file is normalized on load; v1 files (byte counters) migrate —
    calls/endpoints/errors/first_seen survive, tokens start at 0; unknown
    version (> 2) ignored; loaded rows are never reprobed; usage_snapshot
    triggers the load; broken YAML → empty table without an exception and is
    overwritten by the next save; reset_model zeroes the row's counters
    (calls/input_tokens/output_tokens → 0) and keeps the row in memory and in
    the file; flush_table and the save interval gate periodic saves
  - reprobe («Перепроверить»): a background re-probe of a row's endpoints
    updates only endpoints/errors (calls/tokens untouched, file saved on
    persist); missing row / first probe in flight (probing) → False;
    start_reprobe spawns the daemon worker and rejects a second run while one
    is in flight; reprobe_state mirrors running/model
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
        assert rows[0]["input_tokens"] == 0
        assert rows[0]["output_tokens"] == 0
        assert rows[0]["backend"] == "AAA"
        assert rows[0]["endpoints"] == {}
        assert rows[0]["probing"] is False


class TestAddUsageTokens:
    """add_usage_tokens: input/output usage-token counters accumulate on the
    existing row; zero in both → no-op; gate is on the SUM (input=0 with
    output>0 must still count)."""

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
        mu.add_usage_tokens("m", 100, 50)
        mu.add_usage_tokens("m", 20, 5)
        rows = mu.usage_snapshot()
        assert rows[0]["input_tokens"] == 120
        assert rows[0]["output_tokens"] == 55
        assert rows[0]["calls"] == 1  # токены счётчик вызовов не трогают

    def test_input_zero_output_positive_counts(self):
        """input=0 with output>0 passes the gate (usage with output only)."""
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
        mu.add_usage_tokens("m", 0, 7)
        rows = mu.usage_snapshot()
        assert rows[0]["input_tokens"] == 0
        assert rows[0]["output_tokens"] == 7

    def test_zeros_are_noop(self):
        """input=0/output=0 → counters unchanged (no usage in the answer)."""
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
        mu.add_usage_tokens("m", 0, 0)
        rows = mu.usage_snapshot()
        assert rows[0]["input_tokens"] == 0
        assert rows[0]["output_tokens"] == 0

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
        mu.add_usage_tokens("m", 100, 50)
        rows = mu.usage_snapshot()
        assert rows[0]["input_tokens"] == 0
        assert rows[0]["output_tokens"] == 0

    def test_missing_row_noop(self):
        """No row for the model → nothing created, nothing raised."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        mu.add_usage_tokens("ghost", 100, 50)  # must not raise
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


class TestEndpointSync:
    """Синхронизация found-эндпоинтов в config._ENDPOINT_STATE (v0.8.5,
    задача 2): найденные пробой модели эндпоинты переносятся в кэш бэкенда
    (колонка «Доступные API» и экспортёр видят их), только found=True и
    только для бэкендов из числа настроенных (config._BACKEND_BY_NAME)."""

    def _config(self):
        _reload_config()
        from backend_adapter import config
        return config

    def _backend(self, name="AAA"):
        return {"name": name, "base": f"http://{name.lower()}.local", "key": "k"}

    def _probe_result(self):
        """Результат _probe_model_endpoints: два found + один не-found."""
        return {
            "endpoints": {
                "completions": {"status": 200, "found": True},
                "responses": {"status": 200, "found": True},
                "embeddings": {"status": 500, "found": False},
            },
            "errors": {"embeddings": "HTTP 500"},
        }

    def test_record_syncs_found_endpoints_to_backend_state(self):
        """Первая проба модели: found-эндпоинты видны в _ENDPOINT_STATE
        бэкенда; не-found — не переносятся (синхронизируются ТОЛЬКО
        находки, found=True ⇔ HTTP 200)."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = self._backend()
        config._MODEL_TO_BACKEND = {"m": (backend_cfg["name"], backend_cfg)}
        config._BACKENDS = [backend_cfg]
        config._BACKEND_BY_NAME = {backend_cfg["name"]: backend_cfg}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(mu, "_probe_model_endpoints",
                                   return_value=self._probe_result()):
                mu.record_model_usage("m")
        entry = config._ENDPOINT_STATE.get("AAA")
        assert entry is not None
        found = {p: v["found"] for p, v in entry["endpoints"].items()}
        assert found == {
            "/v1/chat/completions": True,
            "/v1/responses": True,
        }
        # embeddings (found=False) в кэш бэкенда не синхронизирован.
        assert "/v1/embeddings" not in entry["endpoints"]
        assert entry["at"] > 0

    def test_record_orphaned_backend_not_synced(self):
        """Бэкенд вне _BACKEND_BY_NAME (удалён из YAML — строка-история):
        синхронизации в кэш НЕТ."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = self._backend("GONE")
        config._MODEL_TO_BACKEND = {"m": (backend_cfg["name"], backend_cfg)}
        # В _BACKEND_BY_NAME бэкенда нет — как после удаления из YAML.
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(mu, "_probe_model_endpoints",
                                   return_value=self._probe_result()):
                mu.record_model_usage("m")
        assert "GONE" not in config._ENDPOINT_STATE
        # Строка usage-таблицы осталась как история.
        rows = mu.usage_snapshot()
        assert rows[0]["model"] == "m"
        assert rows[0]["backend"] == "GONE"

    def test_record_probe_returns_empty_no_sync(self):
        """Проба без эндпоинтов (политика — ничего не перечислено) →
        синхронизировать нечего, запись бэкенда не создаётся."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = True
        backend_cfg = self._backend()
        config._MODEL_TO_BACKEND = {"m": (backend_cfg["name"], backend_cfg)}
        config._BACKENDS = [backend_cfg]
        config._BACKEND_BY_NAME = {backend_cfg["name"]: backend_cfg}
        with mock.patch.object(config, "_resolve_backend", return_value=(backend_cfg, "m")):
            with mock.patch.object(mu, "_probe_model_endpoints",
                                   return_value={"endpoints": {}, "errors": {}}):
                mu.record_model_usage("m")
        assert config._ENDPOINT_STATE == {}

    def test_reprobe_syncs_found_endpoints(self):
        """«Перепроверить» строки (reprobe_model): перепробованные found —
        в кэш бэкенда."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        config._BACKENDS = [self._backend()]
        config._BACKEND_BY_NAME = {"AAA": self._backend()}
        mu.reset_model_usage()
        mu._TABLE["m"] = {
            "model": "m", "backend": "AAA", "calls": 5,
            "input_tokens": 0, "output_tokens": 0,
            "endpoints": {}, "errors": {},
            "first_seen": "10:00:00", "probing": False,
        }
        with mock.patch.object(config, "_resolve_backend",
                               return_value=({"name": "AAA"}, "m")):
            with mock.patch.object(mu, "_backend_has_model", return_value=True):
                with mock.patch.object(mu, "_probe_model_endpoints",
                                       return_value=self._probe_result()):
                    assert mu.reprobe_model("m") is True
        entry = config._ENDPOINT_STATE.get("AAA")
        assert entry is not None
        assert entry["endpoints"]["/v1/chat/completions"] == {"status": 200, "found": True}
        # Не-found эндпоинты (embeddings, status 500) в кэш не синхронизируются.
        assert "/v1/embeddings" not in entry["endpoints"]

    def test_load_syncs_found_endpoints_from_file(self, tmp_path):
        """Загрузка model-usage.yaml: found-эндпоинты загруженных строк для
        настроенных бэкендов переносятся в кэш; строки НЕ перепробуются."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        backend_cfg = self._backend()
        config._MODEL_TO_BACKEND = {"m": (backend_cfg["name"], backend_cfg)}
        config._BACKENDS = [backend_cfg]
        config._BACKEND_BY_NAME = {backend_cfg["name"]: backend_cfg}
        with open(persist_file, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "version": 2,
                "models": {
                    "m": {
                        "model": "m", "backend": "AAA", "calls": 3,
                        "input_tokens": 0, "output_tokens": 0,
                        "endpoints": {
                            "completions": {"status": 200, "found": True},
                            "embeddings": {"status": 500, "found": False},
                        },
                        "errors": {}, "first_seen": "10:00:00", "probing": False,
                    }
                },
            }, f)
        with mock.patch.object(mu, "_probe_model_endpoints") as m_probe:
            rows = mu.usage_snapshot()  # лениво читает файл
        assert len(rows) == 1
        m_probe.assert_not_called()  # контракт: загруженные строки не перепробуются
        entry = config._ENDPOINT_STATE.get("AAA")
        assert entry is not None
        assert entry["endpoints"]["/v1/chat/completions"] == {"status": 200, "found": True}

    def test_load_orphaned_backend_not_synced(self, tmp_path):
        """Загруженная строка бэкенда, которого нет в настройках, кэш НЕ
        пополняет (строка остаётся историей в usage-таблице)."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        with open(persist_file, "w", encoding="utf-8") as f:
            yaml.safe_dump({
                "version": 2,
                "models": {
                    "m": {
                        "model": "m", "backend": "GONE", "calls": 1,
                        "endpoints": {
                            "completions": {"status": 200, "found": True},
                        },
                        "errors": {}, "first_seen": "10:00:00", "probing": False,
                    }
                },
            }, f)
        rows = mu.usage_snapshot()
        assert rows[0]["backend"] == "GONE"
        assert config._ENDPOINT_STATE == {}

    def test_sync_helper_calls_upsert_for_found_only(self):
        """_sync_found_endpoints: upsert зовётся только для found-эндпоинтов,
        с (name, pname, status, True); осиротевший бэкенд — ни одного вызова."""
        config, mu = _fresh()
        backend_cfg = self._backend()
        config._BACKEND_BY_NAME = {backend_cfg["name"]: backend_cfg}
        source = {
            "endpoints": {
                "completions": {"status": 200, "found": True},
                "embeddings": {"status": 500, "found": False},
            }
        }
        with mock.patch.object(config, "upsert_endpoint_state") as m_upsert:
            mu._sync_found_endpoints("AAA", source)
        assert m_upsert.call_args_list == [
            mock.call("AAA", "completions", 200, True),
        ]
        m_upsert.reset_mock()
        with mock.patch.object(config, "upsert_endpoint_state") as m_upsert2:
            mu._sync_found_endpoints("GONE", source)
        m_upsert2.assert_not_called()

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


class TestProbeModelEndpointsPolicy:
    """Прямые тесты model_usage._probe_model_endpoints под политику «только
    явно указанные пробы» (v0.8.5): эндпоинт пробуется ТОЛЬКО если он
    перечислен в probe-ключе бэкенда с непустой моделью; пробуем resolved
    (моделью запроса), а не probe-модель."""

    def test_only_listed_endpoints_probed(self):
        config, mu = _fresh()
        backend_cfg = {
            "name": "AAA", "base": "http://aaa.local", "key": "k",
            "probe": {"completions": "some-model", "responses": "another"},
        }
        captured = {}

        def recording_probe(backend, probes, timeout=None):
            captured["probes"] = probes
            return {"endpoints": {}, "errors": {}}

        with mock.patch.object(config, "_probe_backend_endpoints",
                               side_effect=recording_probe):
            result = mu._probe_model_endpoints(backend_cfg, "resolved-m")
        # Пробуются только пути, перечисленные в probe; модели — resolved
        assert captured["probes"] == {
            "/v1/chat/completions": "resolved-m",
            "/v1/responses": "resolved-m",
        }
        assert result == {"endpoints": {}, "errors": {}}

    def test_no_probe_key_returns_empty(self):
        """Ключа probe нет вовсе → проб нет: пустой результат, сеть не ходила."""
        config, mu = _fresh()
        backend_cfg = {"name": "AAA", "base": "http://aaa.local", "key": "k"}
        with mock.patch.object(config, "_probe_backend_endpoints") as m_probe:
            result = mu._probe_model_endpoints(backend_cfg, "m")
        m_probe.assert_not_called()
        assert result == {"endpoints": {}, "errors": {}}

    def test_empty_probe_values_not_probed(self):
        """Пустые значения (messages:) — не пробуются (только непустые)."""
        config, mu = _fresh()
        backend_cfg = {
            "name": "AAA", "base": "http://aaa.local", "key": "k",
            "probe": {"completions": "m", "messages": ""},
        }
        captured = {}

        def recording_probe(backend, probes, timeout=None):
            captured["probes"] = probes
            return {"endpoints": {}, "errors": {}}

        with mock.patch.object(config, "_probe_backend_endpoints",
                               side_effect=recording_probe):
            mu._probe_model_endpoints(backend_cfg, "m")
        assert set(captured["probes"]) == {"/v1/chat/completions"}


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
        """record → file exists: {"version": 2, "models": ...}, probing False,
        insertion order preserved, token fields present."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        mu.record_model_usage("m1")
        mu.record_model_usage("m2")
        mu.flush_table()   # инкрементные мутации — по периоду; flush форсирует
        with open(persist_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["version"] == 2
        assert list(data["models"].keys()) == ["m1", "m2"]
        assert data["models"]["m1"]["probing"] is False
        assert data["models"]["m1"]["calls"] == 1
        assert data["models"]["m1"]["input_tokens"] == 0
        assert data["models"]["m1"]["output_tokens"] == 0
        assert "bytes_sent" not in data["models"]["m1"]
        assert "bytes_recv" not in data["models"]["m1"]

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
                "input_tokens": 0,
                "output_tokens": 0,
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
            "version": 2,
            "models": {
                "m1": {
                    "model": "m1",
                    "backend": "AAA",
                    "calls": "abc",
                    "input_tokens": -5,
                    "output_tokens": 3,
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
        assert r["calls"] == 0 and r["input_tokens"] == 0 and r["output_tokens"] == 3
        assert r["endpoints"] == {"completions": {"status": 200, "found": True}}
        assert r["errors"] == {"completions": "boom"}
        assert r["probing"] is False
        assert "extra" not in r
        assert r["first_seen"]  # непустая строка (сейчас — текущее время)

    def test_v1_file_migrates_tokens_start_at_zero(self, tmp_path):
        """version: 1 (byte counters) loads: calls/endpoints/errors/first_seen
        survive, bytes dropped, tokens start at 0."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        raw = {
            "version": 1,
            "models": {
                "m1": {
                    "model": "m1",
                    "backend": "AAA",
                    "calls": 42,
                    "bytes_sent": 1523400,
                    "bytes_recv": 8123456,
                    "endpoints": {"completions": {"status": 200, "found": True}},
                    "errors": {},
                    "first_seen": "14:32:05",
                    "probing": False,
                }
            },
        }
        with open(persist_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f)
        rows = mu.usage_snapshot()
        assert len(rows) == 1
        r = rows[0]
        assert r["model"] == "m1"
        assert r["backend"] == "AAA"
        assert r["calls"] == 42
        assert r["input_tokens"] == 0
        assert r["output_tokens"] == 0
        assert "bytes_sent" not in r
        assert "bytes_recv" not in r
        assert r["endpoints"] == {"completions": {"status": 200, "found": True}}
        assert r["errors"] == {}
        assert r["first_seen"] == "14:32:05"
        # Следующее сохранение переписывает файл в v2 без байтовых полей.
        mu.record_model_usage("m1")   # fast-path: строка из файла
        mu.flush_table()
        with open(persist_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        assert data["version"] == 2
        assert data["models"]["m1"]["calls"] == 43
        assert "bytes_sent" not in data["models"]["m1"]

    def test_flat_legacy_loads_like_v1(self, tmp_path):
        """Flat legacy file (no version/models keys) still loads; tokens 0."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        raw = {
            "m1": {
                "model": "m1",
                "backend": "AAA",
                "calls": 5,
                "bytes_sent": 10,
                "endpoints": {},
                "errors": {},
                "first_seen": "10:00:00",
            }
        }
        with open(persist_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f)
        rows = mu.usage_snapshot()
        assert len(rows) == 1
        assert rows[0]["calls"] == 5
        assert rows[0]["input_tokens"] == 0

    def test_unknown_version_ignored(self, tmp_path):
        """version: 3 (unknown future format) → file ignored, table empty."""
        config, mu = _fresh()
        persist_file = str(tmp_path / mu.MODEL_USAGE_FILE)
        mu.set_persist_path(persist_file)
        raw = {
            "version": 3,
            "models": {"m1": {"model": "m1", "calls": 5}},
        }
        with open(persist_file, "w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f)
        assert mu.usage_snapshot() == []

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
    def test_reset_zeroes_counters_keeps_row_memory_and_file(self, tmp_path):
        """reset_model → calls/tokens zeroed, row stays in table and file."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        mu.record_model_usage("m")          # строка создана (calls=1)
        mu._TABLE["m"]["calls"] = 7         # сид счётчиков напрямую
        mu._TABLE["m"]["input_tokens"] = 300
        mu._TABLE["m"]["output_tokens"] = 500
        assert mu.reset_model("m") is True
        rows = mu.usage_snapshot()
        assert len(rows) == 1               # строка осталась
        assert rows[0]["calls"] == 0
        assert rows[0]["input_tokens"] == 0
        assert rows[0]["output_tokens"] == 0
        # Бэкенд/endpoints первой пробы не тронуты (строка целиком на месте)
        with open(persist_file, encoding="utf-8") as f:
            saved = yaml.safe_load(f)["models"]["m"]
        assert saved["calls"] == 0 and saved["input_tokens"] == 0
        assert saved["output_tokens"] == 0 and saved["model"] == "m"
        # повторный сброс существующей строки — True (строка жива)
        assert mu.reset_model("m") is True

    def test_reset_missing_row_returns_false(self, tmp_path):
        """reset_model for a row that is not in the table → False."""
        config, mu = _fresh()
        persist_file = _persist_setup(config, mu, tmp_path)
        assert mu.reset_model("nope") is False

    def test_flush_writes_dirty_table(self, tmp_path):
        """flush_table after mutations writes the file."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist_setup(config, mu, tmp_path)
        mu.record_model_usage("m1")
        mu.flush_table()
        with open(persist_file, encoding="utf-8") as f:
            assert "m1" in yaml.safe_load(f)["models"]


class TestReprobe:
    """Фоновая перепроверка эндпоинтов строки (кнопка «Перепроверить»).

    Ядро reprobe_model: endpoints/errors строки обновляются результатом
    новой пробы, calls/токены НЕ растут; строки нет / идёт первичная проба
    (probing=True) → False. start_reprobe: запуск daemon-потока, повторный
    при идущем reprobe → False; reprobe_state — снимок running/model."""

    def _seed_row(self, mu, model="m", backend="AAA", calls=5, probing=False,
                  endpoints=None, input_tokens=10, output_tokens=20):
        mu.reset_model_usage()
        mu._TABLE[model] = {
            "model": model,
            "backend": backend,
            "calls": calls,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "endpoints": endpoints if endpoints is not None else {},
            "errors": {},
            "first_seen": "10:00:00",
            "probing": probing,
        }

    def test_reprobe_updates_endpoints_only(self):
        """Проба возвращает endpoints/errors — строка обновляется ими, calls
        и токены НЕ трогаются; файл сохраняется (force)."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        self._seed_row(mu, calls=5, input_tokens=10, output_tokens=20)
        result = {
            "endpoints": {"completions": {"status": 200, "found": True}},
            "errors": {},
        }
        with mock.patch.object(config, "_resolve_backend", return_value=({"name": "AAA"}, "m")):
            with mock.patch.object(mu, "_backend_has_model", return_value=True):
                with mock.patch.object(mu, "_probe_model_endpoints", return_value=result) as m_probe:
                    assert mu.reprobe_model("m") is True
        assert m_probe.call_count == 1
        row = mu.usage_snapshot()[0]
        assert row["endpoints"] == {"completions": {"status": 200, "found": True}}
        assert row["calls"] == 5      # счётчик не вырос
        assert row["input_tokens"] == 10   # токены не выросли
        assert row["output_tokens"] == 20
        assert row["probing"] is False

    def test_reprobe_updates_backend_name_on_resolve(self):
        """Резолв сменился (бэкенд BBB) → backend строки обновляется."""
        config, mu = _fresh()
        self._seed_row(mu, backend="AAA")
        with mock.patch.object(config, "_resolve_backend", return_value=({"name": "BBB"}, "m")):
            with mock.patch.object(mu, "_backend_has_model", return_value=False):
                assert mu.reprobe_model("m") is True
        assert mu.usage_snapshot()[0]["backend"] == "BBB"
        # модели нет на бэкенде — пробу не делаем, endpoints не трогаем

    def test_reprobe_no_row_false(self):
        """Строки нет в таблице → False, исключений нет."""
        config, mu = _fresh()
        mu.reset_model_usage()
        assert mu.reprobe_model("m") is False

    def test_reprobe_probing_row_false(self):
        """Идёт первичная (первая) проба строки (probing=True) → False."""
        config, mu = _fresh()
        self._seed_row(mu, probing=True)
        assert mu.reprobe_model("m") is False

    def test_reprobe_exception_swallowed(self):
        """Исключение в пробе → лог, строка цела, endpoints не тронуты."""
        config, mu = _fresh()
        self._seed_row(mu)
        with mock.patch.object(config, "_resolve_backend", side_effect=RuntimeError("boom")):
            assert mu.reprobe_model("m") is True  # исключение поймано внутри
        row = mu.usage_snapshot()[0]
        assert row["calls"] == 5

    def test_reprobe_writes_file_on_persist(self, tmp_path):
        """При persist-пути результат reprobe сохраняется в файл сразу."""
        config, mu = _fresh()
        config.ADAPTER_MODEL_USAGE_ENABLE = False
        persist_file = _persist(tmp_path, config, mu)
        self._seed_row(mu)
        result = {
            "endpoints": {"completions": {"status": 200, "found": True}},
            "errors": {},
        }
        with mock.patch.object(config, "_resolve_backend", return_value=({"name": "AAA"}, "m")):
            with mock.patch.object(mu, "_backend_has_model", return_value=True):
                with mock.patch.object(mu, "_probe_model_endpoints", return_value=result):
                    mu.reprobe_model("m")
        with open(persist_file, encoding="utf-8") as f:
            saved = yaml.safe_load(f)
        assert saved["version"] == 2
        assert saved["models"]["m"]["endpoints"]["completions"]["found"] is True

    def test_start_reprobe_launches_worker(self):
        """start_reprobe: строка есть → True, снимок running; воркер
        (reprobe_model) отрабатывает и очищает снимок в finally."""
        config, mu = _fresh()
        self._seed_row(mu)
        with mock.patch.object(config, "_resolve_backend", return_value=({"name": "AAA"}, "m")):
            with mock.patch.object(mu, "_backend_has_model", return_value=True):
                with mock.patch.object(
                    mu, "_probe_model_endpoints",
                    return_value={"endpoints": {}, "errors": {}},
                ):
                    assert mu.start_reprobe("m") is True
        state = mu.reprobe_state()
        assert state["running"] is False  # воркер уже отработал и очистил
        assert state["model"] is None

    def test_start_reprobe_no_row_or_probing_false(self):
        """start_reprobe: строки нет / probing-строка → False (без потока)."""
        config, mu = _fresh()
        mu.reset_model_usage()
        assert mu.start_reprobe("m") is False
        self._seed_row(mu, probing=True)
        assert mu.start_reprobe("m") is False
        assert mu.reprobe_state()["running"] is False

    def test_start_reprobe_second_while_running_false(self):
        """Повторный start_reprobe при уже идущем reprobe → False."""
        config, mu = _fresh()
        self._seed_row(mu)
        with mock.patch.object(config, "_resolve_backend", return_value=({"name": "AAA"}, "m")):
            with mock.patch.object(mu, "_backend_has_model", return_value=True):
                with mock.patch.object(
                    mu, "_probe_model_endpoints",
                    side_effect=lambda *a: time.sleep(0.15) or {"endpoints": {}, "errors": {}},
                ) as m_probe:
                    assert mu.start_reprobe("m") is True   # первый — запущен
                    assert mu.start_reprobe("m") is False  # второй — уже идёт
                    # дать воркеру завершиться и очистить снимок
                    deadline = time.time() + 3
                    while mu.reprobe_state()["running"] and time.time() < deadline:
                        time.sleep(0.02)
                    assert m_probe.call_count == 1
        assert mu.reprobe_state()["running"] is False

    def test_reprobe_state_snapshot(self):
        """reprobe_state при идущем reprobe: running/model/started_at."""
        config, mu = _fresh()
        self._seed_row(mu)
        with mock.patch.object(config, "_resolve_backend", return_value=({"name": "AAA"}, "m")):
            with mock.patch.object(mu, "_backend_has_model", return_value=True):
                with mock.patch.object(
                    mu, "_probe_model_endpoints",
                    side_effect=lambda *a: time.sleep(0.3) or {"endpoints": {}, "errors": {}},
                ):
                    assert mu.start_reprobe("m") is True
                    state = mu.reprobe_state()
                    assert state["running"] is True
                    assert state["model"] == "m"
                    assert state["started_at"] is not None
                    deadline = time.time() + 3
                    while mu.reprobe_state()["running"] and time.time() < deadline:
                        time.sleep(0.02)
        assert mu.reprobe_state()["running"] is False
