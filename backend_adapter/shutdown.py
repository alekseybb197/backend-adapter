"""Корректное завершение по Ctrl-C/SIGTERM: процедура и хэндлеры.

Логика завершения процесса адаптера (backend-adapter.py) вынесена в
отдельный модуль, чтобы её можно было покрыть тестами: процедура
вежливого останова слушателей/фоновых воркеров и сохранения usage-хвоста,
правила переключения сигналов на немедленный выход во время завершения,
интерпретация кода возврата после обработки сигнала.

Контракт завершения (зафиксирован в v0.8.6, см. ADR «Корректное
завершение по Ctrl-C/SIGTERM» в CLAUDE.md):

- Ctrl-C (SIGINT) и SIGTERM завершают процесс ВЕЖЛИВО: останавливаются
  слушатели (shutdown() дожидается активных обработчиков), останавливается
  фоновая проверка бэкендов, таблица использованных моделей сохраняется
  (flush_table), печатается «[EXIT] Bye», код возврата 0;
- повторный сигнал во время завершения — НЕМЕДЛЕННЫЙ выход os._exit(130):
  запись YAML (flush_table) нельзя прерывать на середине — файл останется
  с .tmp-хвостом; 130 = 128 + SIGINT (2), как у прерванного процесса;
- провал процедуры завершения НЕ маскирует успех: если всё вежливое
  завершение прошло (процесс сам не был прерван повторным сигналом),
  код возврата — 0 (SIGINT обработан), а не 130/1;
- одиночный SIGTERM без SIGINT — тоже вежливое завершение с кодом 0.

См. также _handle_signal/_exit_code в daemon.py (PID-файл) — общее у
модулей только имя; shutdown.py стоит в DAG после config/model_usage
(импортирует их локально в _graceful_finish).
"""

from __future__ import annotations

import signal
import sys
import threading
from collections.abc import Callable


def install_signal_handlers(graceful: bool = True) -> None:
    """Поставить обработчики завершения (только в main-потоке).

    ``graceful=True``: SIGINT оставлен дефолтным (KeyboardInterrupt — его
    ловит главный цикл serve_forever), SIGTERM перехвачен на вежливое
    завершение (raise KeyboardInterrupt: без этого SIGTERM убивает процесс
    мгновенно, без finally — usage-хвост не сохранится). ``graceful=False``:
    повторный сигнал во время процедуры завершения — немедленный выход
    os._exit(130) (запись YAML прерывать нельзя). В не-main-потоках
    signal.signal кидает ValueError — молча пропускаем (PEP 475 доставляет
    KeyboardInterrupt в main-поток)."""
    if threading.current_thread() is not threading.main_thread():
        return

    def _force_exit(_signum: int, _frame: object) -> None:
        os_exit(130)

    if graceful:
        signal.signal(signal.SIGTERM, _graceful_signal)
    else:
        signal.signal(signal.SIGINT, _force_exit)
        signal.signal(signal.SIGTERM, _force_exit)


def _graceful_signal(_signum: int, _frame: object) -> None:
    """SIGTERM → KeyboardInterrupt: как Ctrl-C (PEP 475 перезапускает
    системный вызов serve_forever, KI прилетает в main-поток)."""
    raise KeyboardInterrupt


def _dummy_finish() -> None:
    """Пустой коллбек завершения для тестов graceful_shutdown/graceful_finish
    без реальных модулей (config/model_usage)."""


def os_exit(code: int) -> None:
    """os._exit с явной аннотацией NoReturn для mypy-strict. os._exit
    минует обработчики/буферы Python — процесс умирает сразу."""
    import os

    os._exit(code)


def exit_code(exc: BaseException) -> int:
    """Код возврата процесса для пойманного исключения завершения.

    KeyboardInterrupt (Ctrl-C или SIGTERM-хэндлер) обработан — процедура
    завершения отработала штатно, код 0 (процесс не «упал», а вежливо
    завершился по сигналу). Любое ДРУГОЕ исключение (сбой в главном цикле)
    — код 1 (аварийный выход, как у неперехваченного исключения)."""
    if isinstance(exc, KeyboardInterrupt):
        return 0
    return 1


def graceful_finish(webui, exporter, exporter_enabled: bool, finish: Callable[[], None]) -> None:
    """Вежливая остановка фоновых слушателей и фоновой проверки.

    Вызывается из finally главного цикла (после переключения сигналов на
    немедленный выход — процедуру прервать нельзя). Ошибки не пробрасывает
    (завершение не должно падать): каждую остановку пробуем отдельно и
    продолжаем к следующему шагу, финальный flush usage-таблицы выполняется
    всегда. ``finish`` — коллбек, который выполняет остановку фоновой
    проверки и финальный flush таблицы использованных моделей (в
    backend-adapter.py: _cfg.stop_refresh + _model_usage.flush_table):
    вынесен параметром, чтобы тесты могли подменить реальные модули."""
    import contextlib

    with contextlib.suppress(BaseException):
        webui.shutdown()
        webui.server_close()
    if exporter_enabled and exporter is not None:
        with contextlib.suppress(BaseException):
            exporter.shutdown()
            exporter.server_close()
    finish()


def handle_main_loop_exception(exc: BaseException) -> int:
    """Обработка исключения главного цикла: лог и код возврата.

    KeyboardInterrupt — вежливое завершение, код 0 (см. exit_code).
    Прочее исключение печатаем в stderr (traceback, как у
    неперехваченного) и возвращаем 1."""
    code = exit_code(exc)
    if code != 0:
        print(f"[FATAL] Главный цикл упал: {exc!r}", file=sys.stderr)
    return code


# Функция ниже — точка входа процедуры graceful-завершения (main-цикл
# вызывает её из finally). Держим её поверх остальных определений модуля.
def graceful_shutdown(
    httpd, webui, exporter, exporter_enabled: bool, finish: Callable[[], None]
) -> int:
    """Полная процедура вежливого завершения главного цикла.

    Порядок: (1) сигналы переключаются на немедленный выход (повторный
    Ctrl-C/SIGTERM во время процедуры = os._exit(130)); (2) вежливая
    остановка слушателей/воркера и финальный flush usage-таблицы
    (graceful_finish, ошибки не пробрасываются); (3) возврат кода — 0.

    Вызывается из finally-блока main после обработки KeyboardInterrupt
    (код возврата из except-ветки берёт exit_code)."""
    install_signal_handlers(graceful=False)
    graceful_finish(webui, exporter, exporter_enabled, finish)
    if httpd is not None:
        import contextlib

        with contextlib.suppress(BaseException):
            httpd.server_close()
    return 0


# Хэндлеры сигналов ставятся в main-потоке при запуске адаптера
# (backend-adapter.py): install_signal_handlers(graceful=True) до входа в
# главный цикл serve_forever. Повторный вызов с graceful=False — в finally
# (процедура завершения).
