"""Тесты корректного завершения по Ctrl-C/SIGTERM (backend_adapter.shutdown).

Модуль shutdown.py выносит логику graceful-завершения из backend-adapter.py
(главный цикл вызывает функции модуля — см. ADR «Корректное завершение по
Ctrl-C/SIGTERM»), поэтому контракт можно проверить тестами без спавна
реального процесса:

- install_signal_handlers(graceful=False) переключает SIGINT/SIGTERM на
  немедленный os._exit(130) — повторный Ctrl-C во время завершения не
  прерывает запись YAML (flush_table). Проверяется в суба-процессе: из
  обработчика нельзя вернуться, а os._exit убивает процесс — см.
  test_repeat_signal_exits_130;
- exit_code() интерпретирует пойманное исключение главного цикла:
  KeyboardInterrupt (Ctrl-C или SIGTERM-хэндлер) → 0, прочее → 1;
- graceful_finish() останавливает слушателей (каждого отдельно,
  suppress(BaseException)) и вызывает finish — финальный flush usage-таблицы;
  провал одного шага не мешает остальным;
- graceful_shutdown() = переключение сигналов + graceful_finish + закрытие
  httpd + код 0 (сам вызов install_signal_handlers(graceful=False) в main-
  потоке тестов НЕ выполняется: он поставил бы хэндлеры на os._exit —
  поэтому он мокается, а его вызов проверяется).

Сигналы ставить можно только в main-потоке; pytest гоняет тесты в main —
install_signal_handlers(graceful=False) внутри graceful_shutdown мокаем.
"""
import signal
import subprocess
import sys
from unittest import mock

import pytest


def _reload_all():
    """Выкинуть backend_adapter* из sys.modules (переимпорт свежих копий)."""
    to_remove = [n for n in list(sys.modules) if n.startswith("backend_adapter")]
    for n in to_remove:
        del sys.modules[n]


class FakeListener:
    """Заглушка HTTP-слушателя: shutdown/server_close помечаются, но можно
    заставить shutdown кидать — проверяем, что graceful_finish глотает."""

    def __init__(self):
        self.shutdown_called = 0
        self.server_close_called = 0

    def shutdown(self):
        self.shutdown_called += 1
        if getattr(self, "shutdown_raises", False):
            raise RuntimeError("shutdown boom")

    def server_close(self):
        self.server_close_called += 1


class TestExitCode:
    def test_keyboard_interrupt_is_zero(self):
        # Ctrl-C / SIGTERM-хэндлер (KeyboardInterrupt) — вежливое завершение,
        # код 0 (процесс не «упал», а штатно завершился по сигналу).
        _reload_all()
        from backend_adapter.shutdown import exit_code

        assert exit_code(KeyboardInterrupt()) == 0

    def test_other_exception_is_one(self):
        # Сбой главного цикла (не сигнал) — аварийный выход, код 1.
        _reload_all()
        from backend_adapter.shutdown import exit_code

        assert exit_code(RuntimeError("boom")) == 1


