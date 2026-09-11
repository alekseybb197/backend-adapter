"""Tests for backend_adapter.routing — input-endpoint routing (v0.9.0).

Три входных POST-эндпоинта (/v1/messages, /v1/chat/completions, /v1/responses)
управляются тремя TARGET-переменными (config.ADAPTER_*_TARGET). Модуль
routing — чистая логика решений по кэшу проб (config.endpoint_support):
сети в decide НЕТ, что здесь и проверяется.

Каждый тест получает свежие модули: autouse-фикстура isolate_logs
(→ fresh_env) пересоздаёт backend_adapter-модули из env-дефолтов conftest
(zero-config TARGET: messages=completions, completions=none, responses=none),
а setup_method каждого класса делает reload ещё раз — класс держит свои
self.config/self.routing (тот же паттерн, что tests/test_config.py:
module-level импорт не переживает reload между тестами).
"""
import sys
from unittest import mock

import pytest


def _reload_modules():
    """Remove backend_adapter modules from sys.modules and reimport fresh."""
    for name in list(sys.modules):
        if name.startswith("backend_adapter"):
            del sys.modules[name]
    from backend_adapter import config, routing

    return config, routing


class _RouteCase:
    """Base: per-test fresh modules + helpers to manipulate TARGET/кэш проб."""

    def setup_method(self):
        self.config, self.routing = _reload_modules()

    def _set_target(self, **targets: str) -> None:
        """Сменить TARGET-константы config на время одного теста."""
        for var, value in targets.items():
            assert var in ("messages", "completions", "responses"), var
            setattr(self.config, f"ADAPTER_{var.upper()}_TARGET", value)

    def _support(self, backend: str, pname: str, found: bool | None) -> None:
        """Наполнить config._ENDPOINT_STATE результатом пробы (как фоновая
        probe_endpoints / пер-модельные пробы usage-таблицы).

        found=None — «не пробовано»: кэш не трогаем (endpoint_support
        вернёт None → routing трактует как False)."""
        if found is None:
            return
        path = next(
            (p for n, p, _t in self.config.ENDPOINT_PROBES if n == pname), None
        )
        assert path is not None, pname
        state = self.config._ENDPOINT_STATE.setdefault(
            backend, {"at": 0.0, "endpoints": {}, "errors": {}}
        )
        state["endpoints"][path] = {"status": 200 if found else 404, "found": found}


# ---------------------------------------------------------------------------
# input_path_to_format
# ---------------------------------------------------------------------------

class TestInputPathToFormat(_RouteCase):
    def test_messages(self):
        assert self.routing.input_path_to_format("/v1/messages") == "messages"

    def test_messages_with_query(self):
        assert self.routing.input_path_to_format("/v1/messages?foo=1") == "messages"

    def test_chat_completions(self):
        assert self.routing.input_path_to_format("/v1/chat/completions") == "completions"

    def test_responses(self):
        assert self.routing.input_path_to_format("/v1/responses") == "responses"

    def test_non_input_paths(self):
        # Пути вне трёх входных эндпоинтов (в т.ч. с «похожим» префиксом —
        # /v1/messages/…, /v1/chat/completions/…) — не входы адаптера.
        assert self.routing.input_path_to_format("/") is None
        assert self.routing.input_path_to_format("/v1/embeddings") is None
        assert self.routing.input_path_to_format("/v1/messages/extra") is None
        assert self.routing.input_path_to_format("/v1/messagesx") is None
        assert self.routing.input_path_to_format("/v1/chat/completions/extra") is None
        assert self.routing.input_path_to_format("/v1/responses/") is None


# ---------------------------------------------------------------------------
# Config: TARGET-константы и их парсинг (zero-config дефолты из conftest)
# ---------------------------------------------------------------------------

