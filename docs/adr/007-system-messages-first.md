# ADR-007: Инвариант «system-сообщения в начале»

| Поле | Значение |
|---|---|
| Статус | Принято (MVP); расширено в v0.9.2 и v0.9.7 |
| Тип решения | deliberate (записано) |
| Уверенность | high |
| Источник рационала | `README.md` «Проблемы» п. 1; docstring `normalize_messages_system_first`; changelog v0.9.2, v0.9.7 |

## Контекст

Бэкенд и локальные chat-шаблоны (vLLM, Qwen3 под llama.cpp) требуют, чтобы
system-сообщение было первым, а часть шаблонов — ровно одно
(`System message must be at the beginning`). [CC] шлёт system-сообщения по
ходу диалога и `<system-reminder>` внутри `user`; Codex CLI шлёт роль
`developer`, которую часть шаблонов не знает (`Unexpected message role`).

## Решение

Три реализации одного инварианта, по тракту:

| Тракт | Поведение | С версии |
|---|---|---|
| `messages→completions` | все system склеиваются в **одно** сообщение первым | MVP |
| `messages→messages` | system **переносятся** в начало без склейки, остальной порядок сохраняется (`normalize_messages_system_first`) | v0.9.2 |
| `responses→completions` | `instructions` + все `developer` **схлопываются** в одно system; `role=system` не трогается, затем общий перенос в начало | v0.9.7 |
| `passthrough`, `completions→completions` | инвариант **не** применяется (дословная передача) | v0.9.4, v1.0.0 |

## Рассмотренные альтернативы

- Склейка и в `messages→messages`. Отвергнута: «контракт Anthropic-формата
  E→E сохраняется максимально» (docstring).
- Схлопывать и `system`, и `developer` в Responses. Отвергнута: схлопывается
  только `developer` (changelog v0.9.7).

## Следствия

- Инвариант — главное отличие «конверсии в тот же формат» от
  `passthrough`: именно ради него `messages→messages` существует как
  отдельная пара (ADR-011).
- Склейка system меняет позиционный смысл поздних system-напоминаний: модель
  видит их как часть исходной инструкции. Это компромисс в пользу совместимости
  с шаблонами.
- `adapter_invariant_check` в trace фиксирует позицию system (диагностика,
  changelog v0.3.0).

## Доказательства

- `backend_adapter/convert.py`: `convert_messages_anthropic_to_openai`
  (docstring «все system messages ДОЛЖНЫ быть в ОДНОМ сообщении в начале»),
  `normalize_messages_system_first`, `convert_responses_input_to_openai_messages`.
- `8fad99f` (v0.9.2), `735dc5a` (v0.9.7).
- `backend_adapter/server.py`: событие `adapter_invariant_check`.
