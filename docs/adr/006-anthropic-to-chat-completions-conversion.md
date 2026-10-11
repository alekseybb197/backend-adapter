# ADR-006: Конвертация Anthropic Messages ↔ Chat Completions — основной тракт

| Поле | Значение |
|---|---|
| Статус | Принято (MVP `d1ad213`); дефолт входа `/v1/messages` с v0.9.0 |
| Тип решения | deliberate (по признакам) |
| Уверенность | high |
| Источник рационала | `README.md`, `docs/architecture.md` §5.1–5.3; реконструкция |

## Контекст

Бэкенд надёжно реализует только `/v1/chat/completions` (ADR-001). Клиент
[CC] говорит только на Anthropic Messages.

## Решение

Полная двунаправленная конвертация в `convert.py` (не-стрим) и
`streaming.py` (стрим):

| Anthropic | Chat Completions |
|---|---|
| `system` + все `role=system` | одно `system`-сообщение первым (ADR-007) |
| `tool_result`-блоки в `user` | отдельные сообщения `role=tool` с `tool_call_id` |
| `tool_use` в `assistant` | `assistant.tool_calls[]` |
| `tools[].input_schema` | `tools[].function.parameters` |
| `tool_choice` | `tool_choice` (auto/required/function) |
| ← `usage.prompt/completion_tokens` | → `usage.input/output_tokens` |
| ← `finish_reason=tool_calls` | → `stop_reason=tool_use` |

`reasoning_content` из ответа бэкенда в Anthropic-ответ **не** превращается:
пишется только в trace (ADR-018).

## Рассмотренные альтернативы

- Передавать Anthropic-тело бэкенду дословно (`/v1/messages` бэкенда).
  Позже стало доступно как `passthrough` (ADR-011), но не как дефолт:
  дефолт `ADAPTER_MESSAGES_TARGET=completions` не менялся ни в одной версии.

## Следствия

- Конвертер — самая нагруженная логикой часть (`convert.py` ~770 строк,
  `streaming.py` ~975 строк) и самая покрытая тестами (`test_convert.py`,
  `test_streaming.py`).
- Возможности Anthropic, которых нет в Chat Completions (thinking-блоки,
  cache_control, изображения и т. п.), теряются или упрощаются. Перечень
  потерь нигде не зафиксирован.

## Доказательства

- `backend_adapter/convert.py`: `convert_messages_anthropic_to_openai`,
  `convert_tools_anthropic_to_openai`, `convert_tool_choice_anthropic_to_openai`,
  `convert_openai_to_anthropic`.
- `backend_adapter/streaming.py`: `stream_openai_to_anthropic`.
- `backend_adapter/routing.py`: `IMPLEMENTED_CONVERSIONS`.

## Открытые вопросы автору

- Какие элементы Anthropic-протокола сознательно не поддерживаются
  (thinking, images, cache_control, `metadata`)? Это стоит записать как
  ограничение.
