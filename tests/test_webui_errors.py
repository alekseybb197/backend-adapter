#!/usr/bin/env python3
"""Unit-тесты backend_adapter.webui_errors.parse_err_sections — чистый разбор
.err-файла сессии на секции (v0.9.8).

HTTP-часть эндпойнта /errors проверяется в tests/test_webui_sessions.py
(TestErrorsPreviewEndpoint) — там уже живут хелперы запуска сервера и сырых
запросов. Здесь — только разбор текста: делимитеры, незакрытые секции,
преамбула, многострочные тела, поля шапки.

Формат блоков — ровно тот, что пишет session_log (write_error_file,
write_session_error, write_warn_file).
"""
import pytest

# Autouse fresh_env удаляет backend_adapter* и переимпортирует config —
# parse_err_sections от env не зависит, но модуль берём ВНУТРИ тестов (как
# соседний test_webui_sessions.py): свежий объект после перезагрузки.


ERR_OPEN = "==================== ERROR ===================="
ERR_CLOSE = "==================== END ERROR ===================="
WARN_OPEN = "==================== WARNING ===================="
WARN_CLOSE = "==================== END WARNING ===================="


def _parse(text):
    """Разбор через СВЕЖИЙ модуль (autouse fresh_env перезагружает пакет)."""
    from backend_adapter import webui_errors as fresh

    return fresh.parse_err_sections(text)


def _error_block(req_id="r-1", status=502, backend_url="http://127.0.0.1:8002"):
    return [
        ERR_OPEN,
        f"[2026-09-12T10:10:10] [{req_id}] session_id=sess-abcdef12 "
        f"final_status={status} model=m-a backend_url={backend_url}",
        f'[2026-09-12T10:10:10] [{req_id}] [REQUEST] {{"model": "m-a"}}',
        f"[2026-09-12T10:10:10] [{req_id}] [BACKEND_ERROR] boom",
        ERR_CLOSE,
    ]


def _text(lines):
    return "\n".join(lines) + "\n"


def test_empty_file_has_no_sections():
    assert _parse("") == []
    assert _parse("\n\n") == []


def test_single_error_section_fields():
    sections = _parse(_text(_error_block()))
    assert len(sections) == 1
    sec = sections[0]
    assert sec["n"] == 1
    assert sec["kind"] == "ERROR"
    assert sec["closed"] is True
    assert sec["kind_mismatch"] is False
    assert sec["ts"] == "2026-09-12T10:10:10"
    assert sec["req_id"] == "r-1"
    assert sec["fields"] == {
        "session_id": "sess-abcdef12",
        "final_status": "502",
        "model": "m-a",
        "backend_url": "http://127.0.0.1:8002",
    }
    assert sec["body_lines"] == [
        ("REQUEST", '{"model": "m-a"}'),
        ("BACKEND_ERROR", "boom"),
    ]
    # summary — последняя запись тела (диагноз), а не шумный [REQUEST]
    assert sec["summary"] == "[BACKEND_ERROR] boom"
    assert sec["raw"].startswith(ERR_OPEN + "\n")
    assert sec["raw"].endswith(ERR_CLOSE + "\n")


def test_warning_section_has_no_final_status():
    lines = [
        WARN_OPEN,
        "[2026-09-12T10:10:11] [r-2] session_id=sess-abcdef12 model=m-a "
        "backend_url=http://127.0.0.1:8002",
        '[2026-09-12T10:10:11] [r-2] [REQUEST] {"model": "m-a"}',
        "[2026-09-12T10:10:11] [r-2] [WARN] First message is NOT system",
        WARN_CLOSE,
    ]
    sections = _parse(_text(lines))
    assert len(sections) == 1
    sec = sections[0]
    assert sec["kind"] == "WARNING"
    # у WARNING нет final_status — поле остаётся пустым, а не отсутствует
    assert sec["fields"]["final_status"] == ""
    assert sec["fields"]["backend_url"] == "http://127.0.0.1:8002"
    assert sec["summary"] == "[WARN] First message is NOT system"


def test_adapter_error_has_no_backend_url():
    lines = [
        ERR_OPEN,
        "[2026-09-12T10:10:12] [r-3] session_id=sess-abcdef12 final_status=400 model=m-a",
        '[2026-09-12T10:10:12] [r-3] [REQUEST] {"bad": true}',
        "[2026-09-12T10:10:12] [r-3] [ADAPTER_ERROR] Invalid JSON",
        ERR_CLOSE,
    ]
    sec = _parse(_text(lines))[0]
    assert sec["fields"]["backend_url"] == ""
    assert sec["fields"]["final_status"] == "400"
    assert sec["summary"] == "[ADAPTER_ERROR] Invalid JSON"


