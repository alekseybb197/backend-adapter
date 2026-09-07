#!/usr/bin/env python3
"""
webui_status.py — эндпойнт "/" общего веб-сервера WEBUI: статус-страница.

Показывает на одной странице:
  - версию кода (из WebContext.version — в адаптере это __version__ из
    backend-adapter.py, единственный источник);
  - каждый настроенный LLM-эндпойнт: доступность, список моделей и
    колонку «Доступные API» — какие известные API-эндпойнты бэкенд реально
    обслуживает (результат дымовой пробы config.probe_endpoints, см. ниже);
  - таблицу «Models in use» — модели, к которым агент обращался
    (см. model_usage.py): счётчик обращений, токены из usage-блоков ответов
    бэкенда (input_tokens/output_tokens — prompt_tokens/completion_tokens;
    ответы без usage не считаются) и какие API-эндпоинты доступны именно
    для каждой модели (результат дымовой пробы этой моделью при первом
    обращении; колонка Endpoints перечисляет только доступные).
    Таблица персистентна: сохраняется в YAML-файл model-usage.yaml в корне
    WEBUI и переживает перезапуски адаптера; кнопка «Сбросить» в строке
    таблицы обнуляет счётчики строки (calls/input_tokens/output_tokens →
    0; строка с результатами пробы эндпоинтов остаётся — POST
    /api/model-usage/reset, см. ModelUsageResetEndpoint). Кнопка «Перепроверить» (POST
    /api/model-usage/reprobe, см. ModelUsageReprobeEndpoint) запускает
    фоновую повторную дымовую пробу эндпоинтов именно этой моделью — для
    строк с «—» в колонке Endpoints; проба не трогает счётчики и токены
    строки. Пока перепроверка идёт, страница показывает баннер и ячейку
    «проверяется…» и авто-обновляется по завершении (JS → /api/model-usage/
    reprobe-state, см. ReprobeStateEndpoint).
  - Счётчики строки (Вызовов/Input/Output) обновляются БЕЗ перезагрузки
    страницы: JS usage_poll каждые ~5 с опрашивает лёгкий JSON-эндпоинт
    /api/model-usage/snapshot (UsageSnapshotEndpoint → model_usage.
    usage_snapshot(), снимок из памяти, сети к бэкендам нет) и обновляет
    только ячейки счётчиков. Оверхед — один маленький JSON-ответ раз в 5 с
    на открытую вкладку; при скрытой вкладке браузер сам троттлит таймеры.
    Колонка Cost — стоимость накопленных токенов строки по тарифу модели
    (ADAPTER_MODELS_TARIFFS, см. model_usage.lookup_tariff): считается на
    лету из токенов и тарифа на момент отображения, поллингом НЕ
    обновляется (производная — пересчитается при следующем рендере).

Откуда данные:
  - Проверка бэкендов запускается:
      * при старте адаптера (backend-adapter.py запускает config.
        start_refresh(timeout=PROBE_TIMEOUT) после поднятия WEBUI) и
      * по кнопке «⟳ Перепроверить» (POST "/") — работает по
        PRG-паттерну: запускает ФОНОВУЮ проверку config.start_refresh(
        timeout=PROBE_TIMEOUT) (см. config._refresh_worker) и отвечает
        303 See Other на GET "/" — браузер переходит на страницу
        GET-навигацией, HTTP-запрос не ждёт завершения проверки.
        Поэтому обновление/авто-релоад страницы никогда не повторяет
        POST (нет диалога «повторить действие?»).
        Кнопка не только перепроверяет бэкенды, но и ПЕРЕЧИТЫВАЕТ конфиг
        ADAPTER_BACKEND_CONFIG (config.start_refresh(reload=True) →
        reload_backend_config): бэкенды добавляются/удаляются БЕЗ рестарта
        адаптера. Битый/недоступный YAML — прежние бэкенды остаются
        ([WARN] в консоли), фоновая проверка перепроверяет их.
  - Первый заход на страницу (GET "/", _autostart_first_check)
    запускает ПЕРВУЮ проверку автоматически, если проверок ещё не
    было (done_at пуст) и есть что проверять; повторные проверки —
    только по кнопке. Пока проверка выполняется, страница показывает
    баннер «Проверка выполняется…» и опрашивает лёгкий JSON-эндпоинт
    /api/refresh-state; по завершении проверки JS сам перезагружает
    страницу (location.reload()) — она рендерит свежий результат.
    Авто-релоад безопасен: он происходит на GET-документе, повторного
    POST нет, зацикливания нет.
  - Страница рендерится из конфиг-глобалов адаптера: бэкенд мог добавить
    новые модели между стартами адаптера (или после ошибки 400 «model is
    not available»), refresh подхватывает их без перезапуска — следующие
    запросы /v1/messages с новыми моделями проходят строгую валидацию.
  - Футер «Список провайдеров обновлён в HH:MM:SS (N провайдеров, M
    моделей)» и статусы строк берутся из состояния последней проверки
    (config.refresh_state(): ok/count/providers/errors/checked_at); при
    полном провале старый список моделей сохраняется и показывается на
    странице вместе с текстом ошибки; при частичном успехе страница
    показывает свежий список ответивших бэкендов и тексты ошибок упавших.
  - Дымовая проба API: refresh_models дополнительно несёт "probe" —
    результат config.probe_endpoints(): какие из известных эндпойнтов
    (/v1/chat/completions, /v1/messages, /v1/responses, /v1/embeddings)
    бэкенд обслуживает. Политика пробы — «только явно указанные»:
    эндпойнт пробуется ТОЛЬКО если он перечислен в ключе probe YAML-записи
    бэкенда с НЕПУСТОЙ моделью; неперечисленные и пустые значения (а также
    отсутствие ключа probe вовсе) — НЕ пробуются (для бэкенда без probe
    состояние не создаётся, колонка «Доступные API» пуста). Проба —
    короткие POST с max_tokens:1 (моделью, заданной в probe), результат
    кэшируется ~60 с (ENDPOINT_PROBE_TTL) и отключается флагом
    ADAPTER_ENDPOINT_PROBE=0. Для каждого бэкенда колонка «Доступные API»
    показывает зелёным ✓ только реально работающие пути (HTTP 200);
    непрошедшие проверку пути на странице не показываются (из
    config._ENDPOINT_STATE).

САМОСТОЯТЕЛЬНЫЙ ЗАПУСК (standalone — python -m backend_adapter.webserver
вне процесса адаптера): конфиг-глобалы адаптера пусты (нет YAML-конфига),
поэтому страница показывает режим standalone и подсказку, как получить
живые данные, — вместо фантомного списка «example.com» из дефолтов env.
Эндпойнты для проверки в этом режиме задаются той же переменной
окружения, что у адаптера, — ADAPTER_BACKEND_CONFIG (путь к YAML);
модуль берёт её из config так же, как сам адаптер.

Чистая логика (snapshot, состояние, рендер) вынесена в отдельные функции,
чтобы её можно было тестировать без HTTP-сервера.
"""

