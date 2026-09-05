"""
Тесты Router: таблица правил, LLM-классификатор, graceful degradation.
Модели не вызываются — используется заглушка шлюза.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.loader import load_routing_rules  # noqa: E402
from core.llm_gateway import LLMResponse, ModelSpec  # noqa: E402
from core.router import Router, _first_json_object  # noqa: E402
from core.schemas import Task, TaskComplexity  # noqa: E402


class FakeGateway:
    """Заглушка шлюза: отдаёт заранее заданный ответ Router-модели."""

    def __init__(self, text: str = "", ok: bool = True):
        self.text = text
        self.ok = ok
        self.calls = 0

    def spec(self, role: str) -> ModelSpec:
        return ModelSpec(id=role, provider="stub", model="stub-model")

    def chat(self, spec, messages, tools=None, task_mode=None, timeout_sec=None):
        self.calls += 1
        return LLMResponse(text=self.text, ok=self.ok,
                           error=None if self.ok else "модель недоступна")


@pytest.fixture()
def rules() -> dict:
    return load_routing_rules()


# ---- таблица правил ------------------------------------------------------


@pytest.mark.parametrize("prompt,needs_tools", [
    (r"Сколько объектов в слое C:\data\wells.shp?", True),
    ("Какая CRS у этого geojson?", True),
    ("Прочитай атрибуты слоя", True),
    ("Расскажи анекдот", False),
])
def test_geodata_prompts_require_tools(rules, prompt, needs_tools):
    router = Router(rules)
    assert router.route(Task(prompt=prompt)).needs_tools is needs_tools


def test_qgis_is_suggested_for_geodata(rules):
    decision = Router(rules).route(Task(prompt="открой слой .gpkg и покажи атрибуты"))
    assert "qgis" in decision.suggested_tools


def test_trivial_prompt_skips_full_debate(rules):
    decision = Router(rules).route(Task(prompt="привет"))
    assert decision.complexity is TaskComplexity.SIMPLE
    assert decision.needs_full_debate is False


def test_retrieval_keywords(rules):
    decision = Router(rules).route(Task(prompt="Найди в документах упоминание габбро"))
    assert decision.needs_retrieval is True


def test_rules_have_priority_over_llm(rules):
    """Если правило сработало, модель не должна вызываться вовсе."""
    gateway = FakeGateway('{"complexity":"simple","needs_tools":false}')
    router = Router(rules, gateway)
    decision = router.route(Task(prompt=r"Инфо о слое C:\d\a.shp"))
    assert decision.decided_by == "rules"
    assert gateway.calls == 0


def test_first_matching_rule_wins(rules):
    router = Router(rules)
    # содержит и гео-признаки, и слово «перепроверь» — гео-правило выше
    decision = router.route(Task(prompt="перепроверь CRS слоя"))
    assert "geodata_keywords" in decision.reason


# ---- LLM-классификатор ---------------------------------------------------


def test_llm_classifier_used_when_no_rule_matches(rules):
    gateway = FakeGateway(
        '{"complexity":"ambiguous","needs_tools":false,"needs_retrieval":false,'
        '"needs_full_debate":true,"reason":"неясная формулировка"}')
    decision = Router(rules, gateway).route(Task(prompt="Сделай красиво"))
    assert decision.decided_by == "llm"
    assert decision.complexity is TaskComplexity.AMBIGUOUS
    assert gateway.calls == 1


def test_llm_answer_with_extra_text_is_parsed(rules):
    gateway = FakeGateway('Вот мой ответ:\n{"complexity":"simple","needs_full_debate":false}\nГотово')
    decision = Router(rules, gateway).route(Task(prompt="Сделай красиво"))
    assert decision.decided_by == "llm" and decision.needs_full_debate is False


def test_llm_failure_falls_back_to_default(rules):
    """Graceful degradation: сбой модели не ломает маршрутизацию."""
    decision = Router(rules, FakeGateway(ok=False)).route(Task(prompt="Сделай красиво"))
    assert decision.decided_by == "fallback"
    assert decision.needs_full_debate is True


def test_garbage_llm_answer_falls_back(rules):
    decision = Router(rules, FakeGateway("я не понял задачу")).route(Task(prompt="Сделай красиво"))
    assert decision.decided_by == "fallback"


def test_invalid_complexity_value_is_sanitized(rules):
    gateway = FakeGateway('{"complexity":"супер-сложно"}')
    decision = Router(rules, gateway).route(Task(prompt="Сделай красиво"))
    assert decision.complexity is TaskComplexity.NEEDS_VERIFICATION


def test_router_without_gateway_uses_default(rules):
    decision = Router(rules).route(Task(prompt="Сделай красиво"))
    assert decision.decided_by == "fallback"


# ---- добавление правила без правки кода ---------------------------------


def test_custom_rule_from_config_is_applied():
    """Ключевое требование: новое правило = запись в YAML, код не меняется."""
    custom = {
        "version": "1.0",
        "llm_classifier": {"enabled": False},
        "default": {"complexity": "needs_verification", "reason": "по умолчанию"},
        "rules": [{
            "name": "excel_rule",
            "match": {"any_keyword": ["xlsx", "эксель"]},
            "decision": {"complexity": "simple", "needs_tools": True,
                         "suggested_tools": ["excel"], "reason": "таблица Excel"},
        }],
    }
    decision = Router(custom).route(Task(prompt="Открой файл отчёт.xlsx"))
    assert decision.suggested_tools == ["excel"]
    assert decision.complexity is TaskComplexity.SIMPLE


def test_match_requires_all_specified_conditions():
    config = {
        "version": "1.0", "llm_classifier": {"enabled": False},
        "default": {"complexity": "needs_verification"},
        "rules": [{"name": "short_and_keyword",
                   "match": {"any_keyword": ["привет"], "max_length": 10},
                   "decision": {"complexity": "simple"}}],
    }
    router = Router(config)
    assert router.route(Task(prompt="привет")).complexity is TaskComplexity.SIMPLE
    long_prompt = "привет, у меня очень длинная задача про геологию"
    assert router.route(Task(prompt=long_prompt)).complexity is TaskComplexity.NEEDS_VERIFICATION


def test_broken_regex_does_not_crash():
    config = {
        "version": "1.0", "llm_classifier": {"enabled": False},
        "default": {"complexity": "simple"},
        "rules": [{"name": "bad", "match": {"regex": "([unclosed"},
                   "decision": {"complexity": "ambiguous"}}],
    }
    assert Router(config).route(Task(prompt="что угодно")).complexity is TaskComplexity.SIMPLE


def test_first_json_object_helper():
    assert _first_json_object('шум {"a": 1} хвост') == {"a": 1}
    assert _first_json_object("без json") is None
    assert _first_json_object('{"broken": ') is None
