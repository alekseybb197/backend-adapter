#!/usr/bin/env python3
"""Интеграционный прогон адаптера как реального процесса (v0.8.6+).

Проверяет то, что unit-тесты не видят: РЕАЛЬНЫЙ процесс backend-adapter.py с fake-
бэкендом (tests/conftest.FakeBackend):

- WEBUI и Prometheus-экспортёр поднимаются без ADAPTER_WEBUI_ENABLE (его
  больше нет — v0.8.6), корень — ADAPTER_DEBUG_LOGPATH;
- консольные debug-блоки видны при ADAPTER_DEBUG_ENABLE=0, файлов на диске
  нет (кроме model-usage.yaml после запросов);
- файловая запись session-*.log/*.jsonl появляется при ADAPTER_DEBUG_ENABLE=1;
- колонка Cost на странице считается из токенов × тарифов;
- корректное завершение по сигналам: SIGINT/SIGTERM → вежливое завершение
  (rc=0, «[EXIT] Bye», без traceback), повторный SIGINT во время завершения
  → немедленный выход rc=130 (см. backend_adapter/shutdown.py).

Тест медленный (спавн реального процесса + стартовая проба бэкенда + три
жизненных цикла с ожиданием WEBUI, ~30 с) и выполняется В ОБЩЕМ прогоне
pytest (маркер @pytest.mark.manual — регистрация и запуск через
venv/bin/pytest -m manual, чтобы при желании гонять только его); скорость
приемлема, а контроль сигнального завершения реального процесса — часть
контракта graceful shutdown (см. ADR), поэтому по умолчанию тест НЕ
пропускается.

ВНИМАНИЕ: процесс-ребёнок пишет model-usage.yaml и session-* в logs_dir —
ТОЛЬКО в tmp-директорию теста (env ADAPTER_DEBUG_LOGPATH), наружу ничего не
выходит; FakeBackend слушает случайный порт.
"""
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from urllib import error as url_error

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PROJ = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
from conftest import FakeBackend  # noqa: E402

pytestmark = pytest.mark.manual


# ---------------------------------------------------------------------------
# Хелперы
# ---------------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class AdapterProc:
    """Запущенный процесс адаптера + накопитель его stdout/stderr."""

    def __init__(self, proc, buf):
        self.proc = proc
        self.buf = buf  # bytearray, наполняется фоновым потоком-дренером

    @property
    def output(self) -> str:
        return bytes(self.buf).decode(errors="replace")

    def send_signal(self, sig):
        self.proc.send_signal(sig)

    def wait(self, timeout=8) -> int:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait(timeout=5)


def _spawn(env) -> AdapterProc:
    """Запуск адаптера; вывод копится в bytearray (фоновый поток)."""
    buf = bytearray()
    proc = subprocess.Popen(
        [sys.executable, "backend-adapter.py"], cwd=_PROJ, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def _drain():
        while True:
            chunk = proc.stdout.read1(4096)  # read1: отдаёт доступное
            if not chunk:
                break
            buf.extend(chunk)

    threading.Thread(target=_drain, daemon=True).start()
    return AdapterProc(proc, buf)


def _wait_http(port: int, path: str = "/healthz", deadline_s: float = 20) -> int | None:
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}{path}", timeout=2
            ) as r:
                return r.status
        except Exception:
            time.sleep(0.3)
    return None


def _http_get(url: str):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.read().decode()


def _http_post(url: str):
    req = urllib.request.Request(url, method="POST", data=b"")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except url_error.HTTPError as e:
        return e.code, e.read().decode()


