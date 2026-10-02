#!/usr/bin/env python3
"""Tests for backend_adapter.templates — встроенные шаблоны настроек (v0.9.12).

Шаблоны ``adapter.yaml``/``tariffs.yaml`` лежат и в коде (``templates.py``, из
него их берёт ``--install`` — так они доступны и из бинарника), и файлами-
справочниками в ``docs/samples/``. Тест сторожит расхождение: любая правка
одного места обязана попасть и во второе. Там, где ``docs/samples/`` нет
(запуск из бинарника/wheel), сверка пропускается — это не ошибка.
"""

import os
from pathlib import Path

import pytest

from backend_adapter import templates

# Эталон — репозиторные образцы. Путь от этого файла: tests/ → корень → docs/samples.
_SAMPLES = Path(__file__).resolve().parent.parent / "docs" / "samples"

_REFERENCE = {
    "sample.adapter.yaml": "SAMPLE_ADAPTER_YAML",
    "sample.tariffs.yaml": "SAMPLE_TARIFFS_YAML",
}


@pytest.mark.parametrize("filename,attr", sorted(_REFERENCE.items()))
def test_embedded_matches_reference(filename, attr):
    """Встроенный шаблон совпадает с docs/samples байт-в-байт."""
    reference = _SAMPLES / filename
    if not reference.is_file():
        pytest.skip(f"docs/samples/{filename} недоступен (запуск не из репозитория)")
    assert getattr(templates, attr) == reference.read_text(encoding="utf-8")


def test_embedded_yaml_ends_with_newline():
    """Отсутствие хвостового \\n — типичная ошибка при переносе в строку."""
    assert templates.SAMPLE_ADAPTER_YAML.endswith("\n")
    assert templates.SAMPLE_TARIFFS_YAML.endswith("\n")


class TestRenderEnv:
    def test_contains_absolute_paths_from_root(self, tmp_path):
        root = str(tmp_path / "home")
        text = templates.render_env(root)
        # os.path.join — идиома кроссплатформенная (разделитель зависит от ОС).
        assert f"ADAPTER_BACKEND_CONFIG='{os.path.join(root, 'adapter.yaml')}'" in text
        assert f"ADAPTER_MODELS_TARIFFS='{os.path.join(root, 'tariffs.yaml')}'" in text
        assert f"ADAPTER_DATA_ROOT='{os.path.join(root, 'tmp', 'adapter')}'" in text

    def test_token_placeholder_and_key_defaults(self, tmp_path):
        text = templates.render_env(str(tmp_path / "home"))
        # Токен — заглушка, не пустое значение.
        assert "ADAPTER_BACKEND_KEY_LLM_SERVICE='*****'" in text
        # Несколько дефолтов, сверяемых с config.py (регрессия при рассинхроне).
        assert "export ADAPTER_PROXY_PORT=9999" in text
        assert "export ADAPTER_ENDPOINT_HOST=\"127.0.0.1\"" in text
        assert "export ADAPTER_DEBUG_TRIM=3000" in text
        assert "export ADAPTER_WEBUI_PORT=8765" in text

    def test_paths_follow_root(self, tmp_path):
        """Разные root дают разные абсолютные пути (файл не захардкожен)."""
        a = templates.render_env(str(tmp_path / "a"))
        b = templates.render_env(str(tmp_path / "b"))
        assert a != b
        assert str(tmp_path / "a") in a
        assert str(tmp_path / "b") in b
