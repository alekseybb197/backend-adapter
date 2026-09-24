"""Фоновая дебаунсная отрисовка деревьев артефактов (v0.9.10).

Лист DAG: stdlib + ``config``. ``artifact_tree`` импортируется ВНУТРИ
функций (паттерн ``session_log.logging_enabled`` → ``session_settings``):
``artifact_tree`` тянет за собой весь граф рендереров (plantuml/graphviz,
turns/registry), а ``session_log`` — база DAG — не должен платить эту цену
на импорте. Так ``session_log`` может звать ``notify`` без цикла.

Зачем модуль. Сборка артефактов (`artifact_tree.generate`) — дорогая:
полный цикл разбирает part-файлы и зовёт внешние рендереры (plantuml/java,
dot) с 60-секундным таймаутом каждый. Выполнять её синхронно в запросе
прокси нельзя: ответ клиенту ждал бы рендер. Поэтому запись части лишь
ДЁШЕВО помечает ``parts_dir`` как «грязный» (``notify``), а отдельный
daemon-поток раз в секунду проверяет очередь и перерисовывает директорию,
когда после последней записи части прошёл тихий период
``config.ADAPTER_ARTIFACT_DEBOUNCE`` секунд (дебаунс — серия частей одного
хода даёт одну перерисовку, а не N).

Канал наблюдательный: любая ошибка генерации логируется WARNING и не
роняет ни поток, ни запись части (``session_log.write_debug_json`` оборачивает
``notify`` в ``contextlib.suppress``). Ленивый путь ``/session``
(``session_viewer.find_or_generate_sessions``) остаётся страховкой: адаптер
перезапустился, воркер ещё ничего не видел, а обзор уже открывают — и
штатным путём для standalone-CLI ``python -m backend_adapter.artifact_tree``.
"""

import logging
import os
import threading
import time

from . import config

logger = logging.getLogger("artifact_refresh")

__all__ = ["notify", "reset", "flush"]

# Период опроса очереди. Меньше секунды смысла нет: дебаунс измеряется
# секундами, а лишние пробуждения ничего не ускоряют.
_POLL_SECONDS = 1.0

_LOCK = threading.Lock()
# Директории, ожидающие перерисовки, и время последней записи в каждую
# (time.monotonic — часы, не зависящие от перевода системного времени).
_PENDING: set[str] = set()
_LAST_WRITE: dict[str, float] = {}
# Директории, генерация которых идёт прямо сейчас (для flush).
_INFLIGHT: set[str] = set()

_THREAD: threading.Thread | None = None
_STOP = threading.Event()


def notify(parts_dir: str) -> None:
    """Пометить ``parts_dir`` как изменённую (дёшево, без блокировок рендера).

    Запоминает время записи и лениво поднимает единственный на процесс
    daemon-поток ``_worker_loop``. Вызывается из
    ``session_log.write_debug_json`` на каждый успешно записанный part-файл —
    потому НЕ должна ни блокироваться на генерации, ни бросать исключений.
    """
    if not parts_dir:
        return
    global _THREAD
    with _LOCK:
        _PENDING.add(parts_dir)
        _LAST_WRITE[parts_dir] = time.monotonic()
        if _THREAD is None or not _THREAD.is_alive():
            _THREAD = threading.Thread(
                target=_worker_loop,
                args=(_STOP,),
                name="artifact-refresh",
                daemon=True,
            )
            _THREAD.start()


def _due_dirs() -> list[str]:
    """Снять с очереди директории, у которых истёк тихий период дебаунса.

    Дебаунс читается ЖИВЬЁМ (``config.ADAPTER_ARTIFACT_DEBOUNCE``) — тесты
    подменяют значение без перезагрузки модуля.
    """
    now = time.monotonic()
    debounce = config.ADAPTER_ARTIFACT_DEBOUNCE
    with _LOCK:
        due = [d for d in _PENDING if now - _LAST_WRITE.get(d, now) >= debounce]
        for d in due:
            _PENDING.discard(d)
    return due


def _run_one(parts_dir: str) -> None:
    """Перерисовать одну директорию. Исключений не бросает.

    ``artifact_tree`` импортируется здесь, а не на уровне модуля, — чтобы
    ``session_log`` (база DAG) не тянул граф рендереров через
    ``artifact_refresh`` (см. докстринг модуля). Сбой генерации (в т.ч.
    битый/недописанный part-файл: ``artifact_tree_parse.load_json`` зовёт
    ``json.load`` без защиты) логируется и глотается — канал наблюдательный.
    """
    tag = os.path.basename(os.path.normpath(parts_dir))
    with _LOCK:
        _INFLIGHT.add(parts_dir)
    try:
        from . import artifact_tree

        artifact_tree.generate(parts_dir, verbose=False)
    except Exception as e:  # noqa: BLE001 — воркер не должен ронять поток
        logger.warning(f"[{tag}] фоновая отрисовка артефактов не удалась: {e}")
    finally:
        with _LOCK:
            _INFLIGHT.discard(parts_dir)


def _worker_loop(stop: threading.Event) -> None:
    """Тело фонового потока: раз в секунду перерисовывает «созревшие» директории.

    Записи, пришедшие во время генерации, остаются в ``_PENDING``
    (``notify`` добавит директорию заново после того, как она снята с
    очереди) — поэтому изменения не теряются: цикл просто увидит её на
    следующей итерации после нового тихого периода.
    """
    while not stop.wait(_POLL_SECONDS):
        try:
            due = _due_dirs()
        except Exception as e:  # noqa: BLE001 — планировщик не должен умереть
            logger.warning(f"artifact_refresh: сбой планировщика: {e}")
            continue
        for parts_dir in due:
            _run_one(parts_dir)


def reset() -> None:
    """Остановить воркер и очистить очередь (для тестов).

    Старый поток получает свой ``stop``-event и завершается сам; новый
    ``notify`` поднимет свежий поток с новым event — уже очищенное
    состояние не «протекает» между тестами.
    """
    global _THREAD, _STOP
    with _LOCK:
        old_thread = _THREAD
        old_stop = _STOP
        _THREAD = None
        _STOP = threading.Event()
        _PENDING.clear()
        _LAST_WRITE.clear()
        _INFLIGHT.clear()
    old_stop.set()
    if old_thread is not None:
        old_thread.join(timeout=2.0)


def flush(timeout: float | None = None) -> bool:
    """Дождаться, пока очередь опустеет и текущие генерации завершатся.

    ``timeout=None`` — ждать без ограничения (используется на завершении
    процесса, если понадобится). Возвращает False, если время вышло.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        with _LOCK:
            busy = bool(_PENDING or _INFLIGHT)
        if not busy:
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