def test_two_sections_numbered_in_file_order():
    text = _text(_error_block(req_id="r-1")) + _text(
        [
            WARN_OPEN,
            "[2026-09-12T10:10:11] [r-2] session_id=sess-abcdef12 model=m-a "
            "backend_url=http://127.0.0.1:8002",
            "[2026-09-12T10:10:11] [r-2] [WARN] warn",
            WARN_CLOSE,
        ]
    )
    sections = _parse(text)
    assert [s["n"] for s in sections] == [1, 2]
    assert [s["kind"] for s in sections] == ["ERROR", "WARNING"]


def test_unclosed_section_at_eof():
    # Обрыв записи: файл дописывается построчно, закрывающего может не быть.
    lines = _error_block()[:-1]
    sec = _parse(_text(lines))[0]
    assert sec["closed"] is False
    assert sec["kind_mismatch"] is False
    assert sec["summary"] == "[BACKEND_ERROR] boom"


def test_unclosed_section_terminated_by_next_open():
    lines = _error_block()[:-1] + [WARN_OPEN, "[t] [r] [WARN] x", WARN_CLOSE]
    sections = _parse(_text(lines))
    assert len(sections) == 2
    assert sections[0]["kind"] == "ERROR" and sections[0]["closed"] is False
    assert sections[1]["kind"] == "WARNING" and sections[1]["closed"] is True


def test_kind_mismatch_on_foreign_close():
    # END не того вида: секция закрыта, но с пометкой.
    lines = _error_block()[:-1] + [WARN_CLOSE]
    sec = _parse(_text(lines))[0]
    assert sec["kind"] == "ERROR"
    assert sec["closed"] is True
    assert sec["kind_mismatch"] is True


def test_preamble_before_first_delimiter():
    text = "garbage line\n\n" + _text(_error_block())
    sections = _parse(text)
    assert [s["kind"] for s in sections] == ["PREAMBLE", "ERROR"]
    # преамбула участвует в общей нумерации — она первая
    assert [s["n"] for s in sections] == [1, 2]
    assert sections[0]["header"] == "garbage line"
    assert sections[0]["raw"] == "garbage line\n\n"


def test_blank_lines_only_are_not_preamble():
    assert _parse("\n\n" + _text(_error_block()))[0]["kind"] == "ERROR"


def test_multiline_body_is_kept_in_raw():
    # _err_body_text не срезает переводы строк: тело запроса многострочно,
    # продолжение клеится к записи [REQUEST], а raw хранит точный срез.
    lines = [
        ERR_OPEN,
        "[2026-09-12T10:10:10] [r-1] session_id=sess-abcdef12 final_status=502 "
        "model=m-a backend_url=http://127.0.0.1:8002",
        "[2026-09-12T10:10:10] [r-1] [REQUEST] {",
        '  "model": "m-a"',
        "}",
        "[2026-09-12T10:10:10] [r-1] [BACKEND_ERROR] boom",
        ERR_CLOSE,
    ]
    sec = _parse(_text(lines))[0]
    assert len(sec["body_lines"]) == 2
    tag, body = sec["body_lines"][0]
    assert tag == "REQUEST"
    assert body == '{\n  "model": "m-a"\n}'
    assert sec["summary"] == "[BACKEND_ERROR] boom"
    assert '  "model": "m-a"' in sec["raw"]


def test_raw_is_exact_source_slice():
    text = _text(_error_block())
    sec = _parse(text)[0]
    assert sec["raw"] == text


@pytest.mark.parametrize("kind", ["ERROR", "WARNING"])
def test_delimiter_constants_match_session_log(kind):
    from backend_adapter import webui_errors
    # Строки делимитеров — контракт с session_log: разъедутся — разбор молча
    # перестанет видеть секции, поэтому фиксируем их в тесте.
    assert webui_errors._ERR_OPEN[kind] == {"ERROR": ERR_OPEN, "WARNING": WARN_OPEN}[kind]
    assert webui_errors._ERR_CLOSE[kind] == {"ERROR": ERR_CLOSE, "WARNING": WARN_CLOSE}[kind]