import html
import json
import logging
import os
import time
from urllib.parse import parse_qs, quote, urlparse

from . import config, model_usage, webserver

logger = logging.getLogger("webui_status")

PROBE_TIMEOUT = 10.0  # жёсткий таймаут живой пробы одного эндпойнта, сек


# ==================== ЧИСТАЯ ЛОГИКА ====================


def _collect_endpoints() -> list[dict]:
    """Список настроенных LLM-эндпойнтов из конфиг-глобалов адаптера.

    Каждый элемент: {"name", "base", "key", "models": [model_id, ...],
    "api": {"путь_без_/v1": {"found": bool, "status": int|None}} | None}.
    models — модели, успешно опрошенные на старте адаптера (из
    _MODEL_TO_BACKEND, сгруппированные по бэкенду; это ровно те модели,
    что адаптер реально принимает в запросах). key — токен для живой
    пробы (в HTML не выводится). api — результат дымовой пробы эндпойнтов
    из config._ENDPOINT_STATE (нормализован: ключи без префикса "/v1/",
    в порядке config.ENDPOINT_PROBES); None — бэкенд ещё не пробован
    (standalone: refresh не делался; ADAPTER_ENDPOINT_PROBE=0).

    Режимы:
      - multi-backend в процессе адаптера (_BACKENDS заполнен при старте);
      - standalone (viewer вне адаптера): эндпойнты не опрошены, но если
        окружение задаёт ADAPTER_BACKEND_CONFIG с YAML-файлом — они
        показываются пустыми, чтобы кнопка «⟳ Перепроверить» могла
        выполнить живую пробу."""
    endpoints = []

    if config._BACKENDS:
        # Бэкенды из YAML, загружен адаптером при старте
        for b in config._BACKENDS:
            api = None
            ep_state = config._ENDPOINT_STATE.get(b["name"])
            if ep_state and ep_state.get("endpoints"):
                # Ключи результата — полные пути ("/v1/chat/completions");
                # нормализуем до "chat/completions" (без "/v1/") — рендер
                # сам восстановит порядок из ENDPOINT_PROBES.
                api = {path[len("/v1/") :]: ep for path, ep in ep_state["endpoints"].items()}
            endpoints.append(
                {
                    "name": b["name"],
                    "base": b["base"],
                    "key": b["key"],
                    "models": sorted(
                        mid
                        for mid, (bname, _) in config._MODEL_TO_BACKEND.items()
                        if bname == b["name"]
                    ),
                    "api": api,
                }
            )
        return endpoints

    # ADAPTER_BACKEND_CONFIG задан, но _BACKENDS пуст — адаптер в этом
    # процессе не стартовал (standalone, либо упал до инициализации).
    # Перечитываем YAML только ради списка эндпойнтов для живой пробы
    # (парсер config._parse_backend_yaml — тот же, что у адаптера;
    # модели всё равно не опрошены — статус будет «не опрошен»).
    cfg_path = config.ADAPTER_BACKEND_CONFIG or os.environ.get("ADAPTER_BACKEND_CONFIG", "")
    if cfg_path and os.path.isfile(cfg_path):
        blocks = config._parse_backend_yaml(cfg_path)
        for b in blocks or []:
            endpoints.append(
                {
                    "name": b["name"],
                    "base": b["base"],
                    "key": b["key"],
                    "models": [],
                    "api": None,
                }
            )
    return endpoints


def _config_snapshot() -> dict:
    """Состояние на старте для GET "/": режим, эндпойнты, доступность.

    Возвращает {"mode": str, "endpoints": [{"name","base","key","models",
    "status"}], "note": str|None}. status — "ok" (на старте опрошен, есть
    модели) или "не опрошен" (бэкенд есть в конфиге, но моделей нет — в
    multi-режиме это бэкенд, чья проба не удалась; в standalone — любой
    бэкенд из конфига, т.к. проб никто не делал). Пустой список
    эндпойнтов — режим standalone без конфига: страница показывает
    подсказку."""
    endpoints = _collect_endpoints()
    mode = "multi-backend" if endpoints else "standalone"

    for ep in endpoints:
        ep["status"] = "ok" if ep["models"] else "не опрошен"

    note = None
    if not endpoints:
        note = (
            "Данные адаптера недоступны — запущен standalone-режим (viewer вне процесса "
            "адаптера). Живые данные появятся после запуска внутри адаптера "
            "(WEBUI поднимается всегда), либо задайте ADAPTER_BACKEND_CONFIG (путь к "
            "YAML-файлу конфигурации бэкенда) и перезапустите сервер."
        )
    return {"mode": mode, "endpoints": endpoints, "note": note}


# ==================== HTML-РЕНДЕР ====================


MODEL_LINES = 4  # высота свёрнутого списка моделей: строк (каждый id — своя строка)


def _models_html(models: list[str], status: str) -> str:
    """HTML ячейки «Models»: каждый id модели — отдельная строка.

    Если моделей больше MODEL_LINES — первые MODEL_LINES показываются,
    остальные прячутся в <span class="models-extra" style="display:none">,
    а кнопка «Показать ещё (N)» разворачивает список (JS models_toggle,
    см. _render_status_page): при клике span получает display:block,
    кнопка меняется на «Свернуть» и прячет его обратно."""
    if not models:
        return f'<span style="color:#999">{status}</span>'
    line = '<div style="line-height:1.5">{}</div>'
    if len(models) <= MODEL_LINES:
        return "".join(line.format(html.escape(m)) for m in models)
    shown = "".join(line.format(html.escape(m)) for m in models[:MODEL_LINES])
    extra = "".join(line.format(html.escape(m)) for m in models[MODEL_LINES:])
    n = len(models) - MODEL_LINES
    btn = (
        f'<button type="button" onclick="models_toggle(this)" '
        f'data-models-count="{len(models)}" '
        f'style="color:#1a7f37;background:none;border:none;padding:0;'
        f'font:inherit;cursor:pointer;text-decoration:underline">'
        f"Показать ещё ({n})</button>"
    )
    return f'{shown}<span class="models-extra" style="display:none">{extra}</span>{btn}'


