"""Process detachment and pidfile utilities.

Stdlib only — no internal dependencies.
"""

import contextlib
import os
import sys
import time


def _detach() -> None:
    """Отпустить процесс от консоли: двойной fork + отключение stdio."""
    try:
        pid = os.fork()
        if pid > 0:
            # Родитель: выходим сразу
            time.sleep(0.5)
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"[FORK] Error: [{e.errno}] {e.strerror}\n")
        sys.exit(1)

    # Первый дочерний: создаём новую сессию (отвязываемся от терминала)
    os.setsid()

    try:
        pid = os.fork()
        if pid > 0:
            # Первый потомок завершается
            sys.exit(0)
    except OSError as e:
        sys.stderr.write(f"[FORK] Error: [{e.errno}] {e.strerror}\n")
        sys.exit(1)

    # Второй потомок: перенаправляем stdio в/dev/null
    sys.stdout.flush()
    sys.stderr.flush()

    with open(os.devnull) as fin:
        os.dup2(fin.fileno(), sys.stdin.fileno())
    with open(os.devnull, "w") as fout:
        os.dup2(fout.fileno(), sys.stdout.fileno())
        os.dup2(fout.fileno(), sys.stderr.fileno())


def _pidfile_path() -> str:
    """Путь PID-файла: ADAPTER_DATA_ROOT/var + basename(ADAPTER_PIDFILE).

    PID-файл живёт в папке состояния адаптера (``ADAPTER_DATA_ROOT/var`` —
    рядом с model-usage.yaml, state.yaml и ``<бэкенд>.models.json``), а не в
    произвольном месте. ``ADAPTER_PIDFILE`` задаёт ИМЯ файла (или под-путь):
    используется basename — абсолютный путь вне корня данных игнорируется,
    файл всё равно кладётся в ``var/``. Пусто/не задано → ``adapter.pid``.

    Единая формула для записи (_write_pidfile) и удаления
    (_remove_pidfile) — путь не должен разъезжаться между ними. Модуль
    остаётся stdlib-only: корень читается из os.environ напрямую (та же
    формула, что у config.ADAPTER_DATA_ROOT / config.var_dir)."""
    data_root = os.environ.get("ADAPTER_DATA_ROOT", "").strip() or "./tmp/adapter"
    name = os.environ.get("ADAPTER_PIDFILE", "").strip()
    if not name:
        name = "adapter.pid"
    return os.path.join(data_root, "var", os.path.basename(name))


def _write_pidfile() -> str:
    """Записать PID процесса в PID-файл, вернуть его путь.

    Пишется при ЛЮБОМ запуске адаптера (не только в detach-режиме) — файл
    нужен, чтобы манипулировать процессом без консоли (контейнер, служба).
    Директория создаётся при необходимости (вызов может произойти до
    создания корня WEBUI). Существующий файл перезаписывается своим PID:
    stale-PID не проверяется. Возвращаемый путь — для консольного
    ``[PID]``-сообщения оператору."""
    pidfile = _pidfile_path()
    os.makedirs(os.path.dirname(pidfile), exist_ok=True)
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
    return pidfile


def _remove_pidfile() -> None:
    """Удалить PID-файл при завершении процесса — только если он наш.

    Идемпотентна и не бросает: отсутствие файла или ошибка удаления — не
    ошибка (процедура завершения не должна падать). Файл удаляется, лишь
    когда содержит ТЕКУЩИЙ PID: если нас успел перезаписать более поздний
    запуск с тем же корнем данных, чужой PID-файл не трогаем. При повторном
    сигнале (os._exit(130)) функция не вызывается — atexit не срабатывает,
    и файл остаётся; это не штатный выход."""
    pidfile = _pidfile_path()
    try:
        with open(pidfile) as f:
            content = f.read().strip()
    except OSError:
        return
    if content != str(os.getpid()):
        return
    with contextlib.suppress(OSError):
        os.unlink(pidfile)
