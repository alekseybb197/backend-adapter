---
paths:
  - "backend-adapter.py"
  - "backend_adapter/{__init__,daemon,shutdown,cli}.py"
  - "tests/test_{daemon,shutdown,manual_check}.py"
  - "docs/architecture.md"
---

# Процесс и структура — действующие решения (docs/adr/)

- 002 — только stdlib для HTTP (`http.server` + `urllib`), поток на запрос, без
  asyncio и фреймворков; это изначальное требование.
- 003 — HTTP/1.0 + `Connection: close` (долг keep-alive — ADR-036, сначала измерить).
- 016 — плоский пакет, все модули на одном уровне; граф импортов верхнего уровня
  ацикличен; разрыв цикла — импорт внутри функции или колбек; новый модуль — лист
  DAG, отразить в `docs/architecture.md` §2 и §10.
- 026 — контракт завершения: SIGINT/SIGTERM → вежливый выход, `[EXIT] Bye`, rc 0;
  повторный сигнал → `os._exit(130)` с первого же сигнала. Покрыт
  `tests/test_shutdown.py` и `tests/test_manual_check.py`.
