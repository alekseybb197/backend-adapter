# ADR-026: Контракт корректного завершения по SIGINT/SIGTERM

| Поле | Значение |
|---|---|
| Статус | Принято (v0.8.6); доработано в v0.9.1 |
| Тип решения | deliberate (записано) |
| Уверенность | high |
| Источник рационала | журнал ADR 2026-09-08 «Корректное завершение по Ctrl-C/SIGTERM» и «Замечания после graceful shutdown»; docstring `shutdown.py`; changelog v0.9.1 |

> Этот ADR восстанавливает запись, на которую ссылаются `backend_adapter/shutdown.py`,
> `backend-adapter.py`, `tests/test_shutdown.py` и `tests/test_manual_check.py`
> («см. ADR … в CLAUDE.md»). Из `CLAUDE.md` запись удалена коммитом `fdbead0`.

## Контекст

Повторный Ctrl-C во время завершения прерывал запись `model-usage.yaml`
посреди операции: traceback и мусорный `.tmp`. SIGTERM (systemd, launchd,
`kill`) убивал процесс мгновенно, без сохранения хвоста учёта. В бинарнике
PyInstaller загрузчик задваивал сигнал.

## Решение

- SIGINT и SIGTERM обрабатываются одинаково: вежливое завершение
  (SIGTERM → `KeyboardInterrupt` в главный поток).
- Порядок: остановить слушателей WEBUI и экспортёра (`shutdown()` ждёт
  активные обработчики) → остановить фоновую проверку бэкендов → сохранить
  таблицу расхода (`flush_table`) → `[EXIT] Bye`, код 0.
- **С первого же сигнала** хэндлеры переключаются на немедленный
  `os._exit(130)` (v0.9.1): повторный сигнал не может прервать запись YAML и
  не печатает traceback.
- PyInstaller-сборка — с `--bootloader-ignore-signals`.
- Логика вынесена в `shutdown.py`, чтобы покрываться тестами; контракт
  проверяется и на реальном процессе (`test_manual_check.py`).

## Следствия

- `os._exit(130)` пропускает `atexit`: PID-файл после повторного сигнала
  остаётся (следующий старт его перезапишет).
- Повторный сигнал во время `flush_table` теряет хвост учёта (не более
  периода сохранения) вместо порчи файла — осознанный обмен.

## Доказательства

- `backend_adapter/shutdown.py`: docstring «Контракт завершения»,
  `install_signal_handlers`, `graceful_shutdown`, `os_exit`.
- `44c03a4`, `2413c1b` (v0.8.6), `99144fe` (v0.9.1).
- `tests/test_shutdown.py`, `tests/test_manual_check.py`.

## Действие

Обновить ссылки «см. ADR … в CLAUDE.md» в коде и тестах на
`docs/adr/026-graceful-shutdown-contract.md`.
