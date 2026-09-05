"""
Тесты агентов (Proposer, Verifier, Judge) на заглушках моделей.

Проверяем главное, что ломалось в прошлой реализации:
  - Proposer действительно вызывает инструмент и строит ответ на его данных;
  - ошибка инструмента доходит до модели, а не подменяется выдумкой;
  - Verifier даёт консенсус 2 из 3 и ловит конфликт фактов;
  - Judge уходит в облако при доступности и падает на локальную модель иначе.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.judge import Judge, _split_answer  # noqa: E402
from agents.proposer import Proposer, _format_tool_result  # noqa: E402
from agents.verifier import Verifier  # noqa: E402
from core.llm_gateway import LLMResponse, ModelSpec  # noqa: E402
from core.schemas import (  # noqa: E402
    Proposal,
    ToolCallRequest,
    ToolCallResult,
    Verdict,
)


class ScriptedGateway:
    """
    Заглушка шлюза: отдаёт заранее подготовленные ответы по очереди.
    Пишет полученные messages, чтобы проверить, что результат инструмента
    реально попал в контекст модели.
    """

    def __init__(self, script: list[LLMResponse], cloud_allowed: bool = False):
        self.script = list(script)
        self.seen_messages: list[list[dict]] = []
        self.judge_cloud = ModelSpec(id="judge_cloud", provider="openrouter", model="cloud-model")
        self.judge_local = ModelSpec(id="judge_local", provider="ollama", model="local-model")

        class MC:
            def __init__(self, allowed: bool):
                self.allowed = allowed

            def status(self, task_mode=None, check_health=True):
                from config.resilience import CloudStatus
                return CloudStatus(allowed=self.allowed,
                                   reason="ок" if self.allowed else "облако выключено")

        self.mc = MC(cloud_allowed)

    def chat(self, spec, messages, tools=None, task_mode=None, timeout_sec=None):
        self.seen_messages.append([dict(m) for m in messages])
        if not self.script:
            return LLMResponse(text="ИТОГ", model=spec.model, provider=spec.provider)
        response = self.script.pop(0)
        response.model = spec.model
        response.provider = spec.provider
        return response

    def spec(self, role: str) -> ModelSpec:
        return ModelSpec(id=role, provider="stub", model=f"{role}-model")

    def judge_specs(self):
        return self.judge_cloud, self.judge_local

    def proposer_specs(self, include_cloud: bool = False):
        return [ModelSpec(id="proposer_a", provider="stub", model="m-a")]


class FakeRegistry:
    """Реестр-заглушка с одним инструментом."""

    def __init__(self, ok: bool = True):
        self.ok = ok
        self.calls: list[ToolCallRequest] = []

    def openai_tools(self, allowed=None):
        return [{"type": "function", "function": {
            "name": "geo__info", "description": "инфо",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}}]

    def call(self, request: ToolCallRequest) -> ToolCallResult:
        self.calls.append(request)
        if self.ok:
            return ToolCallResult(call_id=request.call_id, tool=request.tool,
                                  action=request.action, ok=True,
                                  data={"crs": "EPSG:32636", "features": 1},
                                  summary="1 объект, EPSG:32636", tool_version="1.0.0")
        return ToolCallResult(call_id=request.call_id, tool=request.tool, action=request.action,
                              ok=False, error="Файл слоя не найден", tool_version="1.0.0")


SPEC = ModelSpec(id="proposer_a", provider="ollama", model="test-model")


# ---- Proposer ------------------------------------------------------------


def test_proposer_calls_tool_and_uses_its_data():
    registry = FakeRegistry(ok=True)
    gateway = ScriptedGateway([
        LLMResponse(tool_calls=[ToolCallRequest(tool="geo", action="info",
                                                args={"path": "a.shp"})]),
        LLMResponse(text="В слое 1 объект, CRS EPSG:32636"),
    ])
    proposal = Proposer(SPEC, gateway, registry).run("Сколько объектов?")
    assert proposal.ok
    assert len(registry.calls) == 1
    assert "EPSG:32636" in proposal.answer
    assert proposal.tool_results[0].ok

    # результат инструмента реально попал в контекст второго вызова
    second_call = gateway.seen_messages[1]
    tool_messages = [m for m in second_call if m.get("role") == "tool"]
    assert tool_messages and "EPSG:32636" in tool_messages[0]["content"]


def test_tool_arguments_are_dict_in_history():
    """Регрессия: Ollama отклоняет arguments в виде JSON-строки."""
    registry = FakeRegistry(ok=True)
    gateway = ScriptedGateway([
        LLMResponse(tool_calls=[ToolCallRequest(tool="geo", action="info", args={"path": "a.shp"})]),
        LLMResponse(text="готово"),
    ])
    Proposer(SPEC, gateway, registry).run("вопрос")
    assistant = [m for m in gateway.seen_messages[1] if m.get("role") == "assistant"][0]
    assert isinstance(assistant["tool_calls"][0]["function"]["arguments"], dict)


def test_tool_error_reaches_model_and_is_not_hidden():
    registry = FakeRegistry(ok=False)
    gateway = ScriptedGateway([
        LLMResponse(tool_calls=[ToolCallRequest(tool="geo", action="info", args={})]),
        LLMResponse(text="Инструмент вернул ошибку: файл не найден"),
    ])
    proposal = Proposer(SPEC, gateway, registry).run("вопрос")
    assert proposal.ok is True                    # сам Proposer не «упал»
    assert proposal.tool_results[0].ok is False   # но неудача зафиксирована
    tool_message = [m for m in gateway.seen_messages[1] if m.get("role") == "tool"][0]
    assert "ОШИБКА" in tool_message["content"]
    assert "не придумывай" in tool_message["content"].lower()


def test_proposer_model_failure_is_reported_not_raised():
    gateway = ScriptedGateway([LLMResponse(ok=False, error="модель не отвечает")])
    proposal = Proposer(SPEC, gateway, FakeRegistry()).run("вопрос")
    assert proposal.ok is False and "не отвечает" in proposal.error


def test_tool_iterations_are_limited():
    """Модель, зацикленная на вызовах, не должна крутиться бесконечно."""
    registry = FakeRegistry(ok=True)
    looping = [LLMResponse(tool_calls=[ToolCallRequest(tool="geo", action="info", args={})])
               for _ in range(10)]
    gateway = ScriptedGateway(looping)
    Proposer(SPEC, gateway, registry, max_tool_iterations=2).run("вопрос")
    assert len(registry.calls) == 2


def test_no_tools_means_plain_answer():
    gateway = ScriptedGateway([LLMResponse(text="просто ответ")])
    proposal = Proposer(SPEC, gateway, registry=None).run("вопрос")
    assert proposal.answer == "просто ответ" and proposal.tool_results == []


def test_events_are_emitted_for_ui():
    registry = FakeRegistry(ok=True)
    gateway = ScriptedGateway([
        LLMResponse(tool_calls=[ToolCallRequest(tool="geo", action="info", args={})]),
        LLMResponse(text="ок"),
    ])
    events: list[dict] = []
    Proposer(SPEC, gateway, registry, on_event=events.append).run("вопрос")
    kinds = {e["kind"] for e in events}
    assert {"model_call", "tool_call", "tool_result", "done"} <= kinds


def test_format_tool_result_truncates_huge_payload():
    huge = ToolCallResult(call_id="c", tool="geo", action="info", ok=True,
                          data={"rows": ["x" * 100000]}, summary="много данных")
    text = _format_tool_result(huge)
    # Контекст вырос до 32K токенов, поэтому cap поднят с 4000 до 20000 символов.
    assert "обрезано" in text and len(text) < 21000


# ---- Verifier ------------------------------------------------------------


def _p(pid: str, answer: str, ok: bool = True) -> Proposal:
    return Proposal(proposer_id=pid, model=f"m-{pid}", answer=answer, ok=ok,
                    error=None if ok else "сбой")


def test_two_of_three_agree_gives_consensus_without_llm():
    gateway = ScriptedGateway([])  # LLM не должна вызываться вовсе
    proposals = [
        _p("a", "В слое 1 объект, CRS EPSG:32636"),
        _p("b", "В слое 1 объект, CRS EPSG:32636."),
        _p("c", "Слой содержит 5 объектов в EPSG:4326"),
    ]
    verdict = Verifier(gateway).verify("вопрос", proposals)
    assert verdict.consensus is True
    assert set(verdict.agreeing) == {"a", "b"}
    assert verdict.dissenting == ["c"]
    assert verdict.escalate_to_judge is False
    assert gateway.seen_messages == []


def test_conflicting_facts_block_cheap_consensus():
    """Похожий текст, но разные числа — объединять нельзя."""
    verifier = Verifier(ScriptedGateway([]), use_llm=False)
    proposals = [
        _p("a", "Количество объектов: 1"),
        _p("b", "Количество объектов: 7"),
        _p("c", "Не знаю"),
    ]
    verdict = verifier.verify("вопрос", proposals)
    assert verdict.consensus is False and verdict.escalate_to_judge is True


def test_all_three_agree():
    verdict = Verifier(ScriptedGateway([])).verify("в", [
        _p("a", "ответ один"), _p("b", "ответ один"), _p("c", "ответ один")])
    assert verdict.consensus is True and len(verdict.agreeing) == 3


def test_disagreement_escalates_via_llm():
    gateway = ScriptedGateway([LLMResponse(
        text='{"agreeing":["a","c"],"dissenting":["b"],"consensus":true,'
             '"chosen":"a","reason":"по сути совпадают"}')])
    proposals = [_p("a", "первый вариант"), _p("b", "совершенно иное"),
                 _p("c", "третий текст")]
    verdict = Verifier(gateway).verify("вопрос", proposals)
    assert verdict.consensus is True and verdict.chosen_answer == "первый вариант"
    assert gateway.seen_messages, "Verifier должен был обратиться к модели"


def test_llm_verdict_requires_at_least_two_agreeing():
    gateway = ScriptedGateway([LLMResponse(
        text='{"agreeing":["a"],"consensus":true,"chosen":"a","reason":"один"}')])
    verdict = Verifier(gateway).verify("в", [
        _p("a", "раз"), _p("b", "два"), _p("c", "три")])
    assert verdict.consensus is False and verdict.escalate_to_judge is True


def test_verifier_llm_failure_escalates_to_judge():
    gateway = ScriptedGateway([LLMResponse(ok=False, error="verifier недоступен")])
    verdict = Verifier(gateway).verify("в", [_p("a", "раз"), _p("b", "два"), _p("c", "три")])
    assert verdict.escalate_to_judge is True and "не ответил" in verdict.reason


def test_single_answer_escalates():
    verdict = Verifier(ScriptedGateway([])).verify("в", [
        _p("a", "единственный"), _p("b", "", ok=False), _p("c", "", ok=False)])
    assert verdict.consensus is False and verdict.escalate_to_judge is True
    assert verdict.chosen_answer == "единственный"


def test_all_failed_gives_no_consensus():
    verdict = Verifier(ScriptedGateway([])).verify("в", [
        _p("a", "", ok=False), _p("b", "", ok=False)])
    assert verdict.consensus is False and "Ни один" in verdict.reason


# ---- Judge ---------------------------------------------------------------

_JUDGE_TEXT = "ОТВЕТ: 1 объект, EPSG:32636\nОБОСНОВАНИЕ: подтверждено инструментом"


def test_judge_prefers_cloud_when_available():
    gateway = ScriptedGateway([LLMResponse(text=_JUDGE_TEXT)], cloud_allowed=True)
    final = Judge(gateway).decide("вопрос", [_p("a", "1 объект")],
                                  Verdict(consensus=False, escalate_to_judge=True))
    assert final.decided_by == "judge_cloud"
    assert final.used_cloud is True and final.model == "cloud-model"
    assert final.answer == "1 объект, EPSG:32636"
    assert final.rationale.startswith("подтверждено")


def test_judge_falls_back_to_local_when_cloud_blocked():
    gateway = ScriptedGateway([LLMResponse(text=_JUDGE_TEXT)], cloud_allowed=False)
    final = Judge(gateway).decide("вопрос", [_p("a", "1 объект")], None)
    assert final.decided_by == "judge_local"
    assert final.used_cloud is False and final.model == "local-model"


def test_local_only_mode_never_touches_cloud():
    gateway = ScriptedGateway([LLMResponse(text=_JUDGE_TEXT)], cloud_allowed=True)
    final = Judge(gateway).decide("в", [_p("a", "ответ")], None, task_mode="local_only")
    assert final.used_cloud is False and final.model == "local-model"


def test_cloud_failure_falls_back_to_local():
    gateway = ScriptedGateway([
        LLMResponse(ok=False, error="429 rate limit"),   # облако
        LLMResponse(text=_JUDGE_TEXT),                   # локальная модель
    ], cloud_allowed=True)
    final = Judge(gateway).decide("в", [_p("a", "ответ")], None)
    assert final.decided_by == "judge_local" and final.used_cloud is False


def test_judge_can_ask_local_subagent():
    """agents-as-tools в обратную сторону: облачный судья зовёт локальную модель."""
    asked: list[tuple[str, str]] = []

    def runner(proposer_id: str, question: str) -> str:
        asked.append((proposer_id, question))
        return "Локальная модель уточнила: EPSG:32636"

    gateway = ScriptedGateway([
        LLMResponse(tool_calls=[ToolCallRequest(
            tool="local_agent", action="ask",
            args={"proposer_id": "proposer_a", "question": "уточни CRS"})]),
        LLMResponse(text=_JUDGE_TEXT),
    ], cloud_allowed=True)
    final = Judge(gateway, subagent_runner=runner).decide("в", [_p("a", "ответ")], None)
    assert asked == [("proposer_a", "уточни CRS")]
    assert final.subagent_calls and "proposer_a" in final.subagent_calls[0]
    subagent_message = [m for m in gateway.seen_messages[1] if m.get("role") == "tool"][0]
    assert "EPSG:32636" in subagent_message["content"]


def test_judge_subagents_disabled_by_flag():
    gateway = ScriptedGateway([LLMResponse(text=_JUDGE_TEXT)], cloud_allowed=True)
    judge = Judge(gateway, subagent_runner=None, allow_subagents=False)
    tools = judge._tools_for_judge(None)
    assert all(t["function"]["name"] != "local_agent__ask" for t in tools)


def test_judge_can_call_platform_tools():
    registry = FakeRegistry(ok=True)
    gateway = ScriptedGateway([
        LLMResponse(tool_calls=[ToolCallRequest(tool="geo", action="info", args={"path": "a.shp"})]),
        LLMResponse(text=_JUDGE_TEXT),
    ], cloud_allowed=True)
    final = Judge(gateway, registry=registry).decide("в", [_p("a", "ответ")], None)
    assert len(registry.calls) == 1 and final.decided_by == "judge_cloud"


def test_consensus_answer_returned_when_all_judges_fail():
    gateway = ScriptedGateway([
        LLMResponse(ok=False, error="облако упало"),
        LLMResponse(ok=False, error="локальная модель упала"),
    ], cloud_allowed=True)
    verdict = Verdict(consensus=True, chosen_answer="ответ из голосования",
                      escalate_to_judge=True)
    final = Judge(gateway).decide("в", [_p("a", "ответ")], verdict)
    assert final.decided_by == "fallback"
    assert final.answer == "ответ из голосования"


@pytest.mark.parametrize("text,answer", [
    ("ОТВЕТ: раз\nОБОСНОВАНИЕ: два", "раз"),
    ("ответ: строчными\nобоснование: тоже", "строчными"),
    ("Просто текст без разметки", "Просто текст без разметки"),
])
def test_split_answer_variants(text, answer):
    assert _split_answer(text)[0] == answer
