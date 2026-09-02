"""Shared fixtures for backend_adapter tests.

Key challenge: ``config.py`` and ``session_log.py`` read ``os.environ``
at module import time — global variables capture the env snapshot.
Solution: ``fresh_env`` monkeypatches env vars, then ``importlib.reload()``
re-evaluates the module against the patched environment.
"""
import importlib
import os
import sys
import threading
import time
import json
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from unittest import mock

import pytest


# ---------------------------------------------------------------------------
# Environment / module reload
# ---------------------------------------------------------------------------

@pytest.fixture
def fresh_env(monkeypatch):
    """Return a helper that patches env vars and reloads config/session_log.

    Usage::

        config = fresh_env(ADAPTER_DEBUG_ENABLE="0",
                           ADAPTER_DEBUG_LOGFILE="",
                           ADAPTER_TRACE_LOGFILE="",
                           ADAPTER_BACKEND_KEY="test-key")

    After the call ``config`` is the *reloaded* module; all downstream
    modules that imported ``config.X`` still see the old values, so for
    deep modules prefer ``isolate_logs`` + direct attribute manipulation.
    """
    defaults = {
        "ADAPTER_DEBUG_ENABLE": "0",
        "ADAPTER_DEBUG_LOGFILE": "",
        "ADAPTER_TRACE_LOGFILE": "",
        "ADAPTER_BACKEND_KEY": "",
        "ADAPTER_BACKEND_BASE": "http://localhost:9998",
        "ADAPTER_PROXY_PORT": "9998",
        "ADAPTER_TIMEOUT": "300",
        "ADAPTER_RETRY_COUNT": "3",
        "ADAPTER_STRICT_MODELS": "1",
        "ADAPTER_STREAMING_ENABLE": "1",
        "ADAPTER_STREAM_INCLUDE_USAGE": "1",
        "ADAPTER_MODELS_MAPPING": "",
        "ADAPTER_BACKEND_CONFIG": "",
        "ADAPTER_DEBUG_TAGS_OUT": "0",
        "ADAPTER_DEBUG_TAGS_FULL": "",
        "ADAPTER_WEBUI_ENABLE": "0",
        "ADAPTER_WEBUI_PORT": "8765",
        "ADAPTER_DEBUG_TRIM": "3000",
        "ADAPTER_DEBUG_TOOLS": "0",
        "ADAPTER_DEBUG_TOOLS_ERROR": "0",
        "ADAPTER_TRACE_REASONING_MAX_CHARS": "0",
        "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS": "0",
        "ADAPTER_SENSITIVE_LOGGING_ENABLE": "0",
        "ADAPTER_SKILL_PATTERNS": "",
        "ADAPTER_PIDFILE": "",
    }

    for k, v in defaults.items():
        monkeypatch.setenv(k, v)

    # Remove modules from sys.modules so reload actually re-executes them
    to_remove = [
        name for name in list(sys.modules)
        if name.startswith("backend_adapter")
    ]
    for mod_name in to_remove:
        del sys.modules[mod_name]

    # Now re-import
    from backend_adapter import config, session_log

    return config, session_log


def _default_config():
    """Re-import config with defaults (called by tests that need defaults)."""
    defaults = {
        "ADAPTER_DEBUG_ENABLE": "0",
        "ADAPTER_DEBUG_LOGFILE": "",
        "ADAPTER_TRACE_LOGFILE": "",
        "ADAPTER_BACKEND_KEY": "",
        "ADAPTER_BACKEND_BASE": "http://localhost:9998",
        "ADAPTER_PROXY_PORT": "9998",
        "ADAPTER_TIMEOUT": "300",
        "ADAPTER_RETRY_COUNT": "3",
        "ADAPTER_STRICT_MODELS": "1",
        "ADAPTER_STREAMING_ENABLE": "1",
        "ADAPTER_STREAM_INCLUDE_USAGE": "1",
        "ADAPTER_MODELS_MAPPING": "",
        "ADAPTER_BACKEND_CONFIG": "",
        "ADAPTER_DEBUG_TAGS_OUT": "0",
        "ADAPTER_DEBUG_TAGS_FULL": "",
        "ADAPTER_WEBUI_ENABLE": "0",
        "ADAPTER_WEBUI_PORT": "8765",
        "ADAPTER_DEBUG_TRIM": "3000",
        "ADAPTER_DEBUG_TOOLS": "0",
        "ADAPTER_DEBUG_TOOLS_ERROR": "0",
        "ADAPTER_TRACE_REASONING_MAX_CHARS": "0",
        "ADAPTER_TRACE_TOOL_FIELD_MAX_CHARS": "0",
        "ADAPTER_SENSITIVE_LOGGING_ENABLE": "0",
        "ADAPTER_SKILL_PATTERNS": "",
        "ADAPTER_PIDFILE": "",
    }
    for k, v in defaults.items():
        os.environ[k] = v
    to_remove = [
        name for name in list(sys.modules)
        if name.startswith("backend_adapter")
    ]
    for mod_name in to_remove:
        del sys.modules[mod_name]
    from backend_adapter import config
    return config


