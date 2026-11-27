"""Лог сервиса (stdout/stderr под launchd): путь, SIGHUP, ротация.

Под launchd лог агента задаётся в plist как ``StandardOutPath``/
``StandardErrorPath`` и живёт **вне** дома
(``~/Library/Logs/backend-adapter/adapter.log``): ``<root>/log`` принадлежит
работающему адаптеру, и смешивать два источника в одном каталоге данных
нельзя. launchd открывает файл один раз при старте процесса и сам его не
ротирует — файл растёт без ограничений (измерено: ~21 МБ за сутки).

Модуль решает две задачи:

**Переоткрытие по SIGHUP** (``install_hup_handler``). Ротация внешним
инструментом (newsyslog, ADR-041) переименовывает файл и шлёт процессу
``SIGHUP``; обработчик заново открывает путь и подменяет дескрипторы 1/2
(``dup2``), поэтому новые строки идут в свежий файл, а архив остаётся целым.
Обработчик ставится **только под launchd** (``XPC_SERVICE_NAME`` равен метке
агента — launchd экспортирует её сама): в терминале ``SIGHUP`` сохраняет
прежний смысл — завершение процесса. Путь берётся из самого дескриптора
(``fcntl.F_GETPATH``), а не из константы: если plist направил вывод в другой
файл, переоткроется именно он. На не-macOS (``F_GETPATH`` нет) — тихий no-op.

**Очистка лога** (``clear_log``). ``os.truncate`` здесь **недопустим**:
launchd открывает файл без ``O_APPEND``, и писатель со своим смещением
оставляет после обрезания NUL-дырку (замерено: 160–490 байт). Поэтому файл
удаляется, а работающий агент перезапускается — иначе fd живого процесса
держал бы удалённый inode и место не освобождалось бы.

Модуль — лист DAG (stdlib + ``templates``): путь лога и метка агента берутся
из ``templates``, чтобы код и шаблоны plist не разъехались.
"""

from __future__ import annotations

import contextlib
import os
import signal
import sys

from . import templates

# Обработчик уже поставлен: повторная установка (двойной вызов из тестов или
# будущих точек входа) не должна перезаписывать прежний путь.
_HUP_INSTALLED = False


def service_log_path() -> str:
    """Файл, куда launchd пишет stdout/stderr агента.

    ``~/Library/Logs/backend-adapter/adapter.log`` — тот же каталог, что у
    ``install.sh``, и **не** ``<root>/log``: там логи работающего адаптера
    (``session-*.log``, ``.err``, ``*.parts``). launchd без
    ``StandardOutPath``/``StandardErrorPath`` отправляет вывод в ``/dev/null``,
    поэтому plist всегда задаёт этот файл, а ``--launchd logs`` его читает.
    """
    return os.path.expanduser(os.path.join("~/Library/Logs", "backend-adapter", "adapter.log"))


def is_launchd_service() -> bool:
    """Процесс запущен launchd-агентом адаптера (а не из терминала)?"""
    return os.environ.get("XPC_SERVICE_NAME") == templates.LAUNCHD_LABEL


def fd_log_path(fd: int) -> str | None:
    """Путь, в который открыт ``fd`` (macOS ``F_GETPATH``); иначе ``None``.

    Буфер — 1024 байта: ``F_GETPATH`` копирует в него путь как C-строку, и
    больший буфер на macOS отвергается (``ValueError: fcntl argument 3 is too
    long``). Не-файловые дескрипторы (терминал, pipe) и платформы без
    ``F_GETPATH`` дают ``None`` — обработчик тогда не ставится.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover — Windows
        return None
    getpath = getattr(fcntl, "F_GETPATH", None)
    if getpath is None:
        return None
    try:
        raw = fcntl.fcntl(fd, getpath, b"\0" * 1024)
    except (OSError, ValueError):
        return None
    if not isinstance(raw, bytes):
        return None
    path = raw.split(b"\0", 1)[0].decode("utf-8", "replace")
    return path or None


def install_hup_handler() -> bool:
    """Поставить SIGHUP-переоткрытие лога; вернуть ``True``, если поставлено.

    Условия (оба обязательны, иначе тихий ``False``):

    * процесс под надзором launchd-агента (``is_launchd_service``) — иначе
      ``SIGHUP`` обязан сохранять обычный смысл завершения (ADR-026);
    * stdout — регулярный файл с известным путём (``fd_log_path``).

    Вызывается из ``backend-adapter.py`` перед главным циклом. Вне
    главного потока ``signal.signal`` бросает ``ValueError`` — тогда тоже
    ``False``: PEP 475 доставит сигнал в главный поток, но обработчик там не
    поставить.
    """
    global _HUP_INSTALLED
    if _HUP_INSTALLED:
        return True
    if not is_launchd_service():
        return False
    path = fd_log_path(sys.stdout.fileno() if hasattr(sys.stdout, "fileno") else 1)
    if path is None:
        return False
    try:
        signal.signal(signal.SIGHUP, _hup_handler(path))
    except (ValueError, OSError):
        return False
    _HUP_INSTALLED = True
    print(f"[SERVICE] SIGHUP переоткрывает лог: {path}")
    return True


def _hup_handler(path: str):
    """Собрать обработчик SIGHUP для конкретного пути лога."""

    def _handle(_signum: int, _frame: object) -> None:
        _reopen(path)

    return _handle


def _reopen(path: str, *, out_fd: int = 1, err_fd: int = 2) -> None:
    """Переоткрыть ``path`` и подменить ``out_fd``/``err_fd`` (``dup2``).

    Вызывается из обработчика сигнала, поэтому не бросает: сбой открытия —
    строка ``[WARN]`` в старый поток и выход (процесс продолжает работать,
    просто пишет в прежний, возможно уже переименованный, файл). Буферы
    stdout/stderr сбрасываются **до** подмены, чтобы хвост не ушёл в архив.
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
    try:
        new_fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    except OSError as e:
        _write_fd(err_fd, f"[WARN] Не удалось переоткрыть лог {path}: {e}\n")
        return
    try:
        os.dup2(new_fd, out_fd)
        os.dup2(new_fd, err_fd)
    finally:
        if new_fd not in (out_fd, err_fd):
            os.close(new_fd)
    _write_fd(out_fd, f"[SERVICE] Лог переоткрыт после SIGHUP: {path}\n")


def _write_fd(fd: int, text: str) -> None:
    """Записать строку прямо в дескриптор, минуя буферы Python."""
    with contextlib.suppress(OSError):
        os.write(fd, text.encode("utf-8", "replace"))


def clear_log(path: str) -> bool:
    """Удалить лог сервиса; ``True`` — файл был и удалён.

    Обнуление исключено: fd launchd открыт без ``O_APPEND``, и после
    ``truncate`` писатель оставляет NUL-дырку (см. docstring модуля).
    Отсутствие файла — ``False`` (не ошибка). Ошибку удаления пробрасывает:
    вызывающий решает, как о ней сообщить.
    """
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True