class TestTargetConfig(_RouteCase):
    def test_zero_config_defaults(self):
        # Дефолты conftest: принимается только /v1/messages, конвертация в
        # chat completions; остальные два входа выключены (404).
        assert self.config.ADAPTER_MESSAGES_TARGET == "completions"
        assert self.config.ADAPTER_COMPLETIONS_TARGET == "none"
        assert self.config.ADAPTER_RESPONSES_TARGET == "none"

    def test_input_paths_mirror_endpoint_probes(self):
        # INPUT_PATHS — зеркало ENDPOINT_PROBES для трёх форматов роутинга.
        for fmt, path in self.routing.INPUT_PATHS.items():
            ep = next(
                (p for n, p, _t in self.config.ENDPOINT_PROBES if n == fmt), None
            )
            assert ep == path

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("messages", "messages"),
            ("MESSAGES", "messages"),
            ("  completions ", "completions"),
            ("Responses", "responses"),
            ("auto", "auto"),
            ("AUTO", "auto"),
            ("none", "none"),
            ("", "none"),  # пусто — безопасное выключение
            (None, "none"),
        ],
    )
    def test_parse_target_valid(self, raw, expected):
        assert self.config._parse_target(raw, "ADAPTER_X_TARGET") == expected

    @pytest.mark.parametrize("raw", ["bogus", "messages,completions", "123", " nope "])
    def test_parse_target_invalid_warns_and_none(self, raw, capsys):
        # Невалид — НЕ fatal (в отличие от ADAPTER_BACKEND_CONFIG): [WARN] в
        # консоль + трактовка как 'none' (вход выключен — безопасно).
        assert self.config._parse_target(raw, "ADAPTER_X_TARGET") == "none"
        out = capsys.readouterr().out
        assert "[WARN]" in out
        assert "ADAPTER_X_TARGET" in out
        assert "invalid value" in out


class TestTargetParseOnImport(_RouteCase):
    def test_invalid_env_on_import(self, monkeypatch, capsys):
        # Невалидное значение в env при импорте config — константа 'none' и
        # [WARN] в консоль (перезагрузка из патченного env).
        monkeypatch.setenv("ADAPTER_RESPONSES_TARGET", "sideways")
        cfg, _routing = _reload_modules()
        assert cfg.ADAPTER_RESPONSES_TARGET == "none"
        assert "[WARN]" in capsys.readouterr().out

    def test_auto_env_on_import(self, monkeypatch):
        monkeypatch.setenv("ADAPTER_MESSAGES_TARGET", "auto")
        cfg, _routing = _reload_modules()
        assert cfg.ADAPTER_MESSAGES_TARGET == "auto"


# ---------------------------------------------------------------------------
# target_for_input
# ---------------------------------------------------------------------------

class TestTargetForInput(_RouteCase):
    def test_reads_live_config_attribute(self):
        self._set_target(messages="auto", completions="completions",
                         responses="none")
        assert self.routing.target_for_input("messages") == "auto"
        assert self.routing.target_for_input("completions") == "completions"
        assert self.routing.target_for_input("responses") == "none"

    def test_falls_back_to_module_default(self):
        # Незнакомого атрибута в config нет — target_for_input берёт дефолт
        # из _TARGET_DEFAULTS (зеркало zero-config поведения).
        delattr(self.config, "ADAPTER_COMPLETIONS_TARGET")
        assert self.routing.target_for_input("completions") == "none"


# ---------------------------------------------------------------------------
# decide — таблица решений (все — ТОЛЬКО по кэшу, без сети)
# ---------------------------------------------------------------------------

class TestDecideDisabled(_RouteCase):
    def test_input_disabled_by_default(self):
        # Дефолты conftest: completions и responses выключены → disabled с
        # именем своей env-переменной в тексте и HTTP-статусом 404.
        # Сравнение — ТОЧНОЕ равенство: substring-ассерт пропускал бы
        # дублирование префикса («ADAPTER_ADAPTER_…» содержит «ADAPTER_…»).
        action, out, msg, status = self.routing.decide("completions", "test")
        assert action == "disabled"
        assert out is None
        assert status == 404
        assert msg == "endpoint is disabled (ADAPTER_COMPLETIONS_TARGET=none)"

        action, out, msg, status = self.routing.decide("responses", "test")
        assert action == "disabled"
        assert status == 404
        assert msg == "endpoint is disabled (ADAPTER_RESPONSES_TARGET=none)"

    def test_input_explicitly_disabled(self):
        self._set_target(messages="none")
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "disabled"
        assert out is None
        assert status == 404
        assert msg == "endpoint is disabled (ADAPTER_MESSAGES_TARGET=none)"
        # disabled не зависит от кэша проб.
        self._support("test", "messages", True)
        action, _out, _msg, _status = self.routing.decide("messages", "test")
        assert action == "disabled"


