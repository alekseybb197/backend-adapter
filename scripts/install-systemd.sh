#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2025 Aleksey Barabanov
#
# install-systemd.sh — установка / управление user-level systemd-юнитом (Linux).
#
# Плагин для backend-adapter, аналог scripts/install-launchd.sh для Linux.
# Использует подход ``--root ~/.ba`` — бинарник запускается с ``--root <root>``,
# адаптер самостоятельно находит и загружает ``adapter.env``, ``adapter.yaml``,
# ``tariffs.yaml``. Юнит минимален и не содержит чувствительных данных (токенов).
#
# Отличие от системного ``install.sh --service``: тот ставит юнит в
# /etc/systemd/system под отдельным пользователем и требует root. Здесь —
# user-level юнит в ~/.config/systemd/user, без root, для текущего пользователя.
#
# Usage:
#   install-systemd.sh [install | unit-install | start | stop | status | unit | logs
#                       | unit-uninstall | uninstall [--yes]]
#
#   install        — разметить дом (если нужно) и поставить юнит;
#   unit-install   — только юнит для уже размеченного дома (дом не меняется);
#   start/stop/status/unit — управление установленным юнитом;
#   logs [-n N] [-f] — логи сервиса из journald: по умолчанию последние
#                      20 строк, -f — слежение до Ctrl-C;
#   unit-uninstall — остановить и отключить сервис, удалить юнит (дом НЕ трогает);
#   uninstall      — ПОЛНОЕ удаление (v1.0.3): юнит + дом (токены, конфиги);
#                    спрашивает подтверждение, --yes — без вопроса.
#                    Записи journald не удаляются.
#
# Переменные окружения (все опциональны):
#   BA_ROOT          — домашняя папка (по умолчанию ~/.ba)
#   BA_BINARY        — путь к бинарнику backend-adapter (по умолчанию
#                      /usr/local/bin/backend-adapter; берётся из PATH если не задан)
#   BA_SYSTEMD_UNIT  — путь к unit-файлу (по умолчанию
#                      ~/.config/systemd/user/backend-adapter.service)

set -euo pipefail

# --- Настройки ---
ROOT="${BA_ROOT:-$HOME/.ba}"
BINARY="${BA_BINARY:-/usr/local/bin/backend-adapter}"
UNIT="${BA_SYSTEMD_UNIT:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/backend-adapter.service}"

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

require_systemctl() {
    command -v systemctl &>/dev/null || die "systemctl не найден — управление systemd недоступно."
}

# --- Команды ---

cmd_install() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    echo "       Укажите путь к бинарнику через BA_BINARY или добавьте в PATH."

    # Дом размечает сам бинарник (--systemd install), запуск — отдельно.
    info "Установка systemd-юнита (дом размечается при необходимости)..."
    info "Запуск: $binary --systemd install --root $ROOT"
    "$binary" --systemd install --root "$ROOT"
}

cmd_unit_install() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    echo "       Укажите путь к бинарнику через BA_BINARY или добавьте в PATH."

    # Только юнит для уже размеченного дома — дом не трогается.
    info "Установка юнита (дом не изменяется)..."
    info "Запуск: $binary --systemd unit-install --root $ROOT"
    "$binary" --systemd unit-install --root "$ROOT"
}

cmd_start() {
    require_systemctl
    if [[ ! -f "$UNIT" ]]; then
        die "Юнит не найден: $UNIT. Сначала выполните 'install'."
    fi
    info "Запуск сервиса: systemctl --user start backend-adapter.service"
    systemctl --user start backend-adapter.service || die "Не удалось запустить сервис."
}

cmd_stop() {
    require_systemctl
    if [[ ! -f "$UNIT" ]]; then
        die "Юнит не найден: $UNIT"
    fi
    info "Остановка сервиса: systemctl --user stop backend-adapter.service"
    systemctl --user stop backend-adapter.service || warn "Сервис уже остановлен или недоступен."
}

cmd_status() {
    require_systemctl
    if [[ ! -f "$UNIT" ]]; then
        die "Юнит не найден: $UNIT"
    fi
    systemctl --user status --no-pager backend-adapter.service || true
    # Также проверяем процесс
    if pgrep -f "backend-adapter.*$ROOT" &>/dev/null; then
        info "Процесс backend-adapter запущен (PID: $(pgrep -f "backend-adapter.*$ROOT" | head -1))"
    else
        warn "Процесс backend-adapter не найден."
    fi
}

cmd_unit() {
    if [[ ! -f "$UNIT" ]]; then
        die "Юнит не найден: $UNIT. Сначала выполните 'install'."
    fi
    cat "$UNIT"
}

# logs [-n N] [-f] — логи сервиса из journald (по умолчанию 20 строк).
# Разбор глубины/слежения — в бинарнике (--systemd logs), здесь лишь проброс:
# единственный источник правды о юните и источнике лога.
cmd_logs() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --systemd logs --root "$ROOT" "$@"
}

cmd_unit_uninstall() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --systemd unit-uninstall --root "$ROOT"
}

# uninstall — полное удаление. Флаги (например --yes) пробрасываются как есть:
# подтверждение задаёт сам вызывающий, обёртка его не подставляет.
cmd_uninstall() {
    local binary
    binary="$(find_binary)" || die "backend-adapter не найден."
    "$binary" --systemd uninstall --root "$ROOT" "$@"
}

# --- Точка входа ---

main() {
    local cmd="${1:-install}"
    case "$cmd" in
        install)        cmd_install ;;
        unit-install)   cmd_unit_install ;;
        start)          cmd_start ;;
        stop)           cmd_stop ;;
        status)         cmd_status ;;
        unit)           cmd_unit ;;
        logs)           shift; cmd_logs "$@" ;;
        unit-uninstall) cmd_unit_uninstall ;;
        uninstall)      shift; cmd_uninstall "$@" ;;
        -h|--help|help)
            echo "Usage: $0 [install | unit-install | start | stop | status | unit | logs [-n N] [-f] | unit-uninstall | uninstall [--yes]]"
            echo ""
            echo "Commands:"
            echo "  install        — Разметить дом (если нужно), установить юнит (запуск отдельно)"
            echo "  unit-install   — Только установить юнит для УЖЕ размеченного дома (дом не меняется)"
            echo "  start          — Запустить сервис (systemctl --user start)"
            echo "  stop           — Остановить сервис (systemctl --user stop)"
            echo "  status         — Показать статус сервиса"
            echo "  unit           — Показать содержимое юнита"
            echo "  logs           — Логи сервиса из journald (по умолчанию 20 строк; -n N — глубина, -f — слежение)"
            echo "  unit-uninstall — Остановить и отключить сервис, удалить юнит (дом НЕ трогает)"
            echo "  uninstall      — ПОЛНОЕ удаление: юнит + дом (токены, конфиги);"
            echo "                   спрашивает подтверждение, --yes — без вопроса;"
            echo "                   записи journald не удаляются"
            echo ""
            echo "Environment:"
            echo "  BA_ROOT          — домашняя папка (default: ~/.ba)"
            echo "  BA_BINARY        — путь к бинарнику (default: /usr/local/bin/backend-adapter)"
            echo "  BA_SYSTEMD_UNIT  — путь к юниту (default: ~/.config/systemd/user/backend-adapter.service)"
            ;;
        *)
            die "Неизвестная команда: $cmd"
            echo "См. '$0 help'"
            ;;
    esac
}

main "$@"
