"""Tests for backend_adapter.daemon — detachment and PID file utilities."""
import os
import sys
import subprocess
from unittest import mock


def _reload_all():
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]


class TestWritePidFile:
    """Tests for _write_pidfile().

    PID-файл пишется в ADAPTER_DEBUG_LOGPATH (v0.9.0): ADAPTER_PIDFILE
    задаёт имя файла (basename — абсолютный путь вне LOGPATH
    игнорируется); не задано — ``adapter.pid``. Директория создаётся при
    необходимости. Во всех тестах LOGPATH указывает на tmp_path — иначе
    запись ушла бы в ./tmp/logs (дефолт env) внутри репозитория.
    """

    def test_writes_current_pid_into_logpath(self, tmp_path):
        _reload_all()
        from backend_adapter.daemon import _write_pidfile
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        os.environ["ADAPTER_PIDFILE"] = "test.pid"
        _write_pidfile()
        written = int(open(tmp_path / "test.pid").read())
        assert written == os.getpid()

    def test_default_pidfile(self, tmp_path):
        """ADAPTER_PIDFILE не задан → ADAPTER_DEBUG_LOGPATH/adapter.pid."""
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        os.environ.pop("ADAPTER_PIDFILE", None)
        _reload_all()
        from backend_adapter.daemon import _write_pidfile
        _write_pidfile()
        written = int(open(tmp_path / "adapter.pid").read())
        assert written == os.getpid()

    def test_absolute_pidfile_uses_basename_in_logpath(self, tmp_path):
        """Абсолютный путь в ADAPTER_PIDFILE вне LOGPATH игнорируется:
        файл кладётся в LOGPATH под своим basename."""
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(tmp_path)
        os.environ["ADAPTER_PIDFILE"] = "/abs/other/test.pid"
        _reload_all()
        from backend_adapter.daemon import _write_pidfile
        _write_pidfile()
        assert (tmp_path / "test.pid").exists()
        assert not os.path.exists("/abs/other/test.pid")

    def test_logpath_created_if_missing(self, tmp_path):
        """Директория LOGPATH создаётся (вызов может произойти до стартового
        os.makedirs корня WEBUI в backend-adapter.py)."""
        target = tmp_path / "nested" / "logs"
        os.environ["ADAPTER_DEBUG_LOGPATH"] = str(target)
        os.environ["ADAPTER_PIDFILE"] = "adapter.pid"
        _reload_all()
        from backend_adapter.daemon import _write_pidfile
        _write_pidfile()
        assert int((target / "adapter.pid").read_text()) == os.getpid()


class TestDetach:
    """Tests for _detach().

    Real detach (double fork) cannot be tested in-process — it exits the
    process. We test the error path instead.
    """

    def test_fork_error_exits(self):
        """If os.fork() raises, the process should exit with code 1."""
        # Run a fresh subprocess that mocks os.fork before importing _detach
        code = """\
import os
def _raise_os_error():
    raise OSError(1, "Errno")
os.fork = _raise_os_error
from backend_adapter.daemon import _detach
_detach()
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True
        )
        assert result.returncode == 1
        assert "[FORK] Error" in result.stderr
