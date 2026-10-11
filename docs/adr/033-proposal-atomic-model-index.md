# ADR-033: Атомарная публикация индекса моделей

| Поле | Значение |
|---|---|
| Статус | **Предложено** (2026-10-11); уточняет ADR-017 |
| Тип решения | proposal |
| Повод | ответ автора на вопрос 21: «нашли — надо решать, предложения принимаются» |

## Контекст

`refresh_models` и `_init_multi_backends` публикуют кэши так:

```python
_AVAILABLE_MODELS.clear(); _AVAILABLE_MODELS.update(available)
_MODEL_TO_BACKEND.clear(); _MODEL_TO_BACKEND.update(model_to_backend)
```

Поток запроса между `clear()` и `update()` видит пустые словари:
strict-проверка пропускается (`and _AVAILABLE_MODELS`), `_resolve_backend`
не находит модель и уходит в фолбэк на первый бэкенд. Кроме того, два
словаря обновляются не одновременно: запрос может увидеть новый
`_AVAILABLE_MODELS` и ещё пустой `_MODEL_TO_BACKEND`.

Мутация на месте введена намеренно (v0.7.2, v0.9.8): модули импортируют
словари по имени (`from .config import _AVAILABLE_MODELS`) и держат ссылку
на объект.

Читатели: `server.py` (`do_GET /v1/models`, strict-проверка),
`config._resolve_backend`, `webui_ops` (`/ready`), `webui_status`,
`prometheus_exporter`, ленивый прокси в `__init__.py`.

## Варианты

### B (минимальный, сразу) — убрать окно пустоты, сохранив мутацию на месте

```python
def _replace_in_place(target: dict, source: dict) -> None:
    """Обновить словарь без момента пустоты: сначала добавить/обновить,
    потом удалить исчезнувшие ключи."""
    target.update(source)
    for stale in [k for k in target if k not in source]:
        target.pop(stale, None)
```

Плюсы: 10 строк, импорты по имени не меняются. Минус: два словаря
по-прежнему обновляются не атомарно относительно друг друга. Окно
сокращается до «модель уже/ещё видна в одном словаре и не видна в другом»,
что даёт тот же фолбэк, но только для модели, которая прямо сейчас
появляется или исчезает, а не для всех.

Итерация по словарю в других потоках (`list(_AVAILABLE_MODELS.keys())` при
формировании сообщения об ошибке) во время `update` может бросить
`RuntimeError: dictionary changed size during iteration` — этот риск
существует и сейчас и вариантом B не устраняется.

### A (целевой) — один неизменяемый снимок, публикуемый присваиванием

```python
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping


@dataclass(frozen=True)
class ModelIndex:
    available: Mapping[str, dict]               # client_model -> запись /v1/models
    to_backend: Mapping[str, tuple[str, dict]]  # client_model -> (имя бэкенда, cfg)


_MODEL_INDEX = ModelIndex(MappingProxyType({}), MappingProxyType({}))


def model_index() -> ModelIndex:
    """Текущий снимок. Взять ОДИН раз на запрос и пользоваться им до конца."""
    return _MODEL_INDEX


def _publish_index(available: dict, to_backend: dict) -> None:
    global _MODEL_INDEX
    # Присваивание ссылки атомарно в CPython; читатель видит либо старый,
    # либо новый снимок целиком, оба словаря согласованы между собой.
    _MODEL_INDEX = ModelIndex(
        MappingProxyType(dict(available)), MappingProxyType(dict(to_backend))
    )
```

Читатель:

```python
idx = config.model_index()          # один снимок на весь запрос
if config.ADAPTER_STRICT_MODELS and idx.available and client_model not in idx.available:
    ...
backend_name, backend_cfg = config._resolve_backend(client_model, idx)
```

Плюсы: нет ни пустого, ни рассогласованного состояния; strict-проверка и
выбор бэкенда в одном запросе гарантированно используют одни данные;
`MappingProxyType` запрещает случайную мутацию извне. Минус: миграция всех
читателей с импорта по имени на `config.model_index()` (7 мест + тесты).
Импорт `from .config import _AVAILABLE_MODELS` в этой схеме становится
ошибкой — его стоит удалить, а не оставлять для совместимости.

## Предлагаемое решение

Сначала B (быстро закрывает основной сценарий), затем A как отдельная
задача, совмещённая с ADR-017: инвариант «коллекции мутируются на месте»
заменяется на «индекс моделей публикуется снимком». Для `_BACKENDS` то же
самое при необходимости.

## Проверка

Тест-воспроизведение для обоих вариантов: поток, в цикле вызывающий
публикацию индекса, и поток, в цикле проверяющий
`client_model in available and _resolve_backend(client_model) == ожидаемый`;
на текущем коде тест падает за доли секунды, после исправления — проходит.
