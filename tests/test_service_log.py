#!/usr/bin/env python3
"""Tests for backend_adapter.service_log — лог сервиса под launchd (ADR-041).

Модуль ставит SIGHUP-переоткрытие лога и удаляет файл при очистке. Проверяем
обе стороны контракта, включая отрицательные случаи: в терминале (нет
``XPC_SERVICE_NAME``) обработчик ставиться не должен — там SIGHUP обязан
сохранять смысл завершения (ADR-026).
"""

import os
import signal

import pytest

from backend_adapter import service_log, templates

try:  # fcntl есть не на всех платформах; на Windows модуль — no-op
    import fcntl
except ImportError:  # pragma: no cover — Windows
    fcntl = None  # type: ignore[assignment]

_HAS_F_GETPATH = fcntl is not None and hasattr(fcntl, "F_GETPATH")


@pytest.fixture
def isolated_signal(monkeypatch):
    """Не трогать настоящие обработчики сигналов процесса pytest."""
    monkeypatch.setattr(signal, "signal", lambda *a: None)
    monkeypatch.setattr(service_log, "_HUP_INSTALLED", False)


@pytest.fixture
def launchd_env(monkeypatch, tmp_path):
    """Процесс «под launchd», путь лога известен (``fd_log_path`` подменён).

    Настоящий ``fd_log_path`` опирается на macOS-специфичный ``F_GETPATH``;
    на Linux он возвращает ``None``, поэтому для проверки логики установки
    обработчика путь задаётся напрямую — тест не зависит от платформы.
    """
    monkeypatch.setenv("XPC_SERVICE_NAME", templates.LAUNCHD_LABEL)
    path = tmp_path / "adapter.log"
    path.write_text("", encoding="utf-8")
    monkeypatch.setattr(service_log, "fd_log_path", lambda fd: str(path))
    with open(path, "a", encoding="utf-8") as f:
        monkeypatch.setattr("sys.stdout", f)
        yield path


class TestFdLogPath:
    @pytest.mark.skipif(not _HAS_F_GETPATH, reason="F_GETPATH есть только на macOS")
    def test_regular_file_returns_path(self, tmp_path):
        path = tmp_path / "a.log"
        path.write_text("", encoding="utf-8")
        with open(path, "a", encoding="utf-8") as f:
            assert service_log.fd_log_path(f.fileno()) == str(path.resolve())

    def test_decodes_nul_terminated_path(self, monkeypatch):
        """Путь — C-строка с NUL: декодируется без зависимости от платформы."""
        if fcntl is None:  # pragma: no cover — Windows
            pytest.skip("fcntl недоступен")
        monkeypatch.setattr(fcntl, "F_GETPATH", 50, raising=False)
        monkeypatch.setattr(fcntl, "fcntl", lambda fd, cmd, buf: b"/tmp/x.log\0junk")
        assert service_log.fd_log_path(1) == "/tmp/x.log"

    def test_non_bytes_result_returns_none(self, monkeypatch):
        if fcntl is None:  # pragma: no cover — Windows
            pytest.skip("fcntl недоступен")
        monkeypatch.setattr(fcntl, "F_GETPATH", 50, raising=False)
        monkeypatch.setattr(fcntl, "fcntl", lambda fd, cmd, buf: 0)
        assert service_log.fd_log_path(1) is None

    def test_invalid_fd_returns_none(self):
        assert service_log.fd_log_path(10_000) is None

    def test_non_file_returns_none(self):
        """/dev/null — не даёт полезного пути (или не поддерживается)."""
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            path = service_log.fd_log_path(fd)
        finally:
            os.close(fd)
        assert path in (None, os.devnull)


