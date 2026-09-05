"""
Тесты графа: порядок узлов, фича-флаги, политика конфиденциальности,
запись результата в Blackboard. Модели подменяются заглушкой.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.loader import load_models, load_routing_rules, load_settings  # noqa: E402
from core.blackboard import Blackboard  # noqa: E402
from core.llm_gateway import LLMResponse, ModelSpec  # noqa: E402
from core.schemas import ExecutionMode, Task  # noqa: E402
from workflows.main_graph import Platform, Workflow  # noqa: E402


class StubGateway:
    """Шлюз-заглушка: все модели отвечают одинаково (консенсус 3 из 3)."""

    def __init__(self, answer: str = "Ответ: 1 объект, EPSG:32636", cloud: bool = False):
        self.answer = answer
        self.calls: list[str] = []

        class MC:
            def __init__(self, allowed):
                self.allowed = allowed
                self.mode = "auto"

            def status(self, task_mode=None, check_health=True):
                from config.resilience import CloudStatus
                return CloudStatus(allowed=self.allowed, reason="заглушка")

            def cloud_available(self, task_mode=None, check_health=True):
                return self.allowed

            def effective_mode(self, task_mode=None):
                return "local_only" if task_mode == "local_only" else "auto"

        self.mc = MC(cloud)

    def chat(self, spec, messages, tools=None, task_mode=None, timeout_sec=None):
        self.calls.append(spec.id)
        return LLMResponse(text=self.answer, model=spec.model, provider=spec.provider)

    def spec(self, role: str) -> ModelSpec:
        return ModelSpec(id=role, provider="stub", model=f"{role}-model")

    def proposer_specs(self, include_cloud: bool = False):
        specs = [ModelSpec(id=f"proposer_{c}", provider="stub", model=f"m-{c}")
                 for c in "abc"]
        if include_cloud:
            specs.append(ModelSpec(id="cloud_proposer_a", provider="openrouter", model="cloud-m"))
        return specs

    def judge_specs(self):
        return (ModelSpec(id="judge_cloud", provider="openrouter", model="cloud-judge"),
                ModelSpec(id="judge_local", provider="ollama", model="local-judge"))

    def embed(self, texts):
        return [[0.1] * 8 for _ in texts]


@pytest.fixture()
def platform(tmp_path: Path, monkeypatch) -> Platform:
    settings = load_settings()
    settings["paths"] = dict(settings.get("paths") or {},
                             blackboard_db=str(tmp_path / "bb.sqlite3"))
    plat = Platform(settings=settings, models=load_models(),
                    routing_rules=load_routing_rules())
    plat.rebind_gateway(StubGateway())
    plat.blackboard = Blackboard(tmp_path / "bb.sqlite3")
    return plat


def test_node_plan_order(platform: Platform):
    plan = [name for name, _ in Workflow(platform).node_plan()]
    assert plan == ["router", "secretary", "proposers", "verifier", "judge",
                    "secretary_persist"]


def test_retrieval_node_added_by_flag(platform: Platform):
    platform.settings["feature_flags"]["retrieval"] = True
    plan = [name for name, _ in Workflow(platform).node_plan()]
    assert plan[1] == "retrieval"       # до секретаря, чтобы попасть в сводку
    assert plan.index("retrieval") < plan.index("secretary")


def test_disabled_proposers_node_removed(platform: Platform):
    platform.settings["feature_flags"]["proposers"] = False
    assert "proposers" not in [n for n, _ in Workflow(platform).node_plan()]


def test_langgraph_compiles(platform: Platform):
    assert Workflow(platform).build_langgraph() is not None


def test_full_run_reaches_consensus_and_persists(platform: Platform):
    workflow = Workflow(platform)
    task = Task(prompt="Сколько объектов в слое?", mode=ExecutionMode.LOCAL_ONLY)
    state = workflow.run(task)

    assert state.get("error") is None
    assert len(state["proposals"]) == 3
    assert state["verdict"].consensus is True
    # консенсус -> арбитр не нужен
    assert state["final"].decided_by == "verifier_consensus"

    stored = platform.blackboard.get_task(task.task_id)
    assert stored["status"] == "done"
    assert stored["final"]["answer"] == state["final"].answer
    stages = [d["stage"] for d in platform.blackboard.get_decisions(task.task_id)]
    assert stages == ["created", "routing", "verify", "final"]


def test_simple_task_uses_single_proposer(platform: Platform):
    state = Workflow(platform).run(Task(prompt="привет"))
    assert state["routing"].needs_full_debate is False
    assert len(state["proposals"]) == 1


def test_sensitive_marker_forces_local_only(platform: Platform):
    workflow = Workflow(platform)
    task = Task(prompt="Обработай конфиденциально отчёт по участку", mode=ExecutionMode.AUTO)
    assert workflow.p.effective_mode(task) == "local_only"


def test_privacy_can_be_disabled_in_settings(platform: Platform):
    platform.settings["privacy"]["force_local_for_sensitive"] = False
    task = Task(prompt="конфиденциально: посчитай", mode=ExecutionMode.AUTO)
    assert Workflow(platform).p.effective_mode(task) == "auto"


def test_cloud_proposers_excluded_in_local_only(platform: Platform):
    platform.rebind_gateway(StubGateway(cloud=True))
    task = Task(prompt="Задача для проверки", mode=ExecutionMode.LOCAL_ONLY)
    state = Workflow(platform).run(task)
    assert all(p.provider != "openrouter" for p in state["proposals"])


def test_verifier_disabled_goes_to_judge(platform: Platform):
    platform.settings["feature_flags"]["verifier"] = False
    state = Workflow(platform).run(Task(prompt="Задача для проверки"))
    assert state["verdict"].reason == "Verifier отключён флагом"
    assert state["final"].decided_by in ("judge_local", "judge_cloud", "fallback")


def test_judge_disabled_uses_verifier_answer(platform: Platform):
    platform.settings["feature_flags"]["judge"] = False
    platform.settings["feature_flags"]["verifier"] = False
    state = Workflow(platform).run(Task(prompt="Задача для проверки"))
    assert state["final"].decided_by == "fallback"


def test_events_stream_for_ui(platform: Platform):
    events: list[dict] = []
    platform.on_event = events.append
    Workflow(platform).run(Task(prompt="Задача для проверки"))
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "task_start" and kinds[-1] == "task_end"
    nodes = [e.get("node") for e in events if e["kind"] == "node_start"]
    assert "router" in nodes and "proposers" in nodes


def test_failing_proposer_does_not_break_run(platform: Platform):
    class PartiallyFailing(StubGateway):
        def chat(self, spec, messages, tools=None, task_mode=None, timeout_sec=None):
            if spec.id == "proposer_b":
                return LLMResponse(ok=False, error="модель упала", model=spec.model)
            return super().chat(spec, messages, tools, task_mode, timeout_sec)

    platform.rebind_gateway(PartiallyFailing())
    state = Workflow(platform).run(Task(prompt="Задача для проверки"))
    # Считаем только локальных пропозеров a/b/c: облачный (если сеть и ключ
    # доступны) добавляет четвёртый ok-пропозал, что не относится к смыслу
    # теста "сбой одного не ломает остальных".
    local_ok = sum(1 for p in state["proposals"]
                   if p.ok and p.proposer_id.startswith("proposer_"))
    assert local_ok == 2
    assert any(p.proposer_id == "proposer_b" and not p.ok
               for p in state["proposals"])
    assert state["final"] is not None and state["final"].answer


def test_task_error_is_recorded_as_failed(platform: Platform):
    class Broken(StubGateway):
        def proposer_specs(self, include_cloud: bool = False):
            raise RuntimeError("конфиг моделей повреждён")

    platform.rebind_gateway(Broken())
    task = Task(prompt="Задача для проверки")
    state = Workflow(platform).run(task)
    assert "конфиг моделей повреждён" in (state.get("error") or "")
    assert platform.blackboard.get_task(task.task_id)["status"] == "failed"
