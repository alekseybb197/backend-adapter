---
paths:
  - "backend_adapter/{logger,tracer,session_log,session_registry,session_settings,redact,model_usage,probe_json}.py"
  - "tests/test_{logger,tracer,session_log,session_registry,session_settings,redact,model_usage,probe_json}.py"
  - "docs/logging.md"
  - "docs/sanitizing.md"
---

# Наблюдаемость — действующие решения (docs/adr/)

Наблюдаемость харнесса — исходная цель проекта (ADR-001), не побочная функция.

- 018 — консоль всегда (обрезка `ADAPTER_LOG_TRIM`); файлы `.log/.jsonl/.parts`
  — одним флагом `ADAPTER_DEBUG_ENABLE` и только при `session_id`; trace несёт
  reasoning и аргументы целиком, причинность `tool_use_id → parent_req_id`.
- 019 — `.err` безусловно на любой финальный статус ≥ 400 распознанного входа,
  включая обрыв mid-stream.
- 020 — маскирование секретов во ВСЕХ каналах по умолчанию; сетевой трафик к
  бэкенду не модифицируется никогда. Известные пробелы: `x-api-key`, `Basic`,
  `*_PASSWORD` (исправление — ADR-040, слой 5).
- 021 — сессия — единица управления; Log — снимок при образовании сессии, TARGET —
  живое наследование; пер-сессионное состояние не персистится.
- 022 — учёт только по `usage` ответов; учёт никогда не роняет запрос.

Любой новый канал записи на диск обязан проходить через `redact`.

Предложены: 037, 039 (хранение `log/`), 040 (маскирование и защита WEBUI).