def _api_html(api: dict | None) -> str:
    """HTML ячейки «Доступные API» для одного бэкенда.

    ``api`` — результат дымовой пробы из _collect_endpoints()
    ({короткий_путь: {"found": bool, "status": int|None}}), None — бэкенд
    ещё не пробован (standalone / ADAPTER_ENDPOINT_PROBE=0). Зелёным с ✓
    показываются ТОЛЬКО реально работающие эндпоинты (found=True ⇔ HTTP
    200, см. классификацию в config._probe_backend_endpoints); непрошедшие
    (любой не-200 код, сетевая ошибка) и пропущенные (нет probe-модели —
    в api не значатся) на странице НЕ показываются вовсе. Проба была
    (api не None), но ни один эндпоинт не ответил 200 — ячейка показывает
    один серый «—» с пояснением вместо пустого места. Порядок —
    config.ENDPOINT_PROBES (тот же, что у самой пробы)."""
    if api is None:
        return '<span style="color:#999">не опрошено</span>'
    parts = []
    for _pname, path, _tpl in config.ENDPOINT_PROBES:
        label = path[len("/v1/") :]
        ep = api.get(label)
        if ep is not None and ep["found"]:
            # found=True гарантированно значит HTTP 200 (см. config.py)
            parts.append(f'<span style="color:#1a7f37">{label} ✓</span>')
    if not parts:
        parts.append(
            '<span style="color:#aaa" title="ни один эндпоинт не ответил HTTP 200">—</span>'
        )
    return "<br>".join(parts)


def _cost_cell_html(row: dict) -> str:
    """HTML ячейки «Cost» строки таблицы Models in use.

    Стоимость считается НА ЛЕТУ из токенов строки и тарифа на момент
    отображения: cost = input_tokens×input_price/price_per +
    output_tokens×output_price/price_per (model_usage.lookup_tariff —
    кэш, без сети и диска; см. формат тарифов в config.
    ADAPTER_MODELS_TARIFFS). Модели нет в тарифах / нулевая цена
    (бесплатная модель) / токены ещё не накоплены (0/0 — после «Сбросить»)
    → серая «—»: рендер различать эти случаи не обязан, главное — нулевая
    цена не роняет рендер. Серая «—» выводится и при нечитаемом/пустом
    файле тарифов. Ячейка НЕ несёт data-атрибута и не обновляется JS
    usage_poll (счётчики обновляются; Cost — производная, пересчитается
    при следующем полноценном рендере страницы)."""
    tokens_in = max(int(row.get("input_tokens", 0) or 0), 0)
    tokens_out = max(int(row.get("output_tokens", 0) or 0), 0)
    if tokens_in <= 0 and tokens_out <= 0:
        return '<span style="color:#aaa">—</span>'
    tariff = model_usage.lookup_tariff(str(row.get("model", "")), str(row.get("backend", "")))
    if tariff is None:
        return '<span style="color:#aaa">—</span>'
    cost = (
        tokens_in * tariff["input_price"] / tariff["price_per"]
        + tokens_out * tariff["output_price"] / tariff["price_per"]
    )
    if cost <= 0:
        # Нулевая цена (бесплатная модель) — «—», как «нет тарифа/токенов».
        return '<span style="color:#aaa">—</span>'
    return _fmt_cost(cost, tariff["currency"])


def _endpoints_cell_html(row: dict) -> str:
    """HTML ячейки «Endpoints» строки таблицы Models in use.

    Перечисляет ТОЛЬКО доступные эндпоинты (found=True ⇔ HTTP 200, см.
    классификацию в config._probe_backend_endpoints): короткие имена через
    запятую, зелёным, в порядке config.ENDPOINT_PROBES (том же, что у пробы).
    Непрошедшие/непробованные пути (не-200, probing, ADAPTER_MODEL_USAGE_
    ENABLE=0, модели нет у бэкенда) не показываются; если доступных нет —
    один серый «—» вместо пустой ячейки."""
    parts = []
    endpoints = row.get("endpoints") or {}
    for pname, _path, _tpl in config.ENDPOINT_PROBES:
        ep = endpoints.get(pname)
        if ep is not None and ep["found"]:
            parts.append(f'<span style="color:#1a7f37">{pname}</span>')
    if not parts:
        return '<span style="color:#aaa">—</span>'
    return ", ".join(parts)


def _fmt_tokens(n: int) -> str:
    """Точное число токенов с разделителем тысяч («12 345»; 0 → «0»).

    Неразрывный узкий пробел (U+202F) между разрядами — читаемо и не
    переносится. Отрицательное значение — как 0 (не бывает)."""
    return f"{max(n, 0):,}".replace(",", " ")


def _compact_number(n: int) -> str:
    """Компактное представление числа: 12k3 для 12300, 34m9 для 34874321, 1b2 для 1200000000.

    После символа только 1 цифра, остальное округляется вниз."""
    n = max(n, 0)
    if n >= 1_000_000_000:
        return f"{n // 100_000_000 / 10:.0f}b{n // 100_000_000 % 10}"
    elif n >= 1_000_000:
        return f"{n // 100_000 / 10:.0f}m{n // 100_000 % 10}"
    elif n >= 1000:
        return f"{n // 100 / 10:.0f}k{n // 100 % 10}"
    else:
        return str(n)