class TestDecideExplicitPassthrough(_RouteCase):
    def test_passthrough_when_backend_supports(self):
        self._set_target(messages="messages")
        self._support("test", "messages", True)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "passthrough"
        assert out == "messages"
        assert msg == ""
        assert status == 200

    def test_reject_when_backend_does_not_support(self):
        self._set_target(messages="messages")
        self._support("test", "messages", False)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "reject"
        assert out is None
        assert status == 502  # подтверждённый отказ бэкенда — 502
        assert "does not support messages" in msg

    def test_optimistic_when_never_probed(self):
        # None (не пробовано / пробы выключены) в ЯВНОМ режиме не блокирует:
        # «нет данных» → оптимистичный passthrough (как вёл бы себя адаптер
        # без роутинга). Отказ (502) — только при подтверждённом found=False.
        self._set_target(messages="messages")
        self._support("test", "messages", None)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "passthrough"
        assert out == "messages"
        assert msg == ""
        assert status == 200

    def test_completions_passthrough(self):
        self._set_target(completions="completions")
        self._support("test", "completions", True)
        action, out, msg, status = self.routing.decide("completions", "test")
        assert action == "passthrough"
        assert out == "completions"
        assert msg == ""
        assert status == 200

    def test_responses_passthrough(self):
        self._set_target(responses="responses")
        self._support("test", "responses", True)
        action, out, msg, status = self.routing.decide("responses", "test")
        assert action == "passthrough"
        assert out == "responses"
        assert msg == ""
        assert status == 200


class TestDecideExplicitConvert(_RouteCase):
    def test_messages_to_completions(self):
        # Дефолт messages-входа: конверсия messages → completions.
        self._support("test", "completions", True)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "convert"
        assert out == "completions"
        assert msg == ""
        assert status == 200

    def test_messages_to_completions_backend_unsupported(self):
        self._support("test", "completions", False)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "reject"
        assert out is None
        assert status == 502  # подтверждённый отказ бэкенда — 502
        assert "does not support completions" in msg

    def test_messages_to_completions_optimistic_when_never_probed(self):
        # None (не пробовано) → оптимистичный convert: дефолтный messages-путь
        # не должен ломаться на холодном кэше (как сегодня — запрос уходит).
        self._support("test", "completions", None)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "convert"
        assert out == "completions"
        assert msg == ""
        assert status == 200

    def test_convert_requires_backend_support_of_target(self):
        # Convert в явном режиме при None оптимистичен: поддержка входного
        # (messages) роли не играет, цели (completions) нет данных → пробуем.
        self._set_target(messages="completions")
        self._support("test", "messages", True)
        self._support("test", "completions", None)
        action, _out, _msg, status = self.routing.decide("messages", "test")
        assert action == "convert"
        assert status == 200

    def test_convert_rejected_when_target_confirmed_unsupported(self):
        self._set_target(messages="completions")
        self._support("test", "messages", True)
        self._support("test", "completions", False)
        action, _out, _msg, status = self.routing.decide("messages", "test")
        assert action == "reject"
        assert status == 502


class TestDecideUnimplemented(_RouteCase):
    @pytest.mark.parametrize(
        "inp,target,pair",
        [
            ("completions", "messages", ("completions", "messages")),
            ("messages", "responses", ("messages", "responses")),
            ("responses", "messages", ("responses", "messages")),
            ("completions", "responses", ("completions", "responses")),
            ("responses", "completions", ("responses", "completions")),
        ],
    )
    def test_unimplemented_pair_rejected(self, inp, target, pair):
        self._set_target(
            messages=target if inp == "messages" else "completions",
            completions=target if inp == "completions" else "none",
            responses=target if inp == "responses" else "none",
        )
        # Поддержка целевого формата есть, но конвертера нет — реестр False →
        # reject «not implemented» 400 (не 502: дело не в бэкенде).
        self._support("test", target, True)
        action, out, msg, status = self.routing.decide(inp, "test")  # type: ignore[arg-type]
        assert action == "reject"
        assert out is None
        assert status == 400
        assert self.routing.IMPLEMENTED_CONVERSIONS[pair] is False
        assert f"conversion '{pair[0]}' -> '{pair[1]}' is not implemented" in msg

    @pytest.mark.parametrize(
        "inp,target",
        [
            ("completions", "messages"),
            ("messages", "responses"),
            ("responses", "messages"),
            ("responses", "completions"),
        ],
    )
    def test_unimplemented_pair_rejected_even_without_data(self, inp, target):
        # Нереализованная пара — reject 400 независимо от данных пробы
        # (None тоже): конвертера нет в принципе, оптимизм ни при чём.
        self._set_target(
            messages=target if inp == "messages" else "completions",
            completions=target if inp == "completions" else "none",
            responses=target if inp == "responses" else "none",
        )
        self._support("test", target, None)
        action, _out, msg, status = self.routing.decide(inp, "test")  # type: ignore[arg-type]
        assert action == "reject"
        assert status == 400
        assert "is not implemented" in msg


