"""Офлайн-диагностика конфигурации адаптера — режим ``--root <путь> --check``.

Режим (v0.9.13) проверяет **файлы дома** (``<root>/adapter.env``,
``<root>/adapter.yaml``, ``<root>/tariffs.yaml``) и **живое окружение**
(переменные ``ADAPTER_*`` текущего процесса) и печатает построчный отчёт:

- ``[OK]``   — проверка пройдена;
- ``[WARN]`` — устаревшая/неиспользуемая переменная, отсутствующий
  необязательный файл и т.п. Сам по себе итог не роняет;
- ``[ERROR]``— значение не того типа (нечисло в int-поле, мусор в bool-поле),
  битый YAML, запись бэкенда без обязательного поля.

Итог: ``exit 1`` при наличии ``[ERROR]``, иначе ``0`` (``[WARN]`` итог не
роняет). Запускается из ``cli_args.parse_args`` ДО ``validate_env()``/импорта
config (как ``--version``), поэтому молча диагностирует даже заведомо битое
окружение: ``ADAPTER_PROXY_PORT=abc`` не уронит ``--check`` голым ``ValueError``,
а попадёт в отчёт строкой ``[ERROR]``.

Домен типов — общий с ``env_validate`` (``_ENV_SPECS`` + ``check_value``):
валидатор старта и офлайн-проверка обязаны трактовать int/bool одинаково.

**Про «неизвестные» переменные.** Строка ``[WARN]`` печатается только для
известного списка устаревших имён (``_LEGACY``) и для незнакомых
``ADAPTER_DEBUG_*`` (семейство, где опечатки реальны и все члены либо в
``_ENV_SPECS``, либо в ``_LEGACY``). Произвольные ``ADAPTER_*`` вне таблицы
молчат намеренно: токены бэкендов (``ADAPTER_BACKEND_KEY_MAIN``,
``ADAPTER_DEMO_KEY`` — имена задаёт поле ``key`` в adapter.yaml), TARGET-поля
(``ADAPTER_MESSAGES_TARGET`` и т.п.) и пер-сессионные переменные не входят в
``_ENV_SPECS``, и предупреждать о них было бы ложной тревогой.

Модуль — лист DAG: импортирует только stdlib + PyYAML + ``env_validate``
(лист). Парсер env-файла здесь, а ``cli_args._load_env_file`` берёт его
отсюда — обе точки читают ``adapter.env`` одинаково.
"""

import os
import sys
from typing import TextIO

import yaml

from . import env_validate

# Известные устаревшие/снятые переменные: имя → (версия, подсказка-замена).
# Совпадает с набором стартовых [WARN] в config.py плюс legacy-имена бэкенда
# (ADAPTER_BACKEND_BASE/_KEY сняты в v0.7.2, стартового предупреждения им не
# выдают — потому и полезно поймать их в офлайн-проверке).
_LEGACY: dict[str, tuple[str, str]] = {
    "ADAPTER_DEBUG_LOGPATH": ("v0.9.9", "переименована в ADAPTER_DATA_ROOT"),
    "ADAPTER_DEBUG_PARTS": ("v0.9.10", "удалена — второго флага объёма нет"),
    "ADAPTER_DEBUG_TRIM": ("v0.9.13", "переименована в ADAPTER_LOG_TRIM"),
    "ADAPTER_DEBUG_BODY_FULL": ("v0.6.9", "удалена"),
    "ADAPTER_DEBUG_OPENAI_BODY_FULL": ("v0.6.9", "удалена"),
    "ADAPTER_BACKEND_BASE": ("v0.7.2", "удалена — URL задаётся полем base в adapter.yaml"),
    "ADAPTER_BACKEND_KEY": ("v0.7.2", "удалена — токен задаёт поле key в adapter.yaml"),
}


def parse_env_text(text: str) -> dict[str, str]:
    """Разобрать env-файл в {имя: значение}; порядок строк сохраняется.

    Тот же формат, что ``source`` в sh, но без исполнения shell: построчный
    ``export K=V`` / ``K=V``, ``#`` в начале — комментарий, пробелы вокруг
    имени/значения снимаются, окружающие кавычки снимаются. Общий парсер для
    ``--check`` (здесь) и ``--root`` (``cli_args._load_env_file``) — обе точки
    обязаны читать ``adapter.env`` одинаково.
    """
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        result[key] = value
    return result


def _read_env_file(path: str) -> dict[str, str] | None:
    """Прочитать env-файл; None — файла нет/нечитаем, {} — пустой."""
    try:
        with open(path, encoding="utf-8") as f:
            return parse_env_text(f.read())
    except OSError:
        return None


