"""Tests for backend_adapter.routing — input-endpoint routing (v0.9.0).

Три входных POST-эндпоинта (/v1/messages, /v1/chat/completions, /v1/responses)
управляются тремя TARGET-переменными (config.ADAPTER_*_TARGET). Модуль
routing — чистая логика решений по значению TARGET: сети в decide НЕТ,
что здесь и проверяется.

v0.9.9: активные пробы эндпойнтов удалены — маршрут выбирается ВСЕГДА, а
неверный выбор эндпойнта агентом фиксируется по факту ошибки бэкенда.
Поэтому ветки «reject 502 (бэкенд подтверждённо не поддерживает)» здесь
больше нет: остались 200 (passthrough/convert), 400 (пары нет в адаптере)
и 404 (вход выключен).

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
    """Base: per-test fresh modules + helper to manipulate TARGET."""

    def setup_method(self):
        self.config, self.routing = _reload_modules()

    def _set_target(self, **targets: str) -> None:
        """Сменить TARGET-константы config на время одного теста."""
        for var, value in targets.items():
            assert var in ("messages", "completions", "responses"), var
            setattr(self.config, f"ADAPTER_{var.upper()}_TARGET", value)


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

    def test_input_paths_cover_all_formats(self):
        # INPUT_PATHS — статическая таблица входных путей адаптера (v0.9.9:
        # зеркалить ENDPOINT_PROBES больше нечего — пробы удалены).
        assert set(self.routing.INPUT_PATHS) == {"messages", "completions", "responses"}
        for fmt, path in self.routing.INPUT_PATHS.items():
            assert self.routing.input_path_to_format(path) == fmt

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("messages", "messages"),
            ("MESSAGES", "messages"),
            ("  completions ", "completions"),
            ("Responses", "responses"),
            ("passthrough", "passthrough"),
            ("PASSTHROUGH", "passthrough"),
            ("none", "none"),
            ("", "none"),  # пусто — безопасное выключение
            (None, "none"),
        ],
    )
    def test_parse_target_valid(self, raw, expected):
        assert self.config._parse_target(raw, "ADAPTER_X_TARGET") == expected

    @pytest.mark.parametrize(
        "raw", ["bogus", "messages,completions", "123", " nope ", "auto"]
    )
    def test_parse_target_invalid_warns_and_none(self, raw, capsys):
        # Невалид — НЕ fatal (в отличие от ADAPTER_BACKEND_CONFIG): [WARN] в
        # консоль + трактовка как 'none' (вход выключен — безопасно).
        # 'auto' — значение прежних версий, удалено в v0.9.4: та же ветка.
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

    def test_legacy_auto_env_treated_as_none(self, monkeypatch, capsys):
        # 'auto' из env прежних версий (удалено в v0.9.4): невалидно →
        # [WARN] + 'none' (вход выключен), старт не падает.
        monkeypatch.setenv("ADAPTER_MESSAGES_TARGET", "auto")
        cfg, _routing = _reload_modules()
        assert cfg.ADAPTER_MESSAGES_TARGET == "none"
        assert "[WARN]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# target_for_input
# ---------------------------------------------------------------------------

class TestTargetForInput(_RouteCase):
    def test_reads_live_config_attribute(self):
        self._set_target(messages="passthrough", completions="completions",
                         responses="none")
        assert self.routing.target_for_input("messages") == "passthrough"
        assert self.routing.target_for_input("completions") == "completions"
        assert self.routing.target_for_input("responses") == "none"

    def test_falls_back_to_module_default(self):
        # Незнакомого атрибута в config нет — target_for_input берёт дефолт
        # из _TARGET_DEFAULTS (зеркало zero-config поведения).
        delattr(self.config, "ADAPTER_COMPLETIONS_TARGET")
        assert self.routing.target_for_input("completions") == "none"


class TestTargetEnvName(_RouteCase):
    """target_env_name — публичный доступ к _ENV_NAMES для WEBUI."""

    def test_maps_each_input_to_its_variable(self):
        assert self.routing.target_env_name("messages") == "ADAPTER_MESSAGES_TARGET"
        assert self.routing.target_env_name("completions") == "ADAPTER_COMPLETIONS_TARGET"
        assert self.routing.target_env_name("responses") == "ADAPTER_RESPONSES_TARGET"

    def test_matches_env_names_table(self):
        # Зеркало приватной таблицы: имя переменной — то же, что в decide
        # подставляется в ERROR_DISABLED (иначе селект таблицы Sessions
        # адресовал бы не ту переменную, что управляет входом строки).
        for inp, name in self.routing._ENV_NAMES.items():
            assert self.routing.target_env_name(inp) == name


# ---------------------------------------------------------------------------
# Пер-сессионный TARGET (v0.9.5): decide/target_for_input читают
# session_settings.effective при непустом session_id
# ---------------------------------------------------------------------------

class TestPerSessionTarget(_RouteCase):
    def _settings(self):
        from backend_adapter import session_settings

        return session_settings

    def test_session_override_wins_over_config(self):
        # Общая настройка — passthrough, сессия переопределена в none:
        # запрос этой сессии выключается, общая настройка не тронута.
        self._set_target(messages="passthrough")
        self._settings().set_config("s-1", {"ADAPTER_MESSAGES_TARGET": "none"})
        assert self.routing.target_for_input("messages", "s-1") == "none"
        assert self.routing.target_for_input("messages") == "passthrough"
        action, _out, msg, status = self.routing.decide("messages", "s-1")
        assert action == "disabled"
        assert status == 404
        assert "ADAPTER_MESSAGES_TARGET=none" in msg

    def test_cleared_override_falls_back_to_config(self):
        # v0.9.9: «вернуться к общему» — переопределение СНИМАЕТСЯ (clear),
        # сессия снова живо наследует общую настройку.
        self._set_target(messages="passthrough")
        self._settings().set_config("s-1", {"ADAPTER_MESSAGES_TARGET": "none"})
        assert self.routing.target_for_input("messages", "s-1") == "none"
        self._settings().set_config("s-1", clear=("ADAPTER_MESSAGES_TARGET",))
        assert self.routing.target_for_input("messages", "s-1") == "passthrough"

    def test_unset_session_uses_config(self):
        # Переопределения нет — действует общая настройка приложения
        # (поведение прежних версий без изменений).
        self._set_target(messages="messages")
        assert self.routing.target_for_input("messages", "s-2") == "messages"

    def test_other_session_not_affected(self):
        # Переопределение адресуется session_id: соседняя сессия видит общее.
        self._set_target(completions="passthrough")
        self._settings().set_config("s-1", {"ADAPTER_COMPLETIONS_TARGET": "none"})
        assert self.routing.target_for_input("completions", "s-1") == "none"
        assert self.routing.target_for_input("completions", "s-2") == "passthrough"

    def test_decide_routes_session_to_other_backend_target(self):
        # Сессия уходит на другой маршрут, чем общая настройка: общая —
        # messages→completions, сессия — passthrough messages.
        self._set_target(messages="completions")
        self._settings().set_config("s-1", {"ADAPTER_MESSAGES_TARGET": "passthrough"})
        action, out, _msg, status = self.routing.decide("messages", "s-1")
        assert action == "passthrough"
        assert out == "messages"
        assert status == 200
        # Без session_id — прежний маршрут (convert).
        action, out, _msg, _status = self.routing.decide("messages")
        assert action == "convert"
        assert out == "completions"


# ---------------------------------------------------------------------------
# decide — таблица решений (все — ТОЛЬКО по значению TARGET, без сети)
# ---------------------------------------------------------------------------

class TestDecideDisabled(_RouteCase):
    def test_input_disabled_by_default(self):
        # Дефолты conftest: completions и responses выключены → disabled с
        # именем своей env-переменной в тексте и HTTP-статусом 404.
        # Сравнение — ТОЧНОЕ равенство: substring-ассерт пропускал бы
        # дублирование префикса («ADAPTER_ADAPTER_…» содержит «ADAPTER_…»).
        action, out, msg, status = self.routing.decide("completions")
        assert action == "disabled"
        assert out is None
        assert status == 404
        assert msg == "endpoint is disabled (ADAPTER_COMPLETIONS_TARGET=none)"

        action, out, msg, status = self.routing.decide("responses")
        assert action == "disabled"
        assert status == 404
        assert msg == "endpoint is disabled (ADAPTER_RESPONSES_TARGET=none)"

    def test_input_explicitly_disabled(self):
        self._set_target(messages="none")
        action, out, msg, status = self.routing.decide("messages")
        assert action == "disabled"
        assert out is None
        assert status == 404
        assert msg == "endpoint is disabled (ADAPTER_MESSAGES_TARGET=none)"


class TestDecidePassthrough(_RouteCase):
    """TARGET=passthrough: дословная передача на эндпойнт входного формата."""

    def test_messages_passthrough(self):
        self._set_target(messages="passthrough")
        action, out, msg, status = self.routing.decide("messages")
        assert action == "passthrough"
        assert out == "messages"
        assert msg == ""
        assert status == 200

    def test_completions_passthrough(self):
        self._set_target(completions="passthrough")
        action, out, msg, status = self.routing.decide("completions")
        assert action == "passthrough"
        assert out == "completions"
        assert msg == ""
        assert status == 200

    def test_responses_passthrough(self):
        self._set_target(responses="passthrough")
        action, out, msg, status = self.routing.decide("responses")
        assert action == "passthrough"
        assert out == "responses"
        assert msg == ""
        assert status == 200

    def test_passthrough_optimistic_without_probes(self):
        # v0.9.9: пробы удалены — passthrough не блокируется «отсутствием
        # данных» или «подтверждённым отказом»: маршрут выбирается всегда,
        # отказ придёт от бэкенда как обычная ошибка (фиксируется в .err).
        self._set_target(messages="passthrough")
        action, out, msg, status = self.routing.decide("messages")
        assert (action, out, msg, status) == ("passthrough", "messages", "", 200)

    def test_concrete_format_equal_to_input_is_conversion_not_passthrough(self):
        # TARGET=messages — это ПРЕОБРАЗОВАНИЕ messages→messages (сортировка
        # system), а НЕ дословная передача; дословная — только 'passthrough'.
        self._set_target(messages="messages")
        action, out, _msg, status = self.routing.decide("messages")
        assert action == "convert"
        assert out == "messages"
        assert status == 200


class TestDecideExplicitConvert(_RouteCase):
    def test_messages_to_completions(self):
        # Дефолт messages-входа: конверсия messages → completions.
        action, out, msg, status = self.routing.decide("messages")
        assert action == "convert"
        assert out == "completions"
        assert msg == ""
        assert status == 200

    def test_messages_to_messages_is_convert(self):
        # messages→messages — реализованная пара (сортировка system в начало).
        self._set_target(messages="messages")
        action, out, msg, status = self.routing.decide("messages")
        assert action == "convert"
        assert out == "messages"
        assert msg == ""
        assert status == 200

    def test_responses_to_responses_is_convert(self):
        # responses→responses — реализованная пара (v0.9.6): «внутренний
        # конвертор» — store=false + /model, без перестановки полей. TARGET
        # responses на входе responses даёт convert (не passthrough, не
        # reject), выходной формат — responses.
        self._set_target(responses="responses")
        action, out, msg, status = self.routing.decide("responses")
        assert action == "convert"
        assert out == "responses"
        assert msg == ""
        assert status == 200

    def test_responses_to_completions_is_convert(self):
        # responses→completions — реализованная пара (v0.9.7): полный
        # кросс-форматный конвертер + /model. TARGET completions на входе
        # responses даёт convert (не reject), выходной формат — completions.
        self._set_target(responses="completions")
        action, out, msg, status = self.routing.decide("responses")
        assert action == "convert"
        assert out == "completions"
        assert msg == ""
        assert status == 200


class TestDecideUnimplemented(_RouteCase):
    @pytest.mark.parametrize(
        "inp,target,pair",
        [
            ("completions", "messages", ("completions", "messages")),
            ("messages", "responses", ("messages", "responses")),
            ("responses", "messages", ("responses", "messages")),
            ("completions", "responses", ("completions", "responses")),
            # self-пары без конвертера (реестр False) — тоже 400; дословная
            # передача таких входов достигается значением TARGET=passthrough.
            # (responses→responses с v0.9.6 и responses→completions с v0.9.7 —
            # реализованные пары, см. TestDecideExplicitConvert.)
            ("completions", "completions", ("completions", "completions")),
        ],
    )
    def test_unimplemented_pair_rejected(self, inp, target, pair):
        self._set_target(
            messages=target if inp == "messages" else "completions",
            completions=target if inp == "completions" else "none",
            responses=target if inp == "responses" else "none",
        )
        # Конвертера нет — реестр False → reject «not implemented» 400
        # (дело не в бэкенде, а в отсутствии пары в адаптере).
        action, out, msg, status = self.routing.decide(inp)  # type: ignore[arg-type]
        assert action == "reject"
        assert out is None
        assert status == 400
        assert self.routing.IMPLEMENTED_CONVERSIONS[pair] is False
        assert f"conversion '{pair[0]}' -> '{pair[1]}' is not implemented" in msg


class TestDecideNeverRejectsWith502(_RouteCase):
    """v0.9.9: пробы эндпойнтов удалены — маршрут выбирается ВСЕГДА.

    Решение не зависит от поддержки эндпойнта бэкендом: статус исхода —
    только 200 (passthrough/convert), 400 (пары нет в реестре) или 404
    (вход выключен). 502 «бэкенд подтверждённо не поддерживает» исчез
    вместе с пробами.
    """

    @pytest.mark.parametrize("inp", ["messages", "completions", "responses"])
    @pytest.mark.parametrize(
        "target", ["messages", "completions", "responses", "passthrough", "none"]
    )
    def test_status_never_502(self, inp, target):
        self._set_target(
            messages=target if inp == "messages" else "completions",
            completions=target if inp == "completions" else "none",
            responses=target if inp == "responses" else "none",
        )
        _action, _out, _msg, status = self.routing.decide(inp)  # type: ignore[arg-type]
        assert status in (200, 400, 404), (inp, target, status)


class TestDecideNoNetwork(_RouteCase):
    def test_decide_makes_no_http_calls(self):
        # Решения — только по значению TARGET: ни urlopen, ни синхронных
        # запросов к бэкенду во время decide не происходит ни при каком
        # раскладе (v0.9.9: активные пробы эндпойнтов удалены). Мок urlopen
        # падал бы на любом сетевом обращении.
        self._set_target(messages="passthrough", completions="completions",
                         responses="none")
        with mock.patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("network in decide"),
        ):
            for inp in ("messages", "completions", "responses"):
                action, _out, _msg, status = self.routing.decide(inp)  # type: ignore[arg-type]
                assert action in ("passthrough", "convert", "reject", "disabled")
                assert isinstance(status, int)

    def test_no_probe_api_left(self):
        # Следы пробы эндпойнтов сняты из обоих модулей (v0.9.9).
        for name in (
            "endpoint_support", "probe_endpoints", "_ENDPOINT_STATE",
            "ENDPOINT_PROBES", "ADAPTER_ENDPOINT_PROBE",
        ):
            assert not hasattr(self.config, name), name
        assert not hasattr(self.routing, "ERROR_UNSUPPORTED")
