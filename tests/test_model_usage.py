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
"""
import threading
import time
from unittest import mock

import pytest


def _fresh():
    """Reload config + model_usage with clean state (conftest defaults:
    ADAPTER_MODEL_USAGE_ENABLE="0"). Returns (config, model_usage)."""
    import sys

    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]
    from backend_adapter import config, model_usage

    return config, model_usage


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
