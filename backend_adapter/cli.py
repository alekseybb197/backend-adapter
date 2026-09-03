"""cli.py — консольный entry point пакета (pyproject [project.scripts]).

Сама программа живёт в скрипте ``backend-adapter.py`` уровнем выше пакета
(там объявлены ``__version__`` и ``__comment__`` — единственный источник
версии, который читает регэкспом и ``webserver._detect_version()``).
Модуль не может импортировать этот скрипт напрямую: имя с дефисом не
валидный идентификатор Python (``import backend-adapter`` не работает),
а файл не попадает в wheel (туда входит только пакет ``backend_adapter/``).
Поэтому установленный консольный скрипт ``backend-adapter`` (генерируется
setuptools/hatchling как ``from backend_adapter.cli import main``) зовёт
``main()``, а та исполняет файл скрипта как ``__main__`` через
``runpy.run_path`` — тот же приём-трамплин, что у ``webserver`` при
``python -m`` (см. комментарий внизу webserver.py про двойное исполнение
runpy: namespace ``__main__`` получает свежие глобалы скрипта, а
``backend-adapter.py`` при ``python backend-adapter.py`` сам исполняется
как ``__main__``, поэтому тройного запуска не возникает).

Итог: и ``python backend-adapter.py``, и установленная команда
``backend-adapter`` дают один и тот же процесс, а версия остаётся в одном
месте — в ``backend-adapter.py``.
"""

import os
import runpy
import sys

_SCRIPT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend-adapter.py"
)


def main() -> None:
    """Запустить backend-adapter.py как __main__ (runpy-трамплин).

    run_path исполняет скрипт в отдельном namespace (как ``python
    backend-adapter.py``): модульный ``if __name__ == "__main__"``
    срабатывает, конфиг-глобалы импортируются из ``backend_adapter.config``
    и процесс уходит в ``serve_forever()``. Возврата из run_path нет —
    процесс завершается внутри (Ctrl+C / SIGTERM), как при ручном запуске.
    """
    if not os.path.isfile(_SCRIPT_PATH):
        # Wheel-установка (`pip install .`) не содержит backend-adapter.py —
        # в wheel входит только пакет backend_adapter/. Для репозитория это
        # не проблема (запуск — из исходников: `python backend-adapter.py`,
        # `pip install -e .`, systemd/launchd на путь репозитория), но даём
        # внятную ошибку вместо голого FileNotFoundError из runpy.
        raise FileNotFoundError(
            f"Не найден скрипт запуска: {_SCRIPT_PATH}. "
            "Установите пакет из исходников репозитория "
            "(`pip install -e .`) или запускайте backend-adapter.py напрямую."
        )
    sys.path.insert(0, os.path.dirname(_SCRIPT_PATH))
    runpy.run_path(_SCRIPT_PATH, run_name="__main__")