class TestInstallSignalHandlers:
    def test_non_main_thread_returns_silently(self):
        # В не-main-потоке signal.signal кидает ValueError — установка
        # пропускается молча (PEP 475 доставляет KI в main-поток).
        _reload_all()
        from backend_adapter import shutdown

        result = {}

        def _in_thread():
            result["ret"] = shutdown.install_signal_handlers(graceful=False)

        t = __import__("threading").Thread(target=_in_thread)
        t.start()
        t.join()
        assert result["ret"] is None

    def test_graceful_sets_sigterm_handler(self):
        # graceful=True: SIGTERM перехвачен на KeyboardInterrupt (вежливое
        # завершение как Ctrl-C); SIGINT остаётся дефолтным.
        _reload_all()
        from backend_adapter import shutdown

        shutdown.install_signal_handlers(graceful=True)
        try:
            assert signal.getsignal(signal.SIGTERM) is shutdown._graceful_signal
            assert signal.getsignal(signal.SIGINT) is signal.default_int_handler
        finally:
            # восстановить дефолты (не путать последующие тесты)
            signal.signal(signal.SIGINT, signal.default_int_handler)
            signal.signal(signal.SIGTERM, signal.default_int_handler)

    def test_repeat_signal_exits_130(self):
        # Повторный сигнал во время завершения = немедленный os._exit(130).
        # Проверяется в суба-процессе: из обработчика нельзя вернуться, а
        # os._exit убивает процесс. 130 = 128 + SIGINT(2).
        _reload_all()
        code = (
            "import signal\n"
            "from backend_adapter.shutdown import install_signal_handlers\n"
            "install_signal_handlers(graceful=False)\n"
            "import os\n"
            "os.kill(os.getpid(), signal.SIGINT)\n"
            "print('ALIVE')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 130
        assert "ALIVE" not in result.stdout


class TestGracefulFinish:
    def _setup(self):
        _reload_all()
        from backend_adapter import shutdown

        return shutdown

    def test_stops_listeners_and_calls_finish(self):
        # Вежливая остановка: shutdown+server_close у webui и exporter (при
        # exporter_enabled=True) и вызов finish (flush usage-таблицы) в конце.
        shutdown = self._setup()
        webui = FakeListener()
        exporter = FakeListener()
        calls = []

        def finish():
            calls.append("finish")

        shutdown.graceful_finish(webui, exporter, exporter_enabled=True, finish=finish)
        assert webui.shutdown_called == 1
        assert webui.server_close_called == 1
        assert exporter.shutdown_called == 1
        assert exporter.server_close_called == 1
        assert calls == ["finish"]

    def test_exporter_disabled_not_touched(self):
        # ADAPTER_EXPORTER_ENABLE=0 → exporter не останавливается (None или
        # заглушка остаётся нетронутой), webui и finish — как обычно.
        shutdown = self._setup()
        webui = FakeListener()
        exporter = FakeListener()
        calls = []

        def finish():
            calls.append("finish")

        shutdown.graceful_finish(webui, exporter, exporter_enabled=False, finish=finish)
        assert webui.shutdown_called == 1
        assert exporter.shutdown_called == 0
        assert exporter.server_close_called == 0
        assert calls == ["finish"]

    def test_error_in_webui_does_not_block_exporter_or_finish(self):
        # Ошибка остановки webui не роняет завершение: exporter и finish
        # выполняются (graceful_finish глотает BaseException по каждому шагу).
        shutdown = self._setup()
        webui = FakeListener()
        webui.shutdown_raises = True
        exporter = FakeListener()
        calls = []

        def finish():
            calls.append("finish")

        shutdown.graceful_finish(webui, exporter, exporter_enabled=True, finish=finish)
        assert exporter.shutdown_called == 1
        assert exporter.server_close_called == 1
        assert calls == ["finish"]


class TestGracefulShutdown:
    def test_full_procedure(self):
        # graceful_shutdown = переключение сигналов на немедленный выход +
        # вежливая остановка слушателей и finish + server_close главного
        # httpd + код 0. install_signal_handlers(graceful=False) мокается:
        # в main-потоке теста он поставил бы реальные хэндлеры на os._exit.
        _reload_all()
        from backend_adapter import shutdown

        webui = FakeListener()
        exporter = FakeListener()
        httpd = FakeListener()
        calls = []

        def finish():
            calls.append("finish")

        with mock.patch.object(
            shutdown, "install_signal_handlers", return_value=None
        ) as m_install:
            code = shutdown.graceful_shutdown(
                httpd, webui, exporter, exporter_enabled=True, finish=finish
            )
        m_install.assert_called_once_with(graceful=False)
        assert code == 0
        assert webui.shutdown_called == 1 and webui.server_close_called == 1
        assert exporter.shutdown_called == 1 and exporter.server_close_called == 1
        assert httpd.server_close_called == 1
        assert calls == ["finish"]

    def test_httpd_none_skips_server_close(self):
        # httpd может быть None (главный цикл не дошёл до serve_forever) —
        # процедура не падает.
        _reload_all()
        from backend_adapter import shutdown

        calls = []

        def finish():
            calls.append("finish")

        with mock.patch.object(shutdown, "install_signal_handlers", return_value=None):
            code = shutdown.graceful_shutdown(
                None, FakeListener(), None, exporter_enabled=False, finish=finish
            )
        assert code == 0
        assert calls == ["finish"]


@pytest.mark.skip(reason="документация контракта; ручная проверка — §3.9 плана")
class TestManualIntegration:
    """Ручной прогон завершения реального процесса адаптера (fake-бэкенд,
    SIGINT×2 / SIGTERM → rc 0/130/0 без traceback) — см. tmp/check_v086.py."""