def _fmt_cost(cost: float, currency: str) -> str:
    """Формат стоимости для колонки Cost: «0,25 USD», «133 RUB».

    Стоимость ≤ 0 (нет токенов / бесплатная модель — нулевая цена) → «--»:
    рендер различает «нет данных» и «бесплатно» не обязан (см.
    _usage_rows_html — нулевая цена не роняет рендер). До 10 000 — точное
    число с запятой-разделителем и ДВУМЯ знаками («1234,56 USD»): деньги
    принято писать точно. ≥ 10 000 — компактно как токены (_compact_number
    на округлённом целом): колонка остаётся узкой на больших суммах
    («12k3 USD»). locale НЕ используется — формат свой (запятая — десятичный
    разделитель, как в тарифах YAML-файла)."""
    if cost <= 0 or not currency:
        return "—"
    if cost < 10_000:
        return f"{cost:.2f}".replace(".", ",") + " " + currency
    return _compact_number(int(cost)) + " " + currency


def _actions_cell_html(model: str, reprobing: bool = False) -> str:
    """HTML ячейки действий строки таблицы использованных моделей.

    Две form-кнопки целиком внутри своего <td> (валидный HTML — без
    вложенных форм и JS): «Перепроверить» (POST /api/model-usage/reprobe?model=…)
    запускает фоновую перепробу эндпоинтов строки, «Сбросить» (POST
    /api/model-usage/reset?model=…) обнуляет счётчики строки (оба эндпоинта
    по PRG отвечают 303 на GET "/"). При reprobing=True (перепроверка этой модели
    уже идёт) вместо кнопок — серый текст «проверяется…»: повторный запуск
    невозможен, страница авто-обновится по завершении. Имя модели кодируется
    quote(safe="") для query-параметра и html.escape — для атрибута action."""
    q = quote(str(model), safe="")
    if reprobing:
        return (
            '<td style="color:#888">'
            '<span title="перепроверка эндпоинтов модели выполняется">'
            "проверяется…</span></td>"
        )
    return (
        "<td>"
        f'<form method="post" action="/api/model-usage/reprobe?model={html.escape(q)}">'
        '<button type="submit" style="color:#555;background:none;border:none;'
        'padding:0 6px 0 0;font:inherit;cursor:pointer;text-decoration:underline">'
        "Перепроверить</button>"
        "</form>"
        f'<form method="post" action="/api/model-usage/reset?model={html.escape(q)}">'
        '<button type="submit" style="color:#c0392b;background:none;border:none;'
        'padding:0;font:inherit;cursor:pointer;text-decoration:underline">Сбросить</button>'
        "</form></td>"
    )


def _usage_rows_html(rows: list[dict], reprobing: dict | None = None) -> str:
    """HTML строк таблицы Models in use (по строке на модель).

    ``rows`` — model_usage.usage_snapshot() (порядок первого обращения).
    Колонки: Модель | Бэкенд | Вызовов | Input | Output | Cost | Endpoints |
    Действия. Колонка Endpoints перечисляет только доступные эндпоинты
    (found=True) короткими именами через запятую (см. _endpoints_cell_html).
    input_tokens/output_tokens — токены из usage-блоков ответов бэкенда (см.
    _fmt_tokens); поля отсутствуют у мигрировавших/старых сидов → 0.
    Колонка Cost — стоимость накопленных токенов по тарифу модели на момент
    отображения (см. _cost_cell_html / _fmt_cost; «--» — модели нет в
    тарифах, бесплатная модель или токенов ещё нет). Ячейки счётчиков несут
    data-атрибуты (data-calls/data-input/data-output) с ТОЧНЫМИ значениями —
    JS usage_poll обновляет их textContent по /api/model-usage/snapshot без
    перезагрузки страницы (см. usage_poll в _render_status_page). ``reprobing``
    — карта client_model → True: у строки идёт фоновая перепроверка (баннер +
    авто-релоад); в ячейке действий вместо кнопок — «проверяется…».
    Модель/бэкенд — html.escape; «Перепроверить»/«Сбросить» — отдельные
    формы в последнем <td> (см. _actions_cell_html)."""
    body = []
    for i, r in enumerate(rows):
        reprobing_row = bool(reprobing and reprobing.get(r["model"]))
        calls = r.get("calls", 0)
        input_tokens = r.get("input_tokens", 0)
        output_tokens = r.get("output_tokens", 0)
        body.append(
            f'<tr id="usage-row-{i}">'
            f"<td>{html.escape(str(r['model']))}</td>"
            f"<td>{html.escape(str(r['backend']))}</td>"
            f'<td data-calls="{calls}">{calls}</td>'
            f'<td data-input="{input_tokens}">{_compact_number(input_tokens)}</td>'
            f'<td data-output="{output_tokens}">{_compact_number(output_tokens)}</td>'
            f"<td>{_cost_cell_html(r)}</td>"
            f"<td>{_endpoints_cell_html(r)}</td>"
            f"{_actions_cell_html(r['model'], reprobing_row)}"
            "</tr>"
        )
    if not body:
        body.append(
            '<tr><td colspan="8" style="color:#888">пока нет данных — '
            "таблица заполняется при первых запросах к моделям</td></tr>"
        )
    return "".join(body)


