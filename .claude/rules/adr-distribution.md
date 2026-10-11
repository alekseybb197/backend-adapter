---
paths:
  - "install.sh"
  - "scripts/**"
  - ".github/workflows/**"
  - "molecule/**"
  - "pyproject.toml"
  - "requirements*.txt"
---

# Поставка и процесс — действующие решения (docs/adr/)

- 005 — runtime-зависимость одна: PyYAML. Новая runtime-зависимость — только
  через ADR «Предложено».
- 028 — standalone-бинарник PyInstaller `--onefile`, один путь установки
  (`install.sh` → бинарник последнего релиза). Код должен работать из бинарника:
  никаких рантайм-чтений `docs/`.
  ДЕФЕКТ: цель `linux-arm64` собирается на x86-64-раннере и публикует x86-64.
  Решение автора (нужна цель или нет) ещё не принято — не исправлять самостоятельно.
- 030 — четыре проверки (ruff check, ruff format --check, mypy --strict, pytest);
  CI повторяет их; molecule-джоб — по path-гейту. CI не нагружать лишними шагами.
