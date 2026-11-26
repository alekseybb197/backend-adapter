#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2025 Aleksey Barabanov
#
# install-launchd.sh — установка / управление launchd-агентом для macOS.
#
# Плагин для backend-adapter. Использует подход ``--root ~/.ba`` —
# бинарник запускается с ``--root <root>``, адаптер самостоятельно
# находит и загружает ``adapter.env``, ``adapter.yaml``, ``tariffs.yaml``.
# Plist минимален и не содержит чувствительных данных (токенов).
#
# Usage:
#   install-launchd.sh [install | plist-install | load | unload | status | plist | logs | uninstall]
#
#   install        — разметить дом (если нужно) и поставить plist;
#   plist-install  — только plist для уже размеченного дома (дом не меняется);
#   load/unload/status/plist — управление установленным plist;
#   logs [-n N] [-f] — хвост лога агента (~/Library/Logs/backend-adapter/adapter.log):
#                      по умолчанию последние 20 строк, -f — слежение до Ctrl-C;
#   uninstall      — остановить агент, снять регистрацию и удалить plist (дом не трогает).
#
# Переменные окружения (все опциональны):
#   BA_ROOT              — домашняя папка (по умолчанию ~/.ba)
#   BA_BINARY            — путь к бинарнику backend-adapter (по умолчанию
#                          ~/.local/bin/backend-adapter; берётся из PATH если не задан)
#   BA_LAUNCHD_PLIST     — путь к plist-файлу (по умолчанию
#                          ~/Library/LaunchAgents/com.user.backend-adapter.plist)

set -euo pipefail

# --- Настройки ---
ROOT="${BA_ROOT:-$HOME/.ba}"
BINARY="${BA_BINARY:-$HOME/.local/bin/backend-adapter}"
PLIST="${BA_LAUNCHD_PLIST:-$HOME/Library/LaunchAgents/com.user.backend-adapter.plist}"

PLIST_DIR="$(dirname "$PLIST")"

# --- Вспомогательные функции ---

die() {
    echo "[FATAL] $*" >&2
    exit 1
}

warn() {
    echo "[WARN] $*" >&2
}

info() {
    echo "[OK] $*"
}

# Найти рабочий бинарник backend-adapter
find_binary() {
    if [[ -f "$BINARY" ]]; then
        echo "$BINARY"
        return 0
    fi
    if command -v backend-adapter &>/dev/null; then
        echo "$(command -v backend-adapter)"
        return 0
    fi
    return 1
}

# --- Команды ---

cmd_install() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    echo "       Укажите путь к бинарнику через BA_BINARY или добавьте в PATH."

    info "Установка launchd-агента..."
    # Дом размечает сам бинарник (--launchd install): ensure_root здесь не
    # нужен и вреден — он завершил бы работу на неразмеченном доме.

    # Запускаем Python-бинарник — он сам разметит дом (если нужно),
    # сгенерирует и установит plist.
    info "Запуск: $binary --launchd install --root $ROOT"
    "$binary" --launchd install --root "$ROOT"
}

cmd_plist_install() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    echo "       Укажите путь к бинарнику через BA_BINARY или добавьте в PATH."

    # Только plist для уже размеченного дома — дом не трогается.
    info "Установка plist (дом не изменяется)..."
    info "Запуск: $binary --launchd plist-install --root $ROOT"
    "$binary" --launchd plist-install --root "$ROOT"
}

# load/unload/status/uninstall — проброс в бинарник: там единственная правда
# о launchd (bootstrap + kickstart вместо «load», bootout вместо «unload»,
# разбор `launchctl print` вместо JSON-словаря `list`).
cmd_load() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --launchd load --root "$ROOT"
}

cmd_unload() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --launchd unload --root "$ROOT"
}

cmd_status() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --launchd status --root "$ROOT"
}

cmd_uninstall() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --launchd uninstall --root "$ROOT"
}

cmd_plist() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --launchd plist --root "$ROOT"
}

# logs [-n N] [-f] — показать хвост лога агента (по умолчанию 20 строк).
# Разбор глубины/слежения — в бинарнике (--launchd logs), здесь лишь проброс:
# единственный источник правды о формате лога и его расположении.
cmd_logs() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --launchd logs --root "$ROOT" "$@"
}

# --- Точка входа ---

main() {
    local cmd="${1:-install}"
    case "$cmd" in
        install)        cmd_install ;;
        plist-install)  cmd_plist_install ;;
        load)           cmd_load ;;
        unload)         cmd_unload ;;
        status)         cmd_status ;;
        plist)          cmd_plist ;;
        logs)           shift; cmd_logs "$@" ;;
        uninstall)      cmd_uninstall ;;
        -h|--help|help)
            echo "Usage: $0 [install | plist-install | load | unload | status | plist | logs [-n N] [-f] | uninstall]"
            echo ""
            echo "Commands:"
            echo "  install        — Разметить дом (если нужно), сгенерировать и установить plist"
            echo "  plist-install  — Только установить plist для УЖЕ размеченного дома (дом не меняется)"
            echo "  load           — Зарегистрировать агент и запустить его"
            echo "  unload         — Остановить агент и снять регистрацию"
            echo "  status         — Показать статус агента"
            echo "  plist          — Показать содержимое plist"
            echo "  logs           — Хвост лога агента (по умолчанию 20 строк; -n N — глубина, -f — слежение)"
            echo "  uninstall      — Остановить агент, снять регистрацию и удалить plist (дом не меняется)"
            echo ""
            echo "Environment:"
            echo "  BA_ROOT          — домашняя папка (default: ~/.ba)"
            echo "  BA_BINARY        — путь к бинарнику (default: ~/.local/bin/backend-adapter)"
            echo "  BA_LAUNCHD_PLIST — путь к plist (default: ~/Library/LaunchAgents/...)"
            ;;
        *)
            die "Неизвестная команда: $cmd"
            echo "См. '$0 help'"
            ;;
    esac
}

main "$@"
