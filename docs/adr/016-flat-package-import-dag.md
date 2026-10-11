# ADR-016: Плоский пакет модулей с инвариантом DAG импортов

| Поле | Значение |
|---|---|
| Статус | Принято (v0.6.0); плоская раскладка подтверждена 2026-09-02 |
| Тип решения | deliberate (записано) |
| Уверенность | high |
| Источник рационала | changelog v0.6.0 (восстановлен из `becdabd9:changelog.md`); сообщение коммита `193d987`; `CLAUDE.md` «Инвариант DAG» |

## Контекст

К v0.6.0 `backend-adapter.py` вырос до ~1780 строк: конфигурация,
конвертация, стриминг, сервер, логирование, трассировка в одном файле.

## Решение

- Монолит разбит на доменный пакет `backend_adapter/`; `backend-adapter.py` —
  тонкая точка входа и единственный источник `__version__`.
- **Все модули лежат на одном уровне**, без подпакетов. При разбиении
  `artifact_tree.py` (1694 строки) на 7 модулей они названы
  `artifact_tree_*.py` рядом, а исходный файл оставлен тонким шимом
  с публичным API `generate()`.
- **Инвариант DAG:** граф импортов ацикличен. Базовые модули без внутренних
  зависимостей при импорте: `redact`, `session_log`, `daemon`, `config`,
  `artifact_tree_common`, `webserver`. Новые модули — «листья»: зависят
  только от `config` и/или stdlib.
- Приёмы удержания DAG:
  - импорт внутри функции для разрыва цикла (`artifact_refresh` →
    `artifact_tree`; `webserver.serve()` импортирует эндпойнты);
  - колбеки вместо обратного импорта (`config.set_on_change` для
    `state_store`);
  - **дублирование констант-дефолтов** вместо общего модуля (журнал
    2026-09-06: «дубликаты-дефолты — паттерн проекта»).
- Изменение раскладки обратно совместимо: env и поведение не меняются.

## Рассмотренные альтернативы

- Подпакет `backend_adapter/artifact_tree/` — отвергнут при рефакторинге
  `193d987` (выбрана плоская раскладка).

## Следствия

- 39 файлов `.py` в одном каталоге (включая `__init__.py`; в `README.md`
  указано 38 — счёт устарел); группировка — только префиксом имени
  (`webui_*`, `artifact_tree_*`, `session_*`).
- Дублированные дефолты (`routing._TARGET_DEFAULTS` и `config.py`,
  `~/.ba` в `daemon`/`probe_json`/`session_log`) нужно менять синхронно;
  от рассинхронизации защищают тесты.
- Граф описан в `docs/architecture.md` §10 и проверяется вручную, не
  инструментом.

## Доказательства

- `72a81e0` (v0.6.0), `193d987` (artifact_tree, «flat package modules»).
- `CLAUDE.md`, «Инвариант DAG».
- `backend_adapter/artifact_refresh.py` — импорт `artifact_tree` внутри
  функций.

## Замеченная несогласованность

`docs/architecture.md` §2 обосновывает плоскую раскладку ссылкой «see ADR
2026-09-01», но запись журнала за 2026-09-01 посвящена тестовому каркасу.
Решение о плоской раскладке записано только в сообщении `193d987`
(2026-09-02). Ссылку стоит заменить на ADR-016.

## Ответ автора (2026-10-10)

- Плоская раскладка — **сознательный выбор дизайна**: дробить проект на
  подпакеты нет смысла, пока он не публикуется в PyPI. Установка через pip и
  Homebrew — задача последнего порядка.
- Автоматическая проверка DAG в CI не исключена, но CI не должен
  перегружаться.

**Предложение по проверке DAG с учётом бюджета CI.** Внешний инструмент
(`import-linter`) нарушает принцип минимума зависимостей (ADR-002) даже для
dev-окружения. Дешевле — один тест в существующем прогоне pytest, без
отдельного CI-джоба: разобрать `ast` каждого модуля `backend_adapter/`,
собрать рёбра **импортов верхнего уровня** (импорты внутри функций
намеренно исключены — это легальный приём разрыва цикла) и проверить
ацикличность обходом в глубину. Стоимость — доли секунды; ошибка теста
печатает найденный цикл.

```python
# tests/test_import_dag.py
import ast
import pathlib

PKG = pathlib.Path(__file__).resolve().parent.parent / "backend_adapter"


def _top_level_deps(path: pathlib.Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    deps: set[str] = set()
    for node in tree.body:  # только верхний уровень модуля
        if isinstance(node, ast.ImportFrom) and node.level == 1:
            if node.module:
                deps.add(node.module.split(".")[0])
            else:  # from . import x, y
                deps.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("backend_adapter"):
            parts = (node.module or "").split(".")
            if len(parts) > 1:
                deps.add(parts[1])
            else:
                deps.update(a.name for a in node.names)
    return deps


def test_package_import_graph_is_acyclic() -> None:
    mods = {p.stem: p for p in PKG.glob("*.py") if p.stem != "__init__"}
    graph = {m: _top_level_deps(p) & mods.keys() for m, p in mods.items()}
    state: dict[str, int] = {}  # 1 — в стеке обхода, 2 — пройден
    stack: list[str] = []

    def visit(m: str) -> None:
        state[m] = 1
        stack.append(m)
        for d in sorted(graph[m]):
            if state.get(d) == 1:
                cycle = stack[stack.index(d):] + [d]
                raise AssertionError("цикл импортов: " + " -> ".join(cycle))
            if d not in state:
                visit(d)
        stack.pop()
        state[m] = 2

    for m in sorted(graph):
        if m not in state:
            visit(m)
```

Тест намеренно проверяет только ацикличность, а не список «листьев» из
`CLAUDE.md`: роль модуля меняется чаще, чем инвариант.

Проверено на v1.0.0: проходит за 0,1 с; на копии пакета с добавленной в
`routing.py` строкой `from . import server` падает с сообщением
`цикл импортов: routing -> server -> routing`. В репозиторий тест не
добавлен — решение за автором.
