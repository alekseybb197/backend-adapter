#!/usr/bin/env python3
"""Unit tests for backend_adapter.artifact_refresh — фоновая дебаунсная
отрисовка деревьев артефактов (v0.9.10).

Tests cover:
  - notify() не рендерит сразу: пока тихий период дебаунса не истёк, генерации нет;
  - после дебаунса дерево появляется (artefacts/tree.html) без участия /session;
  - исключение внутри artifact_tree.generate не роняет воркер, а логируется WARNING;
  - запись части во время идущей генерации не теряется — директория снова
    попадает в очередь и перерисовывается (второй вызов generate);
  - reset()/flush() — управление очередью для тестов.
"""
import logging
import os
import sys
import threading
import time

import pytest


def _write_json(path: str, data) -> None:
    import json

    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _make_parts_dir(tmp_path, name: str = "s.parts") -> str:
    """Создать *.parts-директорию с минимальным валидным набором part-файлов."""
    d = os.path.join(str(tmp_path), name)
    os.makedirs(d, exist_ok=True)
    _write_json(os.path.join(d, "a-1-openai_body.json"), {"messages": [{"role": "user", "content": "Hi"}]})
    _write_json(os.path.join(d, "b-2-fetch_raw.json"), {"choices": [{"message": {"content": "Hello"}}]})
    return d


def _wait_until(pred, timeout: float = 6.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def _fresh_modules():
    """Свежие artifact_refresh/config (общий модульный стейт очереди)."""
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]
    from backend_adapter import artifact_refresh, config

    return artifact_refresh, config


@pytest.fixture(autouse=True)
def _clean_refresh(monkeypatch):
    """Гарантировать пустую очередь и остановленный воркер до/после теста.

    Тесты пересоздают модули через ``_fresh_modules()``, поэтому берём
    текущий модуль из ``sys.modules`` на каждом шаге, а не один раз на
    импорте фикстуры."""
    # Снятый флаг в окружении разработчика дал бы [WARN] на каждом реимпорте
    # config — тесту это не нужно (образец: tests/test_probe_json.py).
    monkeypatch.delenv("ADAPTER_DEBUG_PARTS", raising=False)

    def _current():
        return sys.modules.get("backend_adapter.artifact_refresh")

    m = _current()
    if m is not None:
        m.reset()
    yield
    m = _current()
    if m is not None:
        m.reset()


class TestNotifyCheap:
    def test_notify_does_not_render_immediately(self, tmp_path, monkeypatch):
        """notify() не блокируется на рендере: при большом дебаунсе дерева нет."""
        ar, config = _fresh_modules()
        monkeypatch.setattr(config, "ADAPTER_ARTIFACT_DEBOUNCE", 60)
        parts = _make_parts_dir(tmp_path)

        ar.notify(parts)
        time.sleep(1.5)  # воркер успевает проснуться минимум раз
        assert not os.path.exists(os.path.join(parts, "artefacts", "tree.html"))

    def test_empty_parts_dir_ignored(self, monkeypatch):
        ar, config = _fresh_modules()
        monkeypatch.setattr(config, "ADAPTER_ARTIFACT_DEBOUNCE", 0)
        ar.notify("")
        with ar._LOCK:
            assert not ar._PENDING

    def test_generate_removed_is_imported_lazily(self):
        """artifact_tree не тянется на импорте модуля (разрыв цикла в DAG)."""
        ar, _ = _fresh_modules()
        assert "backend_adapter.artifact_tree" not in sys.modules
        ar.notify("/nonexistent/definitely-not-parts")  # не должно импортировать
        assert "backend_adapter.artifact_tree" not in sys.modules