def _render_status_page(
    context, refresh=None, checked_at=None, running=None, started_at=None
) -> bytes:
    """HTML статус-страницы.

    ``refresh`` — результат последней проверки из config.refresh_state():
    {"ok", "count", "providers", "errors": {имя_бэкенда: текст}} (или None —
    когда проверок ещё не было / обновлять было нечего: standalone без
    настроенных эндпойнтов). checked_at — время последнего обновления
    ("HH:MM:SS", или None). running — идёт ли проверка прямо сейчас
    (показывается баннер). started_at — time.time() запуска идущей
    проверки (для времени в баннере; None, если проверка не идёт).

    Статусы строк берутся из snapshot, но поверх: если бэкенд есть в
    refresh["errors"] — строка показывает «недоступен (текст ошибки)».
    Модели — из обновлённых конфиг-глобалов: при полном провале refresh
    кэш не тронут (показывается прежний список), при частичном — упавший
    бэкенд честно без моделей. Колонка «Доступные API» рендерится из
    config._ENDPOINT_STATE через _collect_endpoints (сама проба выполняется
    внутри refresh_models; refresh["probe"] отдельно не рендерится).
    Сразу под таблицей бэкендов — футер о последней проверке ({footer},
    «Список провайдеров обновлён в HH:MM:SS (N провайдеров, M моделей)»,
    где N = refresh["providers"] — число настроенных бэкендов после
    перечитывания конфига, M = count моделей) и кнопка «⟳ Перепроверить»
    (POST "/").
    Секция «Models in use» рендерится из model_usage.usage_snapshot() (см.
    _usage_rows_html) — таблица заполняется запросами агента в этом процессе
    независимо от проверок бэкендов; колонка Endpoints перечисляет только
    доступные эндпоинты строки. Перепроверка строки (кнопка «Перепроверить»,
    model_usage.reprobe_state()) — отдельный фоновый процесс (см.
    ModelUsageReprobeEndpoint): пока идёт, страница показывает свой баннер
    и авто-обновляется по завершении (JS reprobe_poll → /api/model-usage/
    reprobe-state → location.reload()).
    Счётчики строк секции «Models in use» обновляются без перезагрузки:
    безусловный JS usage_poll (usage_poll_script в <head>) каждые 5 с
    опрашивает /api/model-usage/snapshot (см. UsageSnapshotEndpoint) и
    правит textContent ячеек Вызовов/Input/Output по data-атрибутам строк
    (рендер — см. _usage_rows_html). Строки сопоставляются позиционно:
    снимок идёт в порядке первого обращения, как и рендер. Число строк
    изменилось (сброс/новая модель) — location.reload() перерисует
    таблицу; эндпоинты строк в этом поллинге не трогаются (их меняет
    только reprobe, у которого свой авто-релоад)."""
    # Колонка Cost строк Models in use считается по тарифу на момент
    # отображения: каждый полноценный рендер страницы (GET "/", перезагрузка)
    # перечитывает маленький файл тарифов с диска (model_usage.
    # ensure_tariffs_loaded — см. ADAPTER_MODELS_TARIFFS). Лёгкий поллинг
    # usage_poll (снимок JSON) тарифы НЕ трогает — Cost обновится при
    # следующем рендере (это осознанно, см. _cost_cell_html).
    model_usage.ensure_tariffs_loaded()
    snapshot = _config_snapshot()
    endpoints = snapshot["endpoints"]
    errors = (refresh or {}).get("errors", {}) or {}
    rows = []
    for ep in endpoints:
        err_text = errors.get(ep["name"])
        if err_text:
            status_cell = (
                f'<span style="color:#c0392b">недоступен</span> '
                f'<span style="color:#999">({html.escape(str(err_text))})</span>'
            )
            # Модели: после refresh кэш отражает реальность — упавший
            # бэкенд при частичном успехе выпал из кэша (моделей нет);
            # при полном провале кэш не тронут и показывается прежний.
            models_cell = _models_html(ep["models"], "недоступен")
        else:
            status_cell = (
                '<span style="color:#1a7f37">ok</span>'
                if ep["status"] == "ok"
                else f'<span style="color:#b8860b">{html.escape(ep["status"])}</span>'
            )
            models_cell = _models_html(ep["models"], ep["status"])

        rows.append(f"""
      <tr>
        <td>{html.escape(ep["name"])}</td>
        <td><code>{html.escape(ep["base"])}</code></td>
        <td>{status_cell}</td>
        <td>{_api_html(ep.get("api"))}</td>
        <td>{models_cell}</td>
      </tr>""")

    if not endpoints:
        rows.append("""
      <tr><td colspan="5" style="color:#888">нет данных (см. примечание ниже)</td></tr>""")

    if refresh is None:
        footer = (
            '<p style="color:#888">Список провайдеров и API-эндпойнты бэкендов '
            "проверяются по кнопке «⟳ Перепроверить» (перечитывание "
            "ADAPTER_BACKEND_CONFIG + GET /v1/models + дымовые POST "
            "max_tokens:1, таймаут 10 с на эндпойнт; проба кэшируется 60 с, "
            "ADAPTER_ENDPOINT_PROBE=0 — отключить). Первый заход на страницу "
            "запускает первую проверку автоматически; повторные — только по "
            "кнопке.</p>"
        )
    else:
        count = refresh.get("count", 0)
        providers = refresh.get("providers", len(config._BACKENDS))
        if refresh.get("ok"):
            base = (
                f'<span style="color:#1a7f37">Список провайдеров обновлён '
                f"в {html.escape(checked_at or '')} ({providers} провайдеров, "
                f"{count} моделей).</span>"
            )
        else:
            base = (
                f'<span style="color:#c0392b">Не удалось обновить список провайдеров '
                f"в {html.escape(checked_at or '')} — показан прежний список "
                f"({providers} провайдеров, {count} моделей).</span>"
            )
        if errors:
            details = "<br>".join(
                f"{html.escape(name)}: {html.escape(str(text))}" for name, text in errors.items()
            )
            footer = f'<p style="color:#555">{base} Ошибки:<br>{details}</p>'
        else:
            footer = f'<p style="color:#555">{base}</p>'

    note_html = (
        f'<p style="color:#b8860b">{html.escape(snapshot["note"])}</p>' if snapshot["note"] else ""
    )

    # Баннер «проверка выполняется» + JS поллинга /api/refresh-state.
    # Вставляются только при running=True: без идущей проверки поллинга
    # нет (страница не перезагружается сама по себе). JS опрашивает
    # состояние каждые 2 с; увидев завершение (running=false, done_at
    # есть) — перезагружает страницу, чтобы показать свежий результат.
    banner_html = ""
    poll_script = ""
    if running:
        started = time.strftime("%H:%M:%S", time.localtime(started_at)) if started_at else ""
        banner_html = (
            '<p style="background:#fff8e1;border:1px solid #e0c060;'
            f'padding:8px 12px;color:#8a6d1a">⟳ Проверка выполняется'
            f"{(' (запущена в ' + started + ')') if started else ''}… "
            "модели и API-эндпойнты пере-проверяются в фоне, страница "
            "обновится автоматически по завершении.</p>"
        )
        poll_script = """
<script>
  function status_poll() {{
    fetch("/api/refresh-state")
      .then(function (r) {{ return r.json(); }})
      .then(function (s) {{
        if (!s.running && s.done_at) {{ location.reload(); }}
        else {{ setTimeout(status_poll, 2000); }}
      }})
      .catch(function () {{ setTimeout(status_poll, 2000); }});
  }}
  window.addEventListener("load", status_poll);
</script>
"""

    # Баннер «перепроверка строки идёт» + JS поллинга /api/model-usage/
    # reprobe-state. По образцу баннера проверки бэкендов выше, но состояние
    # живёт в model_usage (reprobe_state), а не в config.refresh_state():
    # перепроверка запускается кнопкой «Перепроверить» строки таблицы
    # использованных моделей, когда её колонки эндпоинтов «—». Пока идёт —
    # ячейка действий строки показывает «проверяется…» (см. _actions_cell_html)
    # и JS перезагружает страницу по завершении.
    reprobe = model_usage.reprobe_state()
    reprobing = None
    reprobe_banner_html = ""
    reprobe_poll_script = ""
    if reprobe.get("running"):
        reprobing = {reprobe.get("model"): True}
        started = (
            time.strftime("%H:%M:%S", time.localtime(reprobe["started_at"]))
            if reprobe.get("started_at")
            else ""
        )
        model_txt = html.escape(str(reprobe.get("model") or ""))
        reprobe_banner_html = (
            '<p style="background:#eef4fb;border:1px solid #9db8d9;'
            f'padding:8px 12px;color:#34506e">⟳ Перепроверка модели '
            f"<code>{model_txt}</code>{(' (запущена в ' + started + ')') if started else ''}… "
            "эндпоинты пере-проверяются этой моделью в фоне, страница "
            "обновится автоматически по завершении.</p>"
        )
        reprobe_poll_script = """
<script>
  function reprobe_poll() {{
    fetch("/api/model-usage/reprobe-state")
      .then(function (r) {{ return r.json(); }})
      .then(function (s) {{
        if (!s.running) {{ location.reload(); }}
        else {{ setTimeout(reprobe_poll, 2000); }}
      }})
      .catch(function () {{ setTimeout(reprobe_poll, 2000); }});
  }}
  window.addEventListener("load", reprobe_poll);
</script>
"""

    # Live-счётчики секции Models in use: JS usage_poll каждые 5 с опрашивает
    # лёгкий /api/model-usage/snapshot (model_usage.usage_snapshot() — копии
    # строк из памяти, сети к бэкендам нет) и обновляет ТОЛЬКО ячейки
    # счётчиков (Вызовов/Input/Output) — без перезагрузки страницы. Строки
    # сопоставляются ПОЗИЦИОННО: и рендер, и снимок идут в порядке первого
    # обращения (usage_snapshot), поэтому экранирование имён не мешает.
    # Число строк изменилось (строка сброшена/добавлена) либо в таблице
    # вообще нет строк — location.reload() перерисует таблицу целиком
    # (редкое событие; reprobe перерисовывает страницу сам через reprobe_poll).
    # Скрипт безусловный (в отличие от status_poll/reprobe_poll): поллинг
    # нужен всегда, когда на странице есть таблица. Оверхед — один маленький
    # JSON-ответ раз в 5 с на открытую вкладку; при скрытой вкладке браузер
    # сам троттлит setTimeout (≥1/мин) — трафика нет.
    usage_poll_script = """
<script>
  function compact_fmt(n) {{
    if (n >= 1000000000) {{
      var b = Math.floor(n / 100000000);
      return Math.floor(b / 10) + "b" + (b % 10);
    }} else if (n >= 1000000) {{
      var m = Math.floor(n / 100000);
      return Math.floor(m / 10) + "m" + (m % 10);
    }} else if (n >= 1000) {{
      var k = Math.floor(n / 100);
      return Math.floor(k / 10) + "k" + (k % 10);
    }}
    return String(n);
  }}
  function usage_poll() {{
    fetch("/api/model-usage/snapshot")
      .then(function (r) {{ return r.json(); }})
      .then(function (rows) {{
        var trs = document.querySelectorAll("tr[id^='usage-row-']");
        if (trs.length !== rows.length) {{ location.reload(); return; }}
        for (var i = 0; i < trs.length; i++) {{
          var row = rows[i];
          var cells = trs[i].getElementsByTagName("td");
          // Колонки: 0 Модель, 1 Бэкенд, 2 Вызовов, 3 Input, 4 Output
          var set = function (idx, val) {{
            if (cells[idx] && String(cells[idx].textContent) !== String(val)) {{
              cells[idx].textContent = val;
            }}
          }};
          set(2, row["calls"]);
          set(3, compact_fmt(row["input_tokens"]));
          set(4, compact_fmt(row["output_tokens"]));
        }}
        setTimeout(usage_poll, 5000);
      }})
      .catch(function () {{ setTimeout(usage_poll, 5000); }});
  }}
  window.addEventListener("load", function () {{ setTimeout(usage_poll, 5000); }});
</script>
"""

    html_page = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<title>backend-adapter — статус</title>