class TestInstallHupHandler:
    def test_terminal_does_not_install(self, monkeypatch, isolated_signal):
        """Без XPC_SERVICE_NAME SIGHUP обязан завершать процесс — не ставим."""
        monkeypatch.delenv("XPC_SERVICE_NAME", raising=False)
        assert service_log.install_hup_handler() is False
        assert service_log._HUP_INSTALLED is False

    def test_other_service_does_not_install(self, monkeypatch, isolated_signal):
        monkeypatch.setenv("XPC_SERVICE_NAME", "com.apple.something.else")
        assert service_log.install_hup_handler() is False

    def test_launchd_with_file_installs(self, monkeypatch, isolated_signal, launchd_env):
        captured: list[tuple[int, object]] = []

        def fake_signal(signum, handler):
            captured.append((signum, handler))

        monkeypatch.setattr(signal, "signal", fake_signal)
        assert service_log.install_hup_handler() is True
        assert captured and captured[0][0] == signal.SIGHUP
        assert callable(captured[0][1])

    def test_second_call_is_noop(self, monkeypatch, isolated_signal, launchd_env):
        calls: list[int] = []
        monkeypatch.setattr(signal, "signal", lambda signum, h: calls.append(signum))
        assert service_log.install_hup_handler() is True
        assert service_log.install_hup_handler() is True
        assert calls == [signal.SIGHUP]

    def test_value_error_from_signal_returns_false(self, monkeypatch, isolated_signal, launchd_env):
        """Вне главного потока signal.signal бросает ValueError — тихий False."""

        def boom(signum, handler):
            raise ValueError("signal only works in main thread")

        monkeypatch.setattr(signal, "signal", boom)
        assert service_log.install_hup_handler() is False


class TestReopen:
    def test_writes_into_new_file(self, tmp_path):
        """После переоткрытия строки идут в новый файл, старый цел."""
        old = tmp_path / "old.log"
        old.write_text("before\n", encoding="utf-8")
        new = tmp_path / "new.log"
        new.write_text("", encoding="utf-8")

        out_fd = os.open(old, os.O_WRONLY | os.O_APPEND)
        err_fd = os.open(old, os.O_WRONLY | os.O_APPEND)
        try:
            service_log._reopen(str(new), out_fd=out_fd, err_fd=err_fd)
            os.write(out_fd, b"after\n")
            os.write(err_fd, b"err-line\n")
        finally:
            os.close(out_fd)
            os.close(err_fd)

        assert old.read_text(encoding="utf-8") == "before\n"
        body = new.read_text(encoding="utf-8")
        assert "after\n" in body and "err-line\n" in body
        assert "[SERVICE]" in body

    def test_creates_missing_file(self, tmp_path):
        target = tmp_path / "fresh.log"
        out_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            service_log._reopen(str(target), out_fd=out_fd, err_fd=out_fd)
        finally:
            os.close(out_fd)
        assert target.is_file()
        assert (target.stat().st_mode & 0o777) == 0o600

    def test_failure_does_not_raise(self, tmp_path):
        """Сбой открытия — не исключение: процесс продолжает работать."""
        bad = str(tmp_path / "no-such-dir" / "log")
        out_fd = os.open(os.devnull, os.O_WRONLY)
        try:
            service_log._reopen(bad, out_fd=out_fd, err_fd=out_fd)
        finally:
            os.close(out_fd)


class TestClearLog:
    def test_removes_file(self, tmp_path):
        path = tmp_path / "adapter.log"
        path.write_text("data\n", encoding="utf-8")
        assert service_log.clear_log(str(path)) is True
        assert not path.exists()

    def test_missing_file_is_not_error(self, tmp_path):
        assert service_log.clear_log(str(tmp_path / "absent.log")) is False

    def test_does_not_truncate_while_file_exists(self, tmp_path):
        """Файл именно удаляется: truncate под launchd оставляет NUL-дырки.

        Открытый дескриптор после ``clear_log`` указывает на unlinked inode
        (``st_nlink == 0``) — значит файл удалён, а не обнулён. Проверка не
        зависит от переиспользования номеров inode (на tmpfs возможно).
        """
        path = tmp_path / "adapter.log"
        path.write_text("x" * 100, encoding="utf-8")
        fd = os.open(path, os.O_RDONLY)
        try:
            assert service_log.clear_log(str(path)) is True
            assert not path.exists()
            assert os.fstat(fd).st_nlink == 0
        finally:
            os.close(fd)