def _check_source(out: TextIO, source: str, values: dict[str, str]) -> int:
    """Проверить один источник (файл/окружение): типы, legacy, ADAPTER_DEBUG_*.

    Возвращает число найденных ошибок типа (устаревшие/неизвестные ошибок не
    дают).
    """
    errors = 0
    for name, raw in values.items():
        if name in _LEGACY:
            if raw.strip():
                version, hint = _LEGACY[name]
                print(f"[WARN] {source}: {name} больше не читается ({version}) — {hint}.", file=out)
            continue
        kind = env_validate._ENV_SPECS.get(name)
        if kind is None:
            # Незнакомый ADAPTER_DEBUG_* — вероятная опечатка (семейство
            # целиком в таблице или в legacy); прочие ADAPTER_* молчат.
            if name.startswith("ADAPTER_DEBUG_"):
                print(f"[WARN] {source}: {name} — неизвестная переменная (опечатка?).", file=out)
            continue
        expected = env_validate.check_value(kind, raw)
        if expected is not None:
            print(f"[ERROR] {source}: {name}={raw!r}: ожидается {expected}.", file=out)
            errors += 1
    return errors


def _check_adapter_yaml(out: TextIO, path: str) -> int:
    """Проверить adapter.yaml: список backend с непустыми name/base/key.

    Возвращает число ошибок. Файла нет — ``[WARN]`` (не ошибка: ``--root``
    подставит путь сам, но запуск без него невозможен).
    """
    if not os.path.isfile(path):
        print(f"[WARN] {path}: файла нет — укажите ADAPTER_BACKEND_CONFIG или --root.", file=out)
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"[ERROR] {path}: не читается как YAML: {e}", file=out)
        return 1

    backends = data.get("backend") if isinstance(data, dict) else None
    if not isinstance(backends, list) or not backends:
        print(
            f"[ERROR] {path}: ожидается непустой список backend: с записями name/base/key.",
            file=out,
        )
        return 1

    errors = 0
    for i, entry in enumerate(backends):
        if not isinstance(entry, dict):
            print(f"[ERROR] {path}: запись backend[{i}] не является словарём.", file=out)
            errors += 1
            continue
        missing = [k for k in ("name", "base", "key") if not str(entry.get(k) or "").strip()]
        if missing:
            label = entry.get("name") or f"#{i}"
            print(
                f"[ERROR] {path}: запись {label!r} — пустые/отсутствующие поля: {', '.join(missing)}.",
                file=out,
            )
            errors += 1
    if errors == 0:
        print(
            f"[OK]   {path}: backend — {len(backends)} запись(ей), поля name/base/key заполнены.",
            file=out,
        )
    return errors


def _check_tariffs_yaml(out: TextIO, path: str) -> int:
    """tariffs.yaml опционален: отсутствие — норма, битый/не тот тип — ошибка."""
    if not os.path.isfile(path):
        print(f"[OK]   {path}: необязательного файла нет — колонка Cost покажет «--».", file=out)
        return 0
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (OSError, yaml.YAMLError) as e:
        print(f"[ERROR] {path}: не читается как YAML: {e}", file=out)
        return 1
    tariffs = data.get("tariffs") if isinstance(data, dict) else None
    if tariffs is not None and not isinstance(tariffs, list):
        print(f"[ERROR] {path}: ключ tariffs: должен быть списком (или отсутствовать).", file=out)
        return 1
    print(f"[OK]   {path}: тарифы — {0 if tariffs is None else len(tariffs)} запись(ей).", file=out)
    return 0


def run_check(root: str, *, out: TextIO | None = None) -> int:
    """Проверить конфигурацию дома ``root`` и окружение; вернуть код выхода.

    ``0`` — ошибок нет (возможны ``[WARN]``); ``1`` — есть ``[ERROR]``.
    ``out`` — поток вывода (по умолчанию ``sys.stdout``; stderr — у ошибок,
    но для простоты отчёт целиком идёт одним потоком).
    """
    out = out or sys.stdout
    env_path = os.path.join(root, "adapter.env")
    yaml_path = os.path.join(root, "adapter.yaml")
    tariffs_path = os.path.join(root, "tariffs.yaml")

    print(f"Проверка конфигурации: {root}", file=out)

    errors = _check_adapter_yaml(out, yaml_path)
    errors += _check_tariffs_yaml(out, tariffs_path)

    file_env = _read_env_file(env_path)
    if file_env is None:
        print(f"[WARN] {env_path}: файла нет (настройки берутся из окружения).", file=out)
    else:
        print(f"[OK]   {env_path}: переменных — {len(file_env)}.", file=out)
        errors += _check_source(out, env_path, file_env)

    live = {k: v for k, v in os.environ.items() if k.startswith("ADAPTER_")}
    print(f"[OK]   Окружение: переменных ADAPTER_* — {len(live)}.", file=out)
    errors += _check_source(out, "окружение", live)

    # Информационно: adapter.yaml есть, но ADAPTER_BACKEND_CONFIG не задан —
    # --root подставит путь сам, это не ошибка. Имена токенов из adapter.yaml
    # не перечисляем (они не входят в _ENV_SPECS и не предупреждаются).
    if os.path.isfile(yaml_path) and not os.environ.get("ADAPTER_BACKEND_CONFIG", "").strip():
        print(
            "[OK]   ADAPTER_BACKEND_CONFIG не задан — при запуске --root подставит adapter.yaml.",
            file=out,
        )

    if errors:
        print(f"Итог: ошибок — {errors}. Исправьте значения и повторите --check.", file=out)
        return 1
    print("Итог: ошибок нет.", file=out)
    return 0