<style>
  body {{ font-family: -apple-system, Segoe UI, Arial, sans-serif; margin: 24px; color: #222; }}
  table {{ border-collapse: collapse; margin-top: 12px; }}
  td, th {{ border: 1px solid #ddd; padding: 6px 12px; text-align: left; vertical-align: top; }}
  th {{ background: #f5f5f5; }}
  code {{ font-size: 13px; }}
</style>
<script>
  function models_toggle(btn) {{
    var span = btn.previousElementSibling;
    if (span && span.classList.contains("models-extra")) {{
      var expanded = span.style.display !== "none";
      span.style.display = expanded ? "none" : "block";
      btn.textContent = expanded
        ? "Показать ещё (" + (btn.dataset.modelsCount - {MODEL_LINES}) + ")"
        : "Свернуть";
    }}
  }}
</script>
{poll_script}
{reprobe_poll_script}
{usage_poll_script}</head>
<body>
<h2>Backend-Adapter — статус</h2>
<p><b>Версия кода:</b> {html.escape(context.version)} &nbsp;·&nbsp;
   <a href="/session">просмотр сессий →</a> &nbsp;·&nbsp;
   <a href="/config">runtime config →</a></p>
{note_html}
{banner_html}
{reprobe_banner_html}
<table>
  <tr><th>Backend</th><th>Base URL</th><th>Status</th><th>Endpoints</th><th>Models</th></tr>
  {"".join(rows)}
</table>
{footer}
<form method="POST" action="/" style="margin-top:12px">
  <button type="submit">⟳ Перепроверить</button>
</form>
<h3 style="margin-top:24px">Models in use</h3>
<table>
  <tr><th>Модель</th><th>Бэкенд</th><th>Вызовов</th><th>Input</th><th>Output</th><th>Cost</th><th>Endpoints</th><th>Действия</th></tr>
  {_usage_rows_html(model_usage.usage_snapshot(), reprobing)}
</table>
<p style="color:#888;margin-top:12px;font-size:13px">
  <a href="https://github.com/alekseybb197/backend-adapter">backend-adapter на GitHub</a>
</p>
</body>
</html>
"""
    return html_page.encode("utf-8")


# ==================== ЭНДПОЙНТ ====================


@webserver.register
class StatusEndpoint(webserver.Endpoint):
    """Эндпойнт "/": статус-страница (версия, эндпойнты LLM, API, модели).

    Проверка бэкендов — по кнопке «⟳ Перепроверить» (POST "/") и при
    автостарте (первый GET, см. _autostart_first_check). POST работает по
    PRG-паттерну: запускает ФОНОВУЮ проверку config.start_refresh(timeout=
    PROBE_TIMEOUT) и отвечает 303 See Other на GET "/" (HTTP не ждёт её
    завершения; браузер переходит на страницу GET-навигацией, поэтому
    авто-релоад не повторяет POST). Кнопка ПЕРЕЧИТЫВАЕТ ADAPTER_BACKEND_CONFIG
    (start_refresh(reload=True) → config.reload_backend_config): бэкенды
    добавляются/удаляются без рестарта адаптера; битый YAML — прежние
    остаются ([WARN]), проверка идёт по ним. Загрузка страницы (GET "/")
    показывает состояние последней проверки (config.refresh_state). Пока
    проверка идёт, страница показывает баннер и авто-обновляется по
    завершении (JS status_poll → /api/refresh-state → location.reload()).
    Периодического фонового refresh нет — только явный POST или автостарт."""

    prefix = "/"

    def __init__(self, context):
        self.context = context

    def _render_from_state(self):
        """Отрендерить страницу из текущего состояния проверки.

        Если настроенных эндпойнтов нет (standalone без env-бэкенда) —
        проверку запускать нечего: страница рендерится с подсказкой."""
        state = config.refresh_state()
        return _render_status_page(
            self.context,
            refresh=_last_result(state),
            checked_at=state.get("checked_at"),
            running=state.get("running"),
            started_at=state.get("started_at"),
        )

    def GET(self, handler, remainder: str):
        if remainder:
            handler.send_error(404, "Not found")
            return
        # Первый заход на страницу запускает первую проверку автоматически
        # (см. _autostart_first_check); _render_from_state после вызова
        # отрендерит баннер, если проверка реально стартовала.
        _autostart_first_check()
        handler._write(200, "text/html; charset=utf-8", self._render_from_state())

    def POST(self, handler, remainder: str):
        # Кнопка «⟳ Перепроверить»: PRG-паттерн — запускаем фоновую
        # проверку (с перечитыванием ADAPTER_BACKEND_CONFIG) и отвечаем
        # 303 See Other на GET "/", чтобы браузер перешёл на неё
        # GET-навигацией. Иначе авто-обновление страницы
        # (location.reload()) повторяло бы POST, а браузер спрашивал бы
        # «повторить действие?» (диалог Chrome/Firefox). Если проверка уже
        # идёт — start_refresh вернёт False; редирект всё равно уводит на
        # GET, который отрендерит баннер. В standalone без настроенных
        # эндпойнтов проверять нечего — не запускаем (GET покажет подсказку).
        if _collect_endpoints():
            config.start_refresh(timeout=PROBE_TIMEOUT)
        handler._redirect("/")


@webserver.register
class RefreshStateEndpoint(webserver.Endpoint):
    """Эндпойнт "/api/refresh-state": состояние фоновой проверки (JSON).

    Лёгкий ответ для JS status_poll на статус-странице: config.refresh_state()
    — {"running", "started_at", "done_at", "ok", "count", "errors",
    "checked_at"}. GET проверку не запускает и ничего не мутирует —
    безопасно опрашивать каждые 2 с."""

    prefix = "/api/refresh-state"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        body = json.dumps(config.refresh_state()).encode("utf-8")
        handler._write(200, "application/json; charset=utf-8", body)


@webserver.register
class ModelUsageResetEndpoint(webserver.Endpoint):
    """POST /api/model-usage/reset?model=<имя> — обнуление счётчиков строки.

    Кнопка «Сбросить» в таблице использованных моделей (form method=post)
    работает по PRG-паттерну: обнуление + 303 See Other на GET "/" — страница
    показывается GET-навигацией, обновление не повторяет POST (как у
    кнопки «⟳ Перепроверить»). JSON-клиент (Content-Type:
    application/json) получает 200 {"ok": true, "model": ...} при успехе,
    404 {"error": ...} — строки нет, 400 {"error": ...} — нет query-
    параметра model (единый формат ошибки, как в server.py). GET на
    префикс — 404 дефолтом Endpoint."""

    prefix = "/api/model-usage/reset"

    def __init__(self, context):
        self.context = context

    def POST(self, handler, remainder: str):
        parsed = urlparse(handler.path)
        model = parse_qs(parsed.query).get("model", [""])[0].strip()
        ct = handler.headers.get("Content-Type", "")
        if "application/json" not in ct:
            # HTML-форма кнопки (application/x-www-form-urlencoded): PRG.
            if not model:
                handler._redirect("/")  # кнопка без model невозможна в норме
                return
            model_usage.reset_model(model)  # строка есть на живой странице
            handler._redirect("/")  # 303 → GET "/" (PRG)
            return
        if not model:
            body = b'{"error": "missing \'model\' query parameter"}'
            handler._write(400, "application/json; charset=utf-8", body)
            return
        if not model_usage.reset_model(model):
            body = json.dumps({"error": f"model '{model}' not in usage table"}).encode()
            handler._write(404, "application/json; charset=utf-8", body)
            return
        body = json.dumps({"ok": True, "model": model}).encode("utf-8")
        handler._write(200, "application/json; charset=utf-8", body)


@webserver.register
class ModelUsageReprobeEndpoint(webserver.Endpoint):
    """POST /api/model-usage/reprobe?model=<имя> — фоновая перепроверка
    эндпоинтов строки модели.

    Кнопка «Перепроверить» в таблице использованных моделей (form
    method=post) работает по PRG-паттерну: старт + 303 See Other на GET "/"
    — страница показывается GET-навигацией, обновление не повторяет POST.
    Проба идёт в фоновом потоке (model_usage.start_reprobe) — статус-страница
    отвечает мгновенно; пока перепроверка выполняется, строка показывает
    «проверяется…» и страница авто-обновляется по завершении (JS опрашивает
    /api/model-usage/reprobe-state). JSON-клиент (Content-Type:
    application/json) получает 202 {"ok": true, "model": ...} при запуске,
    404 {"error": ...} — строки нет / уже перепроверяется / идёт первичная
    проба, 400 {"error": ...} — нет query-параметра model (единый формат
    ошибки, как у reset). 202 — «запущено в фоне» (в отличие от 200-«готово»
    у reset). GET на префикс — 404 дефолтом Endpoint."""

    prefix = "/api/model-usage/reprobe"

    def __init__(self, context):
        self.context = context

    def POST(self, handler, remainder: str):
        parsed = urlparse(handler.path)
        model = parse_qs(parsed.query).get("model", [""])[0].strip()
        ct = handler.headers.get("Content-Type", "")
        if "application/json" not in ct:
            # HTML-форма кнопки (application/x-www-form-urlencoded): PRG.
            if not model:
                handler._redirect("/")  # кнопка без model невозможна в норме
                return
            model_usage.start_reprobe(model)  # строка есть на живой странице
            handler._redirect("/")  # 303 → GET "/" (PRG)
            return
        if not model:
            body = b'{"error": "missing \'model\' query parameter"}'
            handler._write(400, "application/json; charset=utf-8", body)
            return
        if not model_usage.start_reprobe(model):
            body = json.dumps(
                {"error": f"model '{model}' not in usage table or already reprobing"}
            ).encode()
            handler._write(404, "application/json; charset=utf-8", body)
            return
        body = json.dumps({"ok": True, "model": model}).encode("utf-8")
        handler._write(202, "application/json; charset=utf-8", body)


@webserver.register
class ReprobeStateEndpoint(webserver.Endpoint):
    """Эндпойнт "/api/model-usage/reprobe-state": состояние перепроверки (JSON).

    Лёгкий ответ для JS status_poll на статус-странице:
    model_usage.reprobe_state() — {"running", "model", "started_at"}. GET
    перепроверку не запускает и ничего не мутирует — безопасно опрашивать
    каждые 2 с (по образцу /api/refresh-state, но состояние живёт в
    model_usage, а не в config)."""

    prefix = "/api/model-usage/reprobe-state"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        body = json.dumps(model_usage.reprobe_state()).encode("utf-8")
        handler._write(200, "application/json; charset=utf-8", body)


@webserver.register
class UsageSnapshotEndpoint(webserver.Endpoint):
    """Эндпойнт "/api/model-usage/snapshot": снимок таблицы Models in use (JSON).

    Лёгкий ответ для JS usage_poll на статус-странице:
    model_usage.usage_snapshot() — список строк таблицы (calls/input_tokens/
    output_tokens/endpoints/…) в порядке первого обращения. GET ничего не
    мутирует (кроме ленивой загрузки таблицы при первом обращении) и не
    ходит в сеть к бэкендам — безопасно опрашивать каждые 5 с."""

    prefix = "/api/model-usage/snapshot"

    def __init__(self, context):
        self.context = context

    def GET(self, handler, remainder: str):
        body = json.dumps(model_usage.usage_snapshot()).encode("utf-8")
        handler._write(200, "application/json; charset=utf-8", body)


def _last_result(state: dict) -> dict | None:
    """refresh-срез состояния для _render_status_page (или None).

    Проверок ещё не было (ok/count/errors пусты) → None: футер показывает
    подсказку, а не «обновлено 0 моделей». Иначе — {"ok", "count",
    "providers", "errors"}: providers — число настроенных бэкендов после
    перечитывания конфига (len(_BACKENDS) на момент публикации финального
    снимка; None в старых снимках — футер возьмёт len(_BACKENDS) сам)."""
    if state.get("ok") is None and state.get("errors") is None:
        return None
    return {
        "ok": state.get("ok"),
        "count": state.get("count"),
        "providers": state.get("providers", len(config._BACKENDS)),
        "errors": state.get("errors"),
    }


def _autostart_first_check() -> bool:
    """Запустить фоновую проверку на первом GET "/", если проверок ещё не
    было и есть что проверять. Возвращает True, если запущена этим вызовом.

    Сценарии:
      - standalone (python -m backend_adapter.webserver) с YAML в
        ADAPTER_BACKEND_CONFIG: _BACKENDS пуст (адаптер не стартовал), но
        _collect_endpoints() вернёт список из YAML — первый GET запускает
        проверку, чтобы колонка Endpoints и модели заполнились без клика;
      - в процессе адаптера стартовую проверку уже запустил
        backend-adapter.py (running=True) — повторно не гоним;
      - проверка уже завершалась (done_at есть) — не гоним повторно:
        повторные проверки — только по кнопке «⟳ Перепроверить»."""
    if not _collect_endpoints():  # standalone без конфига — нечего проверять
        return False
    state = config.refresh_state()
    if state.get("running"):
        return False  # уже идёт (стартовая адаптера / по кнопке)
    if state.get("done_at") is not None:
        return False  # проверка уже завершалась — не гоним повторно
    return config.start_refresh(timeout=PROBE_TIMEOUT)


__all__ = [
    "PROBE_TIMEOUT",
    "MODEL_LINES",
    "_collect_endpoints",
    "_config_snapshot",
    "_models_html",
    "_api_html",
    "_endpoints_cell_html",
    "_cost_cell_html",
    "_usage_rows_html",
    "_actions_cell_html",
    "_fmt_tokens",
    "_compact_number",
    "_fmt_cost",
    "_render_status_page",
    "StatusEndpoint",
    "RefreshStateEndpoint",
    "ModelUsageResetEndpoint",
    "ModelUsageReprobeEndpoint",
    "ReprobeStateEndpoint",
    "UsageSnapshotEndpoint",
    "_last_result",
    "_autostart_first_check",
]