def _chat(proxy_port: int, body: dict) -> tuple[int, str]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{proxy_port}/v1/messages", data=data, method="POST",
        headers={"Content-Type": "application/json", "x-api-key": "sk-fake",
                 "anthropic-version": "2023-06-01",
                 "X-Claude-Code-Session-Id": "manual-check-session"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read().decode()
    except url_error.HTTPError as e:
        return e.code, e.read().decode()


def _adapter_env(yaml_path: str, tariffs_path: str, proxy_port: int,
                 web_port: int, exp_port: int, logs_dir: str,
                 debug_enable: str, trim: str = "3000") -> dict:
    """env для процесса адаптера: чистое ADAPTER_-окружение + тестовые пути."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith("ADAPTER_") or k in ("FAKE_KEY",):
            del env[k]
    env.update({
        "PYTHONUNBUFFERED": "1",  # шапка/консоль видна в pipe сразу
        "FAKE_KEY": "sk-fake",
        "ADAPTER_BACKEND_CONFIG": yaml_path,
        "ADAPTER_PROXY_PORT": str(proxy_port),
        "ADAPTER_WEBUI_PORT": str(web_port),
        "ADAPTER_EXPORTER_PORT": str(exp_port),
        "ADAPTER_MODELS_TARIFFS": tariffs_path,
        # WEBUI_ENABLE больше НЕ задаём — должен подняться сам (v0.8.6)
        "ADAPTER_DEBUG_ENABLE": debug_enable,
        "ADAPTER_DEBUG_LOGPATH": logs_dir,
        "ADAPTER_DEBUG_TRIM": trim,
    })
    return env


def _usage_cost_cell(page: str, row: int = 0) -> str | None:
    """Текст ячейки Cost строки usage-row-<row> статус-страницы.

    Ячейка Cost не несёт class/data-атрибута (рендерится как 6-й <td>
    строки: Модель|Бэкенд|Вызовов|Input|Output|Cost|Endpoints|Действия) —
    берём ячейку по позиции внутри <tr id="usage-row-N">. None — строки
    с таким номером на странице нет."""
    m = re.search(rf'<tr id="usage-row-{row}">(.*?)</tr>', page, re.S)
    if not m:
        return None
    cells = re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)
    if len(cells) < 6:
        return None
    return re.sub(r"<[^>]+>", "", cells[5]).strip()


# ---------------------------------------------------------------------------
# Тест
# ---------------------------------------------------------------------------

class TestManualAdapterProcess:
    """Реальный процесс адаптера: WEBUI/экспортёр/консоль/файлы/Cost/сигналы.

    Полный жизненный цикл (старт при ENABLE=0 → вызов → Cost → сброс →
    рестарт при ENABLE=1 → файловая запись → три варианта сигнального
    завершения); ср. unit-покрытие в test_webui_status/test_shutdown.
    Намеренно один большой тест: спавн процесса дорог (стартовая проба
    бэкенда ~сек), дробить на части — множить время прогона."""

    def test_full_lifecycle(self, tmp_path):
        logs_dir = str(tmp_path / "logs")
        os.makedirs(logs_dir)

        # --- fake backend ---
        be = FakeBackend()
        be.models_response = {"object": "list",
                              "data": [{"id": "qwen-test", "object": "model"}]}
        be.serve()
        try:
            # YAML-конфиг бэкенда (пробуем только completions)
            yaml_path = str(tmp_path / "adapter.yaml")
            with open(yaml_path, "w") as f:
                f.write("backend:\n"
                        "  - name: fake\n"
                        f"    base: {be.base_url}\n"
                        "    key: FAKE_KEY\n"
                        "    probe:\n"
                        "      - completions: qwen-test\n")

            # тарифы
            tariffs_path = str(tmp_path / "tariffs.yaml")
            with open(tariffs_path, "w") as f:
                f.write("tariffs:\n"
                        "  - name: qwen-test\n"
                        "    input_price: 0,25\n"   # запятая-разделитель
                        "    output_price: 0,75\n"
                        "    currency: USD\n"
                        "    price_per: 1000000\n")

            proxy_port = _free_port()
            web_port = _free_port()
            exp_port = _free_port()

            env = _adapter_env(yaml_path, tariffs_path, proxy_port,
                               web_port, exp_port, logs_dir,
                               debug_enable="0")
            ap = _spawn(env)
            try:
                # --- п.1-2: старт без WEBUI_ENABLE; консоль при ENABLE=0 ---
                time.sleep(4)  # стартовая проба fake-бэкенда (мгновенная)
                assert ap.proc.poll() is None, "adapter упал при старте"
                assert _wait_http(web_port) == 200, f"WEBUI не поднялся на :{web_port}"
                assert _wait_http(exp_port, "/metrics") == 200, \
                    f"экспортёр не поднялся на :{exp_port}"
                out = ap.output
                assert "Backend-Adapter v0.8.6" in out, out[:400]
                assert "[INIT] Probing backend 'fake'" in out, out[:400]
                assert "[WEBUI]" in out and "root:" in out, out[:400]
                files = sorted(os.listdir(logs_dir))
                assert all(f == "model-usage.yaml" for f in files), files

                # --- Cost на странице и в usage-таблице ---
                req_body = {"model": "qwen-test",
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 1}
                be.completions_response = {
                    "id": "x", "object": "chat.completion", "model": "qwen-test",
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant", "content": "ok"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 2000, "completion_tokens": 400,
                              "total_tokens": 2400},
                }
                st, _ = _chat(proxy_port, req_body)
                assert st == 200, st
                time.sleep(1)
                _, page = _http_get(f"http://127.0.0.1:{web_port}/")
                assert "<th>Cost</th>" in page, "нет заголовка Cost"
                assert "qwen-test" in page  # usage-строка в таблице есть
                # 2000×0,25/1e6 + 400×0,75/1e6 = 0,0008 → два знака: «0,00 USD»
                assert _usage_cost_cell(page) == "0,00 USD", _usage_cost_cell(page)

                # сброс счётчиков → токены 0 → Cost «--»
                st, _ = _http_post(
                    f"http://127.0.0.1:{web_port}/api/model-usage/reset"
                    f"?model=qwen-test")
                assert st in (200, 303), st
                time.sleep(1)
                _, page = _http_get(f"http://127.0.0.1:{web_port}/")
                assert _usage_cost_cell(page) == "—", _usage_cost_cell(page)

                # удаление строки → usage-строка исчезает со страницы (сам
                # бэкенд и его модели в таблице бэкендов остаются — qwen-test
                # там по-прежнему виден как модель бэкенда)
                st, _ = _http_post(
                    f"http://127.0.0.1:{web_port}/api/model-usage/delete"
                    f"?model=qwen-test")
                assert st in (200, 303), st
                time.sleep(1)
                _, page = _http_get(f"http://127.0.0.1:{web_port}/")
                assert 'id="usage-row-' not in page, "usage-строка не удалена"
                assert _usage_cost_cell(page) is None, _usage_cost_cell(page)
            finally:
                # вежливая остановка процесса перед рестартом при ENABLE=1
                ap.proc.terminate()
                try:
                    ap.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ap.proc.kill()

            # --- п.3: файловая запись при ENABLE=1 ---
            env = _adapter_env(yaml_path, tariffs_path, proxy_port,
                               web_port, exp_port, logs_dir,
                               debug_enable="1")
            ap = _spawn(env)
            try:
                time.sleep(4)
                assert ap.proc.poll() is None, "adapter упал при старте (ENABLE=1)"
                assert _wait_http(web_port) == 200, "WEBUI не поднялся при ENABLE=1"
                # вызов, чтобы появились session-*.log/*.jsonl
                be.completions_response = None  # 200 без usage — валидный вызов
                _chat(proxy_port, req_body)
                time.sleep(3)
                files2 = sorted(os.listdir(logs_dir))
                assert any(f.startswith("session-") and f.endswith(".log")
                           for f in files2), files2
                assert any(f.endswith(".jsonl") for f in files2), files2

                # --- п.6: корректное завершение по сигналам ---
                # 6a: один Ctrl-C (SIGINT) — вежливое завершение rc=0.
                ap.send_signal(signal.SIGINT)
                rc = ap.wait(timeout=8)
                out = ap.output
                assert rc == 0, f"SIGINT: rc={rc}"
                assert "[EXIT] Bye" in out, out[-400:]
                assert "Traceback" not in out.split("[EXIT] Bye")[0], out[-600:]
            finally:
                ap.proc.terminate()
                try:
                    ap.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ap.proc.kill()

            # 6b: SIGTERM — то же вежливое завершение (rc=0, [EXIT] Bye).
            ap = _spawn(env)
            try:
                time.sleep(4)
                assert ap.proc.poll() is None, "adapter упал при старте (SIGTERM)"
                ap.send_signal(signal.SIGTERM)
                rc = ap.wait(timeout=8)
                out = ap.output
                assert rc == 0, f"SIGTERM: rc={rc}"
                assert "[EXIT] Bye" in out, out[-400:]
            finally:
                ap.proc.terminate()
                try:
                    ap.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ap.proc.kill()

            # 6c: ПОВТОРНЫЙ сигнал во время завершения — os._exit(130) без
            # дампа стека: SIGINT и сразу второй (в процедуру finally).
            ap = _spawn(env)
            try:
                time.sleep(4)
                assert ap.proc.poll() is None, "adapter упал при старте (повторный)"
                ap.send_signal(signal.SIGINT)  # первый — вежливое завершение
                time.sleep(0.05)
                ap.send_signal(signal.SIGINT)  # второй — во время finally → 130
                rc = ap.wait(timeout=8)
                out = ap.output
                assert rc == 130, f"повторный SIGINT: rc={rc}"
                assert "Traceback" not in out, out[-600:]
            finally:
                ap.proc.terminate()
                try:
                    ap.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ap.proc.kill()
        finally:
            be.close()

    def test_console_trimmed_file_full(self, tmp_path):
        """Контракт v0.8.6-реформы на реальном процессе: при ENABLE=1 и малом
        ADAPTER_DEBUG_TRIM консоль показывает ОБРЕЗАННУЮ [BODY]-строку,
        session-*.log получает её ПОЛНОЙ."""
        logs_dir = str(tmp_path / "logs")
        os.makedirs(logs_dir)

        be = FakeBackend()
        be.models_response = {"object": "list",
                              "data": [{"id": "qwen-test", "object": "model"}]}
        be.serve()
        try:
            yaml_path = str(tmp_path / "adapter.yaml")
            with open(yaml_path, "w") as f:
                f.write("backend:\n"
                        "  - name: fake\n"
                        f"    base: {be.base_url}\n"
                        "    key: FAKE_KEY\n"
                        "    probe:\n"
                        "      - completions: qwen-test\n")
            proxy_port = _free_port()
            web_port = _free_port()
            exp_port = _free_port()
            env = _adapter_env(yaml_path, "", proxy_port, web_port, exp_port,
                               logs_dir, debug_enable="1", trim="60")
            ap = _spawn(env)
            try:
                time.sleep(4)
                assert ap.proc.poll() is None, "adapter упал при старте (TRIM=60)"
                assert _wait_http(web_port) == 200, "WEBUI не поднялся"

                # Тело ответа заметно длиннее лимита — контраст обрезки виден
                be.completions_response = {
                    "id": "x", "object": "chat.completion", "model": "qwen-test",
                    "choices": [{"index": 0,
                                 "message": {"role": "assistant",
                                             "content": "The quick brown fox "
                                                        "jumps over the lazy dog."},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 9,
                              "total_tokens": 12},
                }
                req_body = {"model": "qwen-test",
                            "messages": [{"role": "user", "content": "hi"}],
                            "max_tokens": 1}
                st, _ = _chat(proxy_port, req_body)
                assert st == 200, st
                time.sleep(2)
                out = ap.output

                # Консольная обрезка (TRIM=60) идёт по строке С ПРЕФИКСОМ
                # [req_id] (см. logger._write — msg приходит уже с префиксом):
                # начало [BODY]-тела запроса видно, хвост («"content": "hi"}»)
                # обрезан. Служебные print-блоки ([MODEL_USAGE]) не в счёт.
                assert '{"model": "qwen-test", "me' in out, out[-1000:]
                assert '"content": "hi"' not in out, \
                    "в консоли [BODY] не обрезан:\n" + out[-1000:]

                # Файл session-*.log несёт ПОЛНУЮ [BODY]-строку без обрезки.
                log_files = [f for f in sorted(os.listdir(logs_dir))
                             if f.startswith("session-") and f.endswith(".log")]
                assert log_files, os.listdir(logs_dir)
                content = open(os.path.join(logs_dir, log_files[0]),
                               encoding="utf-8").read()
                assert '"content": "hi"' in content, "в файле [BODY] обрезан"
            finally:
                ap.proc.terminate()
                try:
                    ap.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    ap.proc.kill()
        finally:
            be.close()
