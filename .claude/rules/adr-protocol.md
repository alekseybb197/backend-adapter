---
paths:
  - "backend_adapter/{convert,streaming,routing,server}.py"
  - "tests/test_{convert,streaming,routing,server}.py"
  - "docs/routing.md"
---

# Протоколы и маршрутизация — действующие решения (docs/adr/)

Перед изменением открой нужный ADR по номеру:

- 006 — Anthropic Messages ↔ Chat Completions; изображения сознательно не поддерживаются.
- 007 — инвариант «system в начале»: склейка (messages→completions), перенос
  (messages→messages), схлопывание `developer` (responses→completions).
- 008 — разбор текстовых `<tool_call>` Qwen (сейчас только не-стрим).
- 009 — SSE на лету; ретрай только до первого байта клиенту; ошибка после старта
  потока — SSE-событием в формате входа.
- 010 — ретраи (429/502/503/504, таймаут), коридор `max_tokens` (и в passthrough),
  адаптивный подъём при `reasoning_budget_exhausted`.
- 011 — три входа, TARGET по статической таблице `IMPLEMENTED_CONVERSIONS`, без сети.
- 013 — Responses: `store:false`, перехват `/model`.

Предложены, НЕ реализовывать без статуса «Принято»: 034 (коридор — уже Принято),
035 (разрыв клиента перед ретраем), 037 (модель из истории), 038 (`<tool_call>` в стриме).

Новый ADR (статус «Предложено») требуется для: новой пары конверсии, изменения
политики ретраев, нового вмешательства в тело в passthrough-ветке.