class TestDebouncedRender:
    def test_tree_rendered_after_debounce(self, tmp_path, monkeypatch):
        """После тихого периода дебаунса артефакты собираются сами, без /session."""
        ar, config = _fresh_modules()
        monkeypatch.setattr(config, "ADAPTER_ARTIFACT_DEBOUNCE", 0)
        parts = _make_parts_dir(tmp_path)

        ar.notify(parts)
        tree = os.path.join(parts, "artefacts", "tree.html")
        assert _wait_until(lambda: os.path.isfile(tree)), "tree.html не появился"
        assert os.path.isfile(os.path.join(parts, "artefacts", ".build_state.json"))

    def test_second_notify_after_quiet_rerenders(self, tmp_path, monkeypatch):
        """Новая запись после паузы обновляет дерево (инкрементально, чекпойнт жив)."""
        ar, config = _fresh_modules()
        monkeypatch.setattr(config, "ADAPTER_ARTIFACT_DEBOUNCE", 0)
        parts = _make_parts_dir(tmp_path)

        ar.notify(parts)
        tree = os.path.join(parts, "artefacts", "tree.html")
        assert _wait_until(lambda: os.path.isfile(tree))
        mtime1 = os.path.getmtime(tree)

        _write_json(
            os.path.join(parts, "c-3-openai_body.json"),
            {"messages": [{"role": "user", "content": "Second"}]},
        )
        ar.notify(parts)
        assert _wait_until(lambda: os.path.getmtime(tree) > mtime1)
        assert os.path.isfile(os.path.join(parts, "artefacts", ".build_state.json"))


class TestRobustness:
    def test_generate_error_logged_and_worker_survives(self, tmp_path, monkeypatch, caplog):
        """Сбой generate → WARNING, поток жив и обрабатывает следующую запись."""
        ar, config = _fresh_modules()
        monkeypatch.setattr(config, "ADAPTER_ARTIFACT_DEBOUNCE", 0)
        from backend_adapter import artifact_tree

        calls: list[str] = []

        def boom(parts_dir, verbose=True):
            calls.append(parts_dir)
            raise RuntimeError("boom")

        monkeypatch.setattr(artifact_tree, "generate", boom)
        parts = _make_parts_dir(tmp_path)

        with caplog.at_level(logging.WARNING, logger="artifact_refresh"):
            ar.notify(parts)
            assert _wait_until(lambda: "не удалась" in caplog.text), caplog.text

        assert calls  # генерация действительно вызывалась
        # Поток не умер: следующая пометка снова доходит до generate
        ar.notify(parts)
        assert _wait_until(lambda: len(calls) >= 2)
        with ar._LOCK:
            assert parts not in ar._INFLIGHT

    def test_notify_during_generation_not_lost(self, tmp_path, monkeypatch):
        """Запись части во время идущей генерации не теряется: после снятия
        с очереди она снова попадает туда и перерисовывается вторым вызовом."""
        ar, config = _fresh_modules()
        monkeypatch.setattr(config, "ADAPTER_ARTIFACT_DEBOUNCE", 0)
        from backend_adapter import artifact_tree

        started = threading.Event()
        release = threading.Event()
        calls: list[str] = []

        def slow(parts_dir, verbose=True):
            calls.append(parts_dir)
            started.set()
            release.wait(timeout=5)

        monkeypatch.setattr(artifact_tree, "generate", slow)
        parts = _make_parts_dir(tmp_path)

        ar.notify(parts)
        assert started.wait(timeout=5), "генерация не началась"
        # Пока generate висит, приходит новая запись части
        ar.notify(parts)
        release.set()

        assert _wait_until(lambda: len(calls) >= 2), f"calls={len(calls)}"
        assert ar.flush(timeout=5)


class TestResetFlush:
    def test_flush_true_when_idle(self):
        ar, _ = _fresh_modules()
        assert ar.flush(timeout=1.0)

    def test_reset_stops_worker_and_drains(self, tmp_path, monkeypatch):
        ar, config = _fresh_modules()
        monkeypatch.setattr(config, "ADAPTER_ARTIFACT_DEBOUNCE", 60)
        parts = _make_parts_dir(tmp_path)

        ar.notify(parts)
        assert ar._THREAD is not None and ar._THREAD.is_alive()
        ar.reset()
        assert ar._THREAD is None
        with ar._LOCK:
            assert not ar._PENDING
            assert not ar._LAST_WRITE
