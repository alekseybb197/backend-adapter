"""Process detachment and pidfile utilities.

Stdlib only — no internal dependencies.
"""

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


def _write_pidfile() -> None:
    """Записать PID процесса в файл pid внутри ADAPTER_DEBUG_LOGPATH.

    PID-файл живёт в общей директории логов/артефактов адаптера
    (ADAPTER_DEBUG_LOGPATH — единый корень WEBUI, session-логов, *.parts
    и model-usage.yaml), а не в произвольном месте. ``ADAPTER_PIDFILE``
    задаёт ИМЯ файла (или под-путь): используется basename — абсолютный
    путь вне LOGPATH игнорируется, файл всё равно кладётся в LOGPATH.
    Пусто/не задано → ``adapter.pid``. Директория создаётся при
    необходимости (вызов может произойти до создания корня WEBUI).

    Модуль остаётся stdlib-only: LOGPATH читается из os.environ напрямую
    (та же формула, что у config.ADAPTER_DEBUG_LOGPATH)."""
    logpath = os.environ.get("ADAPTER_DEBUG_LOGPATH", "").strip() or "./tmp/logs"
    name = os.environ.get("ADAPTER_PIDFILE", "").strip()
    if not name:
        name = "adapter.pid"
    pidfile = os.path.join(logpath, os.path.basename(name))
    os.makedirs(logpath, exist_ok=True)
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
