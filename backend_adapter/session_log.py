"""Session-scoped log file management.

Manages per-session debug/trace file handles with FIFO eviction.
Reads ADAPTER_DEBUG_LOGPATH from env at import time.
"""

import contextlib
import json
import os
import re
import threading
import time
from collections import OrderedDict

import yaml
from yaml.emitter import Emitter

# ==================== LiteralDumper — YAML dumper ====================
# Port from body-dump.py: uses block scalar (|) for multiline strings,
# inline scalar for single-line strings. Patch fixes space_break detection
# (newline + whitespace) that normally forces allow_block=False in PyYAML.


class LiteralDumper(yaml.Dumper):
    """YAML dumper that uses block scalar (|) for multiline strings."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        _orig_choose = Emitter.choose_scalar_style
        self._LiteralDumper__orig_choose = _orig_choose  # noqa: SLF001

        def _fixed_choose(self):
            if self.analysis is None:
                self.analysis = self.analyze_scalar(self.event.value)
            if "\n" in self.event.value:
                self.analysis.allow_block = True
                self.analysis.allow_block_plain = True
            return _orig_choose(self)

        if not hasattr(Emitter, "_LiteralDumper__orig_choose"):
            Emitter._LiteralDumper__orig_choose = _orig_choose  # noqa: SLF001
        Emitter.choose_scalar_style = _fixed_choose

    def __del__(self):
        if hasattr(Emitter, "_LiteralDumper__orig_choose"):
            Emitter.choose_scalar_style = Emitter._LiteralDumper__orig_choose  # noqa: SLF001
            delattr(Emitter, "_LiteralDumper__orig_choose")  # noqa: SLF001


def _yaml_str_representer(dumper, data):
    """YAML representer for strings with three styles:

    * Block scalar (``|``) — strings with *real* newlines (actual ``\\n``
      U+000A). The ``LiteralDumper`` fix ensures this works even with
      space_break — see ``__init__`` patch on ``choose_scalar_style``.
    * Double-quoted (`"..."`) — strings with *literal* backslash sequences
      (e.g. ``\\n``, ``\\t``) or special YAML characters (``:``, ``{``, ``}``,
      etc.). Escaping is handled by PyYAML's emitter, not pre-computed.
    * Plain — clean single-line strings without special chars.
    """
    # 1) Real newlines (U+000A) → block scalar
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    # 2) Literal backslash (\\) sequences → double-quoted; let PyYAML emitter
    #    handle escaping (not json.dumps — that would double-escape).
    if "\\" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    # 3) Special YAML characters (colons, braces, etc.) → double-quoted;
    #    same: let PyYAML emitter handle escaping.
    if any(ch in data for ch in ":{}[]#&*?|-<>=%@!\"'"):
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
    # 4) Clean single-line → plain scalar
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


LiteralDumper.add_representer(str, _yaml_str_representer)


def dump_yaml(payload) -> str:
    """Serialize *payload* to YAML using LiteralDumper (block scalars for
    multiline strings, inline for single-line). Returns a string."""
    import io

    s = io.StringIO()
    yaml.dump(
        payload,
        stream=s,
        Dumper=LiteralDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    return s.getvalue()


_LOG_FILES_PER_SESSION = 5000
_session_logs: OrderedDict[str, dict] = OrderedDict()  # session_id -> {abspath: file_handle}
_session_file_ts: dict[str, str] = {}  # session_id -> stable timestamp (first-use)
_last_log_session_id: str = ""  # глобальный fallback для _d()

# Глобальный кэш путей к .parts директориям для разных сессий.
# Ключ: session_id или f"{session_id}_jsonparts" (для BODY_TAGS)
# Значение: путь к директории на диске.
_parts_dir: dict[str, str] = {}
_parts_dir_ts: dict[str, str] = {}  # session_id → stable timestamp (first-use)

# ==================== ADAPTER_DEBUG_TAGS_OUT ====================
# Глобальный счётчик для JSON-файлов тегированных блоков.
# Формат имени: <session[:8]>-<seq:04d>-<tag_lower>.json
_debug_json_seq = 0
_debug_json_lock = threading.Lock()


def _resolve_log_base():
    """Прочитать env ADAPTER_DEBUG_LOGPATH и вернуть (debug_is_dir,
    debug_path, trace_is_dir, trace_path).

    Путь всегда трактуется как ДИРЕКТОРИЯ логов сессий (debug-логи, trace,
    *.parts дампы и корень веб-интерфейса — всё в одной папке). Режим
    «один файл» удалён.

    Zero-config: если env-переменная пуста / не задана — файловая запись
    полностью ВЫКЛЮЧЕНА (все четыре значения falsy), дефолтного пути больше
    нет, ничего на диск не пишется и папка не создаётся ни на импорте, ни
    на старте. Консольные debug-блоки при этом видны (ADAPTER_DEBUG_ENABLE=1).

    ``is_dir``-флаги становятся True, когда env-путь задан, — независимо от
    того, существует ли папка на диске: отсутствие лечится os.makedirs(
    exist_ok=True) при записи (см. _open_session_file / _body_tags_parts_dir).
    """
    p = os.environ.get("ADAPTER_DEBUG_LOGPATH", "").strip()
    if not p:
        return (False, "", False, "")
    return (True, p, True, p)


_DEBUG_IS_DIR, _DEBUG_PATH, _TRACE_IS_DIR, _TRACE_PATH = _resolve_log_base()


def _make_session_file(base_path: str, session_id: str, ext: str):
    """Возвращает путь к файлу сессии (вызывается только при is_dir=True).
    Формат имени: session-<YYYYMMDD-HHMMSS>-<sessionID>.<ext>.
    Timestamp фиксируется при первом обращении к сессии и переиспользуется,
    чтобы весь трафик сессии шёл в один файл."""
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", session_id[:8])
    if session_id not in _session_file_ts:
        _session_file_ts[session_id] = time.strftime("%Y%m%d-%H%M%S")
    ts = _session_file_ts[session_id]
    return os.path.join(base_path, f"session-{ts}-{safe}.{ext}")


def _open_session_file(kind: str, session_id: str):
    """Открыть дескриптор для session_id (создать или вернуть существующий).
    Returns open file handle (binary, "ab") или None, если лог-путь не задан.

    Директория логов создаётся при необходимости (lazy): путь может быть
    задан, но ещё не существовать на диске — os.makedirs(exist_ok=True)
    до открытия файла сессии."""
    if kind == "debug":
        is_dir, path = _DEBUG_IS_DIR, _DEBUG_PATH
        ext = "log"
    else:
        is_dir, path = _TRACE_IS_DIR, _TRACE_PATH
        ext = "jsonl"
    if not is_dir or not path:
        return None
    os.makedirs(path, exist_ok=True)
    target = _make_session_file(path, session_id, ext)
    logs = _session_logs.setdefault(session_id, {})
    if target in logs:
        return logs[target]
    # Превысили лимит? Вытесняем самые старые сессии.
    while len(logs) > _LOG_FILES_PER_SESSION:
        _session_logs.popitem(last=False)
    # Дескриптор ДОЛЖЕН пережить этот вызов (пишем в него из других потоков
    # до закрытия сессии), поэтому открываем без контекстного менеджера.
    fd = open(target, "ab")  # noqa: SIM115 — живой хендл хранится в logs[]
    logs[target] = fd
    return fd


def _close_session_file(session_id: str) -> None:
    """Закрыть все открытые дескрипторы сессии (для чистоты при завершении)."""
    logs = _session_logs.pop(session_id, None)
    if logs:
        for f in logs.values():
            with contextlib.suppress(Exception):
                f.close()


# ==================== ADAPTER_DEBUG_TAGS_OUT ====================


def _body_tags_parts_dir(session_id: str) -> str | None:
    """Возвращает путь к session-<YYYYMMDD-HHMMSS>-<session_id[:8]>.parts директории
    для сессии. Создаёт при первом вызове. Возвращает None, если per-session режим
    логов не включён.

    Формат директории: session-<YYYYMMDD-HHMMSS>-<sessionID[:8]>.parts
    Формат файлов: session-<YYYYMMDD-HHMMSS>-<sessionID[:8]>-NNNN-tagname.json
    """
    if not _DEBUG_IS_DIR or not _DEBUG_PATH:
        return None
    # Кэш по session_id
    tag_key = f"{session_id}_jsonparts"
    if tag_key not in _parts_dir:
        safe8 = session_id[:8]
        if session_id not in _parts_dir_ts:
            _parts_dir_ts[session_id] = time.strftime("%Y%m%d-%H%M%S")
        ts = _parts_dir_ts[session_id]
        # Lazy: базовая директория логов может ещё не существовать —
        # создаём и её, и вложенную .parts.
        os.makedirs(_DEBUG_PATH, exist_ok=True)
        tags_path = os.path.join(_DEBUG_PATH, f"session-{ts}-{safe8}.parts")
        os.makedirs(tags_path, exist_ok=True)
        _parts_dir[tag_key] = tags_path
    return _parts_dir[tag_key]


def write_debug_json(session_id: str, tag: str, data: dict | str) -> None:
    """Записать часть протокола обмена в JSON-файл.

    Формат имени: ``<session_id[:8]>-NNNN-<tag_lower>.json``

    ``data`` — dict, str, list или bytearray/bytes. При bytes/bytearray
    декодируется как UTF-8.

    Файл пишется только если включён мастер-выключатель
    ``config.ADAPTER_DEBUG`` (ADAPTER_DEBUG_ENABLE=1) и флаг
    ``config.ADAPTER_DEBUG_TAGS_OUT``, и задан лог-путь (``_DEBUG_IS_DIR``
    — т.е. ADAPTER_DEBUG_LOGPATH задаёт директорию логов сессий; папка
    создаётся при необходимости). Для каждого тега пишутся парные файлы —
    ``.json`` и ``.yaml``.
    """
    # Быстрая проверка — мастер-выключатель / флаг выключен или лог-путь
    # не директория. Локальный импорт читает config на момент вызова, чтобы
    # тесты могли переключать флаги без перезагрузки модуля.
    from .config import ADAPTER_DEBUG, ADAPTER_DEBUG_TAGS_OUT

    if not ADAPTER_DEBUG:
        return
    if not ADAPTER_DEBUG_TAGS_OUT:
        return
    if not _DEBUG_IS_DIR or not _DEBUG_PATH:
        return

    parts_path = _body_tags_parts_dir(session_id)
    if parts_path is None:
        return

    # Глобальный атомарный инкремент
    with _debug_json_lock:
        global _debug_json_seq
        _debug_json_seq += 1
        seq = _debug_json_seq
    tag_lower = tag.lower()
    json_name = f"{session_id[:8]}-{seq:04d}-{tag_lower}.json"
    json_path = os.path.join(parts_path, json_name)

    # Нормализация данных: bytes/bytearray → str, dict/list → pretty json,
    # строка → пытаемся распарсить как JSON и записать в pretty form.
    if isinstance(data, (bytes, bytearray)):
        body_str = data.decode("utf-8", errors="replace")
    elif isinstance(data, dict):
        body_str = json.dumps(data, ensure_ascii=False, indent=2)
    else:
        raw = str(data)
        try:
            body_str = json.dumps(json.loads(raw), ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, ValueError):
            body_str = raw

    with open(json_path, "w", encoding="utf-8") as f:
        f.write(body_str)

    # === YAML dump alongside JSON ===
    # К каждому .json пишем одноимённый .yaml парой. Для YAML нужно "сырое"
    # значение (не json.dumps-строка, а исходный object), чтобы dumper мог
    # правильно обработать типы.
    if isinstance(data, dict):
        yaml_payload: dict | str = data
    elif isinstance(data, list):
        yaml_payload = data
    elif isinstance(data, (bytes, bytearray)):
        # bytes → строка, YAML запишет как plain scalar
        yaml_payload = data.decode("utf-8", errors="replace")
    else:
        raw = str(data)
        try:
            yaml_payload = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            yaml_payload = raw

    yaml_name = json_name.replace(".json", ".yaml")
    yaml_path = os.path.join(parts_path, yaml_name)
    yaml_text = dump_yaml(yaml_payload)
    with open(yaml_path, "w", encoding="utf-8") as f:
        f.write(yaml_text)