class TestDecideAuto(_RouteCase):
    def test_messages_passthrough_has_priority(self):
        # В auto passthrough messages→messages (антропик-совместимый бэкенд)
        # приоритетнее конверсии в completions.
        self._set_target(messages="auto")
        self._support("test", "messages", True)
        self._support("test", "completions", True)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "passthrough"
        assert out == "messages"
        assert msg == ""
        assert status == 200

    def test_messages_falls_back_to_convert(self):
        self._set_target(messages="auto")
        self._support("test", "messages", False)
        self._support("test", "completions", True)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "convert"
        assert out == "completions"
        assert msg == ""
        assert status == 200

    def test_messages_no_route(self):
        # В auto None (не пробовано) НЕ даёт оптимистичного пути: нет
        # found=True → ни passthrough, ни convert — reject 400 «no route».
        self._set_target(messages="auto")
        self._support("test", "messages", None)
        self._support("test", "completions", None)
        action, out, msg, status = self.routing.decide("messages", "test")
        assert action == "reject"
        assert out is None
        assert status == 400
        assert "no route" in msg
        assert "test" in msg

    def test_completions_auto_passthrough(self):
        self._set_target(completions="auto")
        self._support("test", "completions", True)
        action, out, _msg, status = self.routing.decide("completions", "test")
        assert action == "passthrough"
        assert out == "completions"
        assert status == 200

    def test_completions_auto_no_implemented_conversion(self):
        # Пары completions→* не реализованы: нет passthrough — маршрута нет.
        self._set_target(completions="auto")
        self._support("test", "completions", False)
        action, _out, msg, status = self.routing.decide("completions", "test")
        assert action == "reject"
        assert status == 400
        assert "no route" in msg

    def test_responses_auto_no_implemented_conversion(self):
        self._set_target(responses="auto")
        self._support("test", "responses", False)
        action, _out, msg, status = self.routing.decide("responses", "test")
        assert action == "reject"
        assert status == 400
        assert "no route" in msg


class TestDecideNoNetwork(_RouteCase):
    def test_decide_makes_no_http_calls(self):
        # Решения — только по кэшу (config.endpoint_support): ни urlopen, ни
        # _http_json во время decide не вызываются ни при каком раскладе.
        # Мок urlopen падал бы на любом сетевом обращении.
        self._set_target(messages="auto", completions="completions",
                         responses="auto")
        self._support("test", "messages", True)
        self._support("test", "completions", True)
        with mock.patch.object(
            self.config, "_http_json",
            side_effect=AssertionError("network in decide"),
        ), mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("network in decide"),
        ):
            for inp in ("messages", "completions", "responses"):
                action, _out, _msg, status = self.routing.decide(inp, "test")  # type: ignore[arg-type]
                assert action in ("passthrough", "convert", "reject", "disabled")
                assert isinstance(status, int)
        # endpoint_support ходит только в _ENDPOINT_STATE (память).

    def test_endpoint_support_returns_none_for_unknown(self):
        # Неизвестное имя эндпоинта / незнакомый бэкенд → None (не исключение).
        assert self.config.endpoint_support("test", "bogus") is None
        assert self.config.endpoint_support("nope", "completions") is None
        self._support("test", "completions", True)
        assert self.config.endpoint_support("test", "completions") is True
        assert self.config.endpoint_support("other", "completions") is None
        self._support("test", "completions", False)
        assert self.config.endpoint_support("test", "completions") is False
