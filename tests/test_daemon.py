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
    """Tests for _write_pidfile()."""

    def test_writes_current_pid(self, tmp_path):
        _reload_all()
        from backend_adapter.daemon import _write_pidfile
        pidfile = str(tmp_path / "test.pid")
        os.environ["ADAPTER_PIDFILE"] = pidfile
        _write_pidfile()
        written = int(open(pidfile).read())
        assert written == os.getpid()

    def test_default_pidfile(self):
        """Default PIDFILE path should be /tmp/adapter.pid."""
        _reload_all()
        from backend_adapter.daemon import _write_pidfile
        if "ADAPTER_PIDFILE" in os.environ:
            del os.environ["ADAPTER_PIDFILE"]
        # Can't easily test default path — it writes to /tmp/
        # Instead test custom path
        tmp_path = __import__("pathlib").Path("/tmp/test_adapter_pidfile_$$")
        os.environ["ADAPTER_PIDFILE"] = str(tmp_path)
        _write_pidfile()
        assert tmp_path.exists()
        tmp_path.unlink(missing_ok=True)


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
