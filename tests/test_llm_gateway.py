"""
Тесты шлюза моделей: чтение реестра из конфига, конвертация форматов
сообщений, поведение при недоступном облаке.

Эти тесты защищают от повторения двух реальных сбоев:
  - Ollama требует arguments словарём, OpenAI — строкой с tool_call_id;
  - облачная ошибка не должна превращаться в исключение и рвать граф.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.loader import load_models  # noqa: E402
from config.resilience import ModeController  # noqa: E402
from core.llm_gateway import LLMGateway, ModelSpec  # noqa: E402


@pytest.fixture()
def gateway() -> LLMGateway:
    models = load_models()
    controller = ModeController.from_config(models=models,
                                           settings={"mode": "auto", "resilience": {}},
                                           health_probe=lambda: True)
    return LLMGateway(models, controller)


# ---- реестр моделей ------------------------------------------------------


def test_roles_are_read_from_config(gateway: LLMGateway):
    assert gateway.spec("router").model
    assert gateway.spec("verifier").model
    assert gateway.spec("embeddings").model


def test_missing_role_raises(gateway: LLMGateway):
    from core.llm_gateway import ModelCallError
    with pytest.raises(ModelCallError):
        gateway.spec("не_существует")


def test_three_active_proposers(gateway: LLMGateway):
    specs = gateway.proposer_specs()
    assert len(specs) == 3
    assert len({s.model for s in specs}) == 3


def test_disabled_models_are_skipped(gateway: LLMGateway):
    disabled = [p["model"] for p in gateway.config["proposers"] if not p.get("enabled", True)]
    active = {s.model for s in gateway.proposer_specs()}
    assert active.isdisjoint(disabled)


def test_adding_model_needs_no_code_change():
    """Ключевое требование: новая модель = запись в конфиге."""
    config = {
        "version": "1.0",
        "proposers": [{"id": "new_one", "provider": "ollama", "model": "новая-модель:8b"}],
        "judge": {"local_fallback": {"provider": "ollama", "model": "j:8b"}},
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
    }
    gw = LLMGateway(config, ModeController(mode="local_only"))
    specs = gw.proposer_specs()
    assert specs[0].id == "new_one" and specs[0].model == "новая-модель:8b"


def test_judge_specs_priority(gateway: LLMGateway):
    cloud, local = gateway.judge_specs()
    assert cloud is not None and cloud.provider == "openrouter"
    assert local.provider == "ollama"


def test_model_spec_keeps_unknown_fields():
    spec = ModelSpec.from_dict({"model": "m", "notes": "лимиты меняются"})
    assert spec.extra["notes"] == "лимиты меняются"


# ---- конвертация сообщений ----------------------------------------------


def test_openai_conversion_stringifies_arguments():
    messages = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"type": "function",
                         "function": {"name": "qgis__layer_info",
                                      "arguments": {"path": "a.shp"}}}]},
        {"role": "tool", "name": "qgis__layer_info", "content": "результат"},
    ]
    converted = LLMGateway._to_openai_messages(messages)
    call = converted[0]["tool_calls"][0]
    assert isinstance(call["function"]["arguments"], str)
    assert json.loads(call["function"]["arguments"]) == {"path": "a.shp"}
    # ответ инструмента обязан ссылаться на id вызова
    assert converted[1]["tool_call_id"] == call["id"]


def test_openai_conversion_preserves_existing_strings():
    messages = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "abc", "type": "function",
         "function": {"name": "t__a", "arguments": '{"x": 1}'}}]}]
    converted = LLMGateway._to_openai_messages(messages)
    assert converted[0]["tool_calls"][0]["id"] == "abc"
    assert converted[0]["tool_calls"][0]["function"]["arguments"] == '{"x": 1}'


def test_openai_conversion_handles_plain_messages():
    messages = [{"role": "system", "content": "с"}, {"role": "user", "content": "u"}]
    assert LLMGateway._to_openai_messages(messages) == messages


def test_multiple_tool_calls_get_distinct_ids():
    messages = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"type": "function", "function": {"name": "t__a", "arguments": {}}},
            {"type": "function", "function": {"name": "t__b", "arguments": {}}}]},
        {"role": "tool", "name": "t__a", "content": "1"},
        {"role": "tool", "name": "t__b", "content": "2"},
    ]
    converted = LLMGateway._to_openai_messages(messages)
    ids = [c["id"] for c in converted[0]["tool_calls"]]
    assert len(set(ids)) == 2
    assert converted[1]["tool_call_id"] == ids[0]
    assert converted[2]["tool_call_id"] == ids[1]


# ---- поведение при недоступном облаке -----------------------------------


def test_cloud_call_in_local_only_returns_error_not_exception():
    config = load_models()
    gw = LLMGateway(config, ModeController(mode="local_only"))
    spec = ModelSpec(id="c", provider="openrouter", model="any/model")
    response = gw.chat(spec, [{"role": "user", "content": "привет"}])
    assert response.ok is False
    assert "недоступно" in response.error.lower() or "local_only" in response.error


def test_unknown_provider_returns_error():
    gw = LLMGateway(load_models(), ModeController(mode="local_only"))
    spec = ModelSpec(id="x", provider="телепатия", model="m")
    response = gw.chat(spec, [{"role": "user", "content": "?"}])
    assert response.ok is False and "провайдер" in response.error.lower()


def test_invented_tool_name_does_not_kill_the_step(monkeypatch, gateway: LLMGateway):
    """
    Модель придумала действие (files__read_csv) -> Ollama отвечает 500 на весь
    запрос. Раньше из-за этого падал ВЕСЬ шаг плана, хотя предыдущие шаги уже
    добыли данные (случай 06.08 в журнале). Теперь это данные для модели.
    """
    def boom(spec, messages, tools, timeout_sec):
        raise RuntimeError("tool 'files__read_csv' not found (status code: 500)")

    monkeypatch.setattr(gateway, "_chat_ollama", boom)
    tools = [{"type": "function", "function": {"name": "datafiles__write_excel",
                                               "description": "",
                                               "parameters": {"type": "object"}}}]
    response = gateway.chat(ModelSpec(id="p", provider="ollama", model="m"),
                            [{"role": "user", "content": "сохрани в эксель"}],
                            tools=tools)
    assert response.ok is True, "шаг не должен падать из-за выдумки модели"
    assert "files__read_csv" in response.text
    # модели показываем, что РЕАЛЬНО доступно, иначе она повторит выдумку
    assert "datafiles__write_excel" in response.text


def test_real_infrastructure_failure_still_reported(monkeypatch, gateway: LLMGateway):
    """Обратная сторона: настоящий сбой не должен маскироваться под ответ."""
    def boom(spec, messages, tools, timeout_sec):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(gateway, "_chat_ollama", boom)
    response = gateway.chat(ModelSpec(id="p", provider="ollama", model="m"),
                            [{"role": "user", "content": "?"}])
    assert response.ok is False and "connection refused" in response.error


def test_tool_hint_added_to_system_prompt(monkeypatch, gateway: LLMGateway):
    """Подсказка формата вызова обязательна для моделей без нативного tool calling."""
    captured: dict = {}

    def fake_chat(spec, messages, tools, timeout_sec):
        captured["messages"] = messages
        return {"content": "ок", "tool_calls": []}

    monkeypatch.setattr(gateway, "_chat_ollama", fake_chat)
    tools = [{"type": "function", "function": {"name": "t__a", "description": "",
                                               "parameters": {"type": "object"}}}]
    gateway.chat(ModelSpec(id="p", provider="ollama", model="m"),
                 [{"role": "system", "content": "База"}, {"role": "user", "content": "?"}],
                 tools=tools)
    assert "ОБЯЗАТЕЛЬНО вызови инструмент" in captured["messages"][0]["content"]