# ---------------------------------------------------------------------------
# Isolate logs and global state between tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_logs(fresh_env):
    """Auto-cleanup global mutable state after every test.

    Resets session_log, tracer, config globals to empty state so tests
    don't interfere with each other.
    """
    from backend_adapter import session_log, tracer, config

    # Reset session_log state
    session_log._session_logs.clear()
    session_log._session_file_ts.clear()
    session_log._parts_dir.clear()
    session_log._parts_dir_ts.clear()
    session_log._debug_json_seq = 0
    session_log._last_log_session_id = ""
    # Save current paths
    saved_debug_path = session_log._DEBUG_PATH
    saved_debug_is_dir = session_log._DEBUG_IS_DIR
    saved_trace_path = session_log._TRACE_PATH
    saved_trace_is_dir = session_log._TRACE_IS_DIR
    # Clear log paths for isolation
    session_log._DEBUG_PATH = ""
    session_log._DEBUG_IS_DIR = False
    session_log._TRACE_PATH = ""
    session_log._TRACE_IS_DIR = False

    # Reset tracer state
    tracer._session_seq.clear()
    tracer._tool_use_producers.clear()
    tracer._tool_use_names.clear()

    # Reset config globals
    config._AVAILABLE_MODELS.clear()
    config._MAP.clear()
    config._BACKENDS.clear()
    config._BACKEND_BY_NAME.clear()
    config._MODEL_TO_BACKEND.clear()
    config._DEFAULT_BACKEND = None

    yield

    # Restore after test
    session_log._DEBUG_PATH = saved_debug_path
    session_log._DEBUG_IS_DIR = saved_debug_is_dir
    session_log._TRACE_PATH = saved_trace_path
    session_log._TRACE_IS_DIR = saved_trace_is_dir


# ---------------------------------------------------------------------------
# Fake HTTP backend for integration tests
# ---------------------------------------------------------------------------

class FakeBackendHandler(BaseHTTPRequestHandler):
    """HTTP handler that returns pre-programmed responses."""

    models_response = None
    completions_response = None
    completions_status = 200
    request_count = 0
    requests = []  # list of all (path, method, body) requests

    def log_message(self, format, *args):
        """Suppress server log noise."""
        pass

    def do_GET(self):
        FakeBackendHandler.request_count += 1
        FakeBackendHandler.requests.append((self.path, "GET", None))
        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            data = self.models_response or {"object": "list", "data": []}
            self.wfile.write(json.dumps(data).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        FakeBackendHandler.request_count += 1
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode() if length else None
        FakeBackendHandler.requests.append((self.path, "POST", body))

        if self.path == "/v1/chat/completions":
            self.send_response(FakeBackendHandler.completions_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            if FakeBackendHandler.completions_status == 200 and FakeBackendHandler.completions_response:
                self.wfile.write(json.dumps(FakeBackendHandler.completions_response).encode())
            elif FakeBackendHandler.completions_status in (429, 502, 503, 504):
                self.wfile.write(json.dumps({"error": "backend error"}).encode())
        else:
            self.send_response(404)
            self.end_headers()


class FakeBackend:
    """Standalone HTTP server serving pre-programmed responses.

    Usage::

        backend = FakeBackend()
        backend.models_response = {"data": [...]}
        backend.completions_response = {...}
        backend.serve()
        # ... calls ...
        backend.close()

    Response configuration is a property facade over the handler's *class*
    attributes: ThreadingHTTPServer instantiates a fresh handler per request,
    and the handler reads ``FakeBackendHandler.completions_response``, so
    instance-level settings would never be seen. The facade lets tests keep
    the natural ``backend.completions_response = ...`` API while writes land
    on the class. Reads happen per request, so tests may also reconfigure the
    backend mid-test (e.g. 503 twice, then 200 for a retry scenario).
    """

    def __init__(self):
        self.server = None
        self.thread = None
        self.host = "127.0.0.1"

    # -- response configuration: instance facade over handler class attrs --
    @property
    def models_response(self):
        return FakeBackendHandler.models_response

    @models_response.setter
    def models_response(self, value):
        FakeBackendHandler.models_response = value

    @property
    def completions_response(self):
        return FakeBackendHandler.completions_response

    @completions_response.setter
    def completions_response(self, value):
        FakeBackendHandler.completions_response = value

    @property
    def completions_status(self):
        return FakeBackendHandler.completions_status

    @completions_status.setter
    def completions_status(self, value):
        FakeBackendHandler.completions_status = value

    @property
    def request_count(self):
        return FakeBackendHandler.request_count

    @property
    def requests(self):
        return FakeBackendHandler.requests

    def serve(self):
        """Start the fake backend server in a background thread."""
        self.server = ThreadingHTTPServer((self.host, 0), FakeBackendHandler)
        self.port = self.server.server_address[1]
        self.base_url = f"http://{self.host}:{self.port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        FakeBackendHandler.request_count = 0
        FakeBackendHandler.requests = []
        return self

    def close(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()

    def __enter__(self):
        return self.serve()

    def __exit__(self, *args):
        self.close()


@pytest.fixture
def fake_backend():
    """Provide a FakeBackend instance and clean up after the test."""
    backend = FakeBackend()
    # Reset handler state
    FakeBackendHandler.models_response = None
    FakeBackendHandler.completions_response = None
    FakeBackendHandler.completions_status = 200
    FakeBackendHandler.request_count = 0
    FakeBackendHandler.requests = []
    yield backend
    backend.close()


# ---------------------------------------------------------------------------
# Helpers for testing streaming
# ---------------------------------------------------------------------------

class FakeRespStream:
    """Fake response stream that yields bytes lines for streaming tests.

    Usage::

        stream = FakeRespStream([
            b'data: {"choices": [{"delta": {"content": "Hello"}}]}\\n\\n',
            b'data: [DONE]\\n\\n',
        ])
    """

    def __init__(self, lines: list[bytes]):
        self.lines = iter(lines)

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self.lines)
        except StopIteration:
            raise StopIteration


class FakeWfile:
    """Captures what ``wfile.write`` receives for testing streaming output."""

    def __init__(self):
        self.data = b""

    def write(self, data: bytes):
        self.data += data

    def flush(self):
        pass
