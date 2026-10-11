# ADR-032: `/metrics` на слушателе WEBUI; отдельный порт — только по явному выбору

| Поле | Значение |
|---|---|
| Статус | Предложено (2026-10-11); заменит часть ADR-023 |
| Тип решения | proposal |
| Повод | ответ автора на вопрос 10: порт 9100 выбран без учёта конфликта, «принимаю предложения» |

## Контекст

Экспортёр включён по умолчанию (`ADAPTER_EXPORTER_ENABLE=1`) на порту 9100.
По реестру портов Prometheus (wiki «Default port allocations») 9100 — порт
Node exporter, самого распространённого экспортёра. На хосте с ним адаптер
печатает `[EXPORTER] Failed to bind` и работает без метрик.

Отдельный слушатель был выбран в v0.8.5 с обоснованием «повторный `serve()`
перезаписал бы `Handler.context/endpoints`». Это ограничение второго вызова
`serve()`, а не регистрации ещё одного эндпойнта в первом: реестр
`@webserver.register` для этого и создан.

## Варианты

| Вариант | Конфликт портов | Новые порты | Поведение при basic auth (ADR-040) |
|---|---|---|---|
| **A. `/metrics` на WEBUI (8765), отдельный слушатель — только при явном `ADAPTER_EXPORTER_PORT`** | исчезает как класс | 0 | скрейп с `basic_auth` в Prometheus, либо `/metrics` в списке исключений |
| B. Оставить отдельный слушатель, сменить порт по умолчанию | остаётся вероятностным | 1 | независим |
| C. Экспортёр выключен по умолчанию | исчезает | 0/1 | — |

## Предлагаемое решение — A

1. Новый эндпойнт в `prometheus_exporter.py`, тот же рендер:

   ```python
   @webserver.register
   class MetricsEndpoint(webserver.Endpoint):
       """/metrics на общем WEBUI-слушателе (ADR-032)."""

       prefix = "/metrics"

       def __init__(self, context):
           self.context = context

       def GET(self, handler, remainder):
           if remainder:
               handler.send_error(404, "Not found")
               return
           MetricsHandler.version = self.context.version
           body = _render_metrics().encode("utf-8")
           handler._write(200, _METRICS_CONTENT_TYPE, body)
   ```

   и одна строка импорта в `webserver.serve()`, как у прочих эндпойнтов.
   Монтируется только при `ADAPTER_EXPORTER_ENABLE=1`; иначе 404.
2. `serve_exporter(...)` на отдельном порту запускается, **только если
   `ADAPTER_EXPORTER_PORT` задан явно** (непустой env). Дефолт `9100`
   удаляется из `config.py`; кто полагался на него, задаёт порт явно
   (миграционная заметка в changelog).
3. Prometheus:
   ```yaml
   scrape_configs:
     - job_name: backend-adapter
       static_configs: [{targets: ["127.0.0.1:8765"]}]
       metrics_path: /metrics
       # при включённом basic auth (ADR-040):
       # basic_auth: {username: prom, password_file: /etc/prometheus/ba.pass}
   ```

## Следствия

- Один слушатель для UI, health и метрик; один адрес привязки и одна
  политика доступа.
- Метрики подпадают под проверки `Host`/auth из ADR-040. Для Kubernetes
  это стандартная схема (scrape с учётными данными); если она неудобна,
  `/metrics` можно добавить в список исключений из аутентификации, но **не**
  из проверки `Host`.
- Строка `[EXPORTER] http://…:9100/metrics` в баннере меняется на путь WEBUI.

## Если выбран B

Порт нужно выбрать по реестру и зарегистрировать там же (wiki требует
публичный репозиторий — он есть). Предварительно следует проверить
актуальный список: в доступной части реестра (до 9505) свободных значений
не отмечено, но последняя часть страницы получена не была.
