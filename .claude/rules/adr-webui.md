---
paths:
  - "backend_adapter/{webserver,session_viewer,prometheus_exporter,artifact_refresh}.py"
  - "backend_adapter/webui_*.py"
  - "backend_adapter/artifact_tree*.py"
  - "tests/test_{webserver,session_viewer,prometheus_exporter,artifact_refresh,artifact_tree}.py"
  - "tests/test_webui_*.py"
  - "docs/webui.md"
---

# WEBUI и экспортёр — действующие решения (docs/adr/)

- 023 — WEBUI — поток внутри процесса адаптера; новый эндпойнт = модуль с
  `@webserver.register` + импорт в `webserver.serve()`; изменяющие действия —
  POST + 303 (PRG); экспортёр — отдельный слушатель.
- 024 — привязка к `127.0.0.1` по умолчанию; это сейчас единственная мера доступа.
- 027 — тяжёлая отрисовка артефактов только в фоне (`artifact_refresh`), никогда в
  пути запроса прокси.

ВНИМАНИЕ (подтверждено, ADR-024/040): WEBUI уязвим для CSRF и DNS rebinding —
изменяющие POST не проверяют `Origin`, чужой `Host` обслуживается. Новые
изменяющие эндпойнты не добавлять до реализации ADR-040 (слои 1–2), либо
проверять `Origin`/`Host` в самом эндпойнте.

Предложены: 032 (`/metrics` на слушателе WEBUI), 040 (Host/Origin/basic auth).
