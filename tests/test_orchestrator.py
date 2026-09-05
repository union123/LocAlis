"""
Тесты режима «Оркестратор».

Проверяем главные риски этого режима:
  - планировщик возвращает JSON в ```-обёртке и с мусором вокруг;
  - модель придумывает несуществующих исполнителей и инструменты;
  - зависимости шагов образуют цикл (иначе выполнение зависло бы навсегда);
  - при полном отказе планировщика задача всё равно выполняется одним шагом;
  - при отказе сборщика пользователь получает работу исполнителей, а не ошибку;
  - конфиденциальные задачи не уходят в облако даже при placement=cloud.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.orchestrator import (  # noqa: E402
    Orchestrator, _extract_absolute_paths, _extract_json, _split_answer,
)
from core.llm_gateway import LLMResponse, ModelSpec  # noqa: E402
from core.schemas import (  # noqa: E402
    ExecutionMode,
    OrchestratorPlacement,
    Plan,
    PlanStep,
    StepResult,
    Task,
    ToolCallResult,
)

AGENTS = [
    {"id": "proposer_a", "model": "m-a", "purpose": "универсальный"},
    {"id": "proposer_b", "model": "m-b", "purpose": "русский язык"},
]
TOOLS = [{"name": "files", "description": "чтение файлов"},
         {"name": "datafiles", "description": "создание файлов"}]


class PlannerGateway:
    """Шлюз-заглушка: отдаёт заготовленные ответы по очереди."""

    def __init__(self, script: list[LLMResponse]):
        self.script = list(script)
        self.seen: list[tuple[str, list[dict]]] = []
        self.cloud = ModelSpec(id="judge_cloud", provider="openrouter", model="cloud-model")
        self.local = ModelSpec(id="judge_local", provider="ollama", model="local-model")

    def chat(self, spec, messages, tools=None, task_mode=None, timeout_sec=None):
        self.seen.append((spec.model, [dict(m) for m in messages]))
        if not self.script:
            return LLMResponse(text="ОТВЕТ: итог\nОБОСНОВАНИЕ: из шагов",
                               model=spec.model, provider=spec.provider)
        response = self.script.pop(0)
        response.model = spec.model
        response.provider = spec.provider
        return response

    def judge_specs(self):
        return self.cloud, self.local

    # --- выбор модели планировщика (как в LLMGateway) ---
    def orchestrator_spec(self):
        return self.local

    def orchestrator_candidates(self):
        return [self.local,
                ModelSpec(id="orchestrator_local", provider="ollama",
                          model="planner-alt")]

    def orchestrator_spec_for(self, model):
        if not model:
            return self.orchestrator_spec()
        for spec in self.orchestrator_candidates():
            if spec.model == model:
                return spec
        return self.orchestrator_spec()

    def cloud_judge_chain(self):
        return [self.cloud]

    def proposer_specs(self, include_cloud: bool = False):
        return [ModelSpec(id=a["id"], provider="ollama", model=a["model"]) for a in AGENTS]


PLAN_JSON = """```json
{"reasoning": "сначала прочитать, потом посчитать",
 "steps": [
   {"step_id": "s1", "title": "Прочитать файл", "instruction": "прочитай a.txt",
    "assignee": "proposer_a", "tools": ["files"], "depends_on": []},
   {"step_id": "s2", "title": "Посчитать", "instruction": "посчитай строки",
    "assignee": "proposer_b", "tools": [], "depends_on": ["s1"]}
 ]}
```
Надеюсь, план подойдёт."""


# ---- разбор ответа планировщика ------------------------------------------


def test_extract_json_handles_fences_and_trailing_text():
    data = _extract_json(PLAN_JSON)
    assert data is not None
    assert [s["step_id"] for s in data["steps"]] == ["s1", "s2"]


def test_extract_json_returns_none_on_garbage():
    assert _extract_json("никакого JSON тут нет") is None
    assert _extract_json("") is None


def test_split_answer_without_markup_keeps_text():
    assert _split_answer("просто ответ") == ("просто ответ", "")


# ---- планирование ---------------------------------------------------------


def test_plan_parses_steps_and_dependencies():
    gateway = PlannerGateway([LLMResponse(text=PLAN_JSON)])
    plan = Orchestrator(gateway, placement=OrchestratorPlacement.LOCAL).plan(
        "прочитай и посчитай", AGENTS, TOOLS)
    assert plan.ok
    assert [s.step_id for s in plan.steps] == ["s1", "s2"]
    assert plan.steps[1].depends_on == ["s1"]
    assert plan.steps[0].tools == ["files"]
    assert plan.placement == OrchestratorPlacement.LOCAL


def test_plan_rejects_unknown_assignee_and_tools():
    """Модель придумывает исполнителей и инструменты — это надо чинить молча."""
    bad = ('{"steps": [{"step_id": "s1", "instruction": "сделай", '
           '"assignee": "gpt-9000", "tools": ["telepathy", "files"]}]}')
    plan = Orchestrator(PlannerGateway([LLMResponse(text=bad)])).plan("x", AGENTS, TOOLS)
    assert plan.steps[0].assignee in {a["id"] for a in AGENTS}
    assert plan.steps[0].tools == ["files"]


def test_plan_drops_dependency_cycles():
    """s1<->s2 без защиты означал бы вечное ожидание."""
    cyclic = ('{"steps": ['
              '{"step_id": "s1", "instruction": "a", "depends_on": ["s2"]},'
              '{"step_id": "s2", "instruction": "b", "depends_on": ["s1"]}]}')
    plan = Orchestrator(PlannerGateway([LLMResponse(text=cyclic)])).plan("x", AGENTS, TOOLS)
    assert plan.steps[0].depends_on == []
    assert plan.steps[1].depends_on == ["s1"]


def test_plan_drops_unknown_dependencies():
    raw = ('{"steps": [{"step_id": "s1", "instruction": "a", '
           '"depends_on": ["s99", "s1"]}]}')
    plan = Orchestrator(PlannerGateway([LLMResponse(text=raw)])).plan("x", AGENTS, TOOLS)
    assert plan.steps[0].depends_on == []


def test_plan_respects_max_steps():
    many = ('{"steps": [' + ",".join(
        f'{{"step_id": "s{i}", "instruction": "шаг {i}"}}' for i in range(1, 11)) + ']}')
    plan = Orchestrator(PlannerGateway([LLMResponse(text=many)]), max_steps=3).plan(
        "x", AGENTS, TOOLS)
    assert len(plan.steps) == 3


def test_plan_falls_back_to_single_step_when_planner_fails():
    """Отказ планировщика не должен отменять задачу целиком."""
    gateway = PlannerGateway([LLMResponse(ok=False, error="таймаут"),
                              LLMResponse(ok=False, error="и локально тоже")])
    plan = Orchestrator(gateway).plan("сделай всё", AGENTS, TOOLS)
    assert not plan.ok
    assert len(plan.steps) == 1
    assert plan.steps[0].instruction == "сделай всё"
    assert plan.error


def test_plan_falls_back_when_json_has_no_steps():
    gateway = PlannerGateway([LLMResponse(text='{"reasoning": "забыл шаги"}'),
                              LLMResponse(text="тоже не JSON")])
    plan = Orchestrator(gateway).plan("задача", AGENTS, TOOLS)
    assert not plan.ok and len(plan.steps) == 1


# ---- выбор облака/локали --------------------------------------------------


def test_placement_cloud_uses_cloud_planner_first():
    gateway = PlannerGateway([LLMResponse(text=PLAN_JSON)])
    plan = Orchestrator(gateway, placement=OrchestratorPlacement.CLOUD,
                        cloud_available=True).plan("x", AGENTS, TOOLS)
    assert plan.placement == OrchestratorPlacement.CLOUD
    assert gateway.seen[0][0] == "cloud-model"


def test_placement_cloud_degrades_to_local_without_cloud():
    """Нет облака — планируем локально, а не падаем."""
    gateway = PlannerGateway([LLMResponse(text=PLAN_JSON)])
    plan = Orchestrator(gateway, placement=OrchestratorPlacement.CLOUD,
                        cloud_available=False).plan("x", AGENTS, TOOLS)
    assert plan.placement == OrchestratorPlacement.LOCAL
    assert gateway.seen[0][0] == "local-model"


def test_placement_local_never_calls_cloud():
    gateway = PlannerGateway([LLMResponse(text=PLAN_JSON)])
    Orchestrator(gateway, placement=OrchestratorPlacement.LOCAL,
                 cloud_available=True).plan("x", AGENTS, TOOLS)
    assert all(model == "local-model" for model, _ in gateway.seen)


def test_cloud_planner_failure_falls_back_to_local():
    gateway = PlannerGateway([LLMResponse(ok=False, error="429 лимит"),
                              LLMResponse(text=PLAN_JSON)])
    plan = Orchestrator(gateway, placement=OrchestratorPlacement.CLOUD,
                        cloud_available=True).plan("x", AGENTS, TOOLS)
    assert plan.ok
    assert plan.placement == OrchestratorPlacement.LOCAL


# ---- выбор модели планировщика пользователем ------------------------------


def test_planner_model_choice_is_used():
    """Явно выбранная модель действительно планирует."""
    gateway = PlannerGateway([LLMResponse(text=PLAN_JSON)])
    plan = Orchestrator(gateway, placement=OrchestratorPlacement.LOCAL,
                        planner_model="planner-alt").plan("x", AGENTS, TOOLS)
    assert gateway.seen[0][0] == "planner-alt"
    assert plan.model == "planner-alt"


def test_unknown_planner_model_falls_back_to_default():
    """Устаревшее имя в настройках не должно отменять задачу."""
    gateway = PlannerGateway([LLMResponse(text=PLAN_JSON)])
    Orchestrator(gateway, placement=OrchestratorPlacement.LOCAL,
                 planner_model="нет-такой-модели").plan("x", AGENTS, TOOLS)
    assert gateway.seen[0][0] == "local-model"


def test_planner_model_does_not_apply_to_cloud():
    """Выбор касается ЛОКАЛЬНОГО планировщика: в облаке модель своя."""
    gateway = PlannerGateway([LLMResponse(text=PLAN_JSON)])
    Orchestrator(gateway, placement=OrchestratorPlacement.CLOUD,
                 cloud_available=True,
                 planner_model="planner-alt").plan("x", AGENTS, TOOLS)
    assert gateway.seen[0][0] == "cloud-model"


def test_planner_model_used_when_cloud_degrades():
    """Облако отвалилось — планируем выбранной локальной моделью, не дефолтной."""
    gateway = PlannerGateway([LLMResponse(ok=False, error="429 лимит"),
                              LLMResponse(text=PLAN_JSON)])
    plan = Orchestrator(gateway, placement=OrchestratorPlacement.CLOUD,
                        cloud_available=True,
                        planner_model="planner-alt").plan("x", AGENTS, TOOLS)
    assert [m for m, _ in gateway.seen] == ["cloud-model", "planner-alt"]
    assert plan.placement == OrchestratorPlacement.LOCAL


# ---- сборка итога ---------------------------------------------------------


def _results() -> list[StepResult]:
    return [
        StepResult(step_id="s1", title="Прочитать", assignee="proposer_a",
                   model="m-a", answer="в файле 42 строки", ok=True,
                   tool_results=[ToolCallResult(call_id="c1", tool="files",
                                                action="read_text", ok=True,
                                                summary="42 строки")]),
        StepResult(step_id="s2", title="Посчитать", assignee="proposer_b",
                   model="m-b", answer="", ok=False, error="модель не ответила"),
    ]


def test_assemble_builds_final_answer():
    gateway = PlannerGateway([LLMResponse(text="ОТВЕТ: 42 строки\nОБОСНОВАНИЕ: из шага s1")])
    final = Orchestrator(gateway, placement=OrchestratorPlacement.LOCAL).assemble(
        "сколько строк", Plan(steps=[]), _results())
    assert final.answer == "42 строки"
    assert final.decided_by == "orchestrator_local"
    assert final.subagent_calls == ["proposer_a:s1", "proposer_b:s2"]


def test_assemble_sees_step_facts_and_failures():
    """Сборщик обязан видеть и факты инструментов, и неудачные шаги."""
    gateway = PlannerGateway([LLMResponse(text="ОТВЕТ: ок")])
    Orchestrator(gateway, placement=OrchestratorPlacement.LOCAL).assemble(
        "вопрос", Plan(steps=[]), _results())
    user_text = gateway.seen[0][1][-1]["content"]
    assert "42 строки" in user_text
    assert "НЕ ВЫПОЛНЕН" in user_text


def test_assemble_returns_step_work_when_assembler_dead():
    """Сборщик недоступен — отдаём проделанную работу, а не сообщение об ошибке."""
    gateway = PlannerGateway([LLMResponse(ok=False, error="нет модели"),
                              LLMResponse(ok=False, error="и локальной нет")])
    final = Orchestrator(gateway, placement=OrchestratorPlacement.LOCAL).assemble(
        "вопрос", Plan(steps=[]), _results())
    assert "42 строки" in final.answer
    assert final.decided_by == "fallback"


def test_assemble_cloud_marks_used_cloud():
    gateway = PlannerGateway([LLMResponse(text="ОТВЕТ: готово")])
    final = Orchestrator(gateway, placement=OrchestratorPlacement.CLOUD,
                         cloud_available=True).assemble(
        "вопрос", Plan(steps=[]), _results())
    assert final.used_cloud is True
    assert final.decided_by == "orchestrator_cloud"


# ---- интеграция с графом --------------------------------------------------


@pytest.fixture()
def platform(tmp_path, monkeypatch):
    """Платформа с временной БД: тесты не должны трогать рабочий журнал."""
    from config.loader import load_models, load_routing_rules, load_settings
    from workflows.main_graph import Platform

    settings = load_settings()
    settings["paths"] = dict(settings.get("paths") or {})
    settings["paths"]["blackboard_db"] = str(tmp_path / "test.sqlite3")
    plat = Platform(settings=settings, models=load_models(),
                    routing_rules=load_routing_rules())
    yield plat
    plat.blackboard.close()


def test_graph_uses_orchestrator_node_only_in_that_mode(platform):
    from workflows.main_graph import Workflow

    workflow = Workflow(platform)
    auto_nodes = [n for n, _ in workflow.node_plan(
        Task(prompt="x", mode=ExecutionMode.AUTO))]
    orch_nodes = [n for n, _ in workflow.node_plan(
        Task(prompt="x", mode=ExecutionMode.ORCHESTRATED))]
    assert "proposers" in auto_nodes and "orchestrator" not in auto_nodes
    # В режиме оркестратора голосования нет: шаги решают разные подзадачи
    assert "orchestrator" in orch_nodes
    assert "verifier" not in orch_nodes and "judge" not in orch_nodes


def test_orchestrated_mode_does_not_break_cloud_policy(platform):
    """orchestrated — про архитектуру, а не про доступ к сети."""
    assert platform.effective_mode(
        Task(prompt="обычная задача", mode=ExecutionMode.ORCHESTRATED)) == "auto"


def test_sensitive_task_forces_local_planner(platform):
    """Конфиденциальность важнее явно выбранного облака."""
    task = Task(prompt="конфиденциально: посчитай объекты",
                mode=ExecutionMode.ORCHESTRATED,
                metadata={"orchestrator_placement": "cloud"})
    assert platform.orchestrator_placement(task) == OrchestratorPlacement.LOCAL
    assert platform.effective_mode(task) == "local_only"


def test_placement_from_task_metadata_wins_over_settings(platform):
    task = Task(prompt="задача", mode=ExecutionMode.ORCHESTRATED,
                metadata={"orchestrator_placement": "local"})
    assert platform.orchestrator_placement(task) == OrchestratorPlacement.LOCAL


def test_execute_plan_passes_previous_results_to_dependent_step(platform):
    """
    Шаг-потребитель обязан увидеть результат предшественника: малая модель
    не имеет доступа к состоянию графа, ей нужен текст.
    """
    from workflows.main_graph import Workflow

    captured: list[str] = []

    class FakeProposal:
        def __init__(self, text: str):
            self.answer = text
            self.ok = True
            self.error = None
            self.tool_results = []

    class FakeProposer:
        def __init__(self, spec, *args, **kwargs):
            self.spec = spec

        def run(self, prompt, context_summary="", allowed=None, mode=None):
            captured.append(prompt)
            return FakeProposal(f"готово: {prompt[:20]}")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("workflows.main_graph.Proposer", FakeProposer)
    try:
        plan = Plan(steps=[
            PlanStep(step_id="s1", title="Первый", instruction="прочитай файл",
                     assignee="proposer_a"),
            PlanStep(step_id="s2", title="Второй", instruction="посчитай",
                     assignee="proposer_b", depends_on=["s1"]),
        ])
        state = {"task": Task(prompt="x", mode=ExecutionMode.ORCHESTRATED),
                 "context_summary": ""}
        results = Workflow(platform)._execute_plan(state, plan)
    finally:
        monkeypatch.undo()

    assert [r.step_id for r in results] == ["s1", "s2"]
    assert all(r.ok for r in results)
    # Во втором задании должен быть результат первого шага
    assert "Результат шага s1" in captured[1]
    assert "ТЕПЕРЬ ВЫПОЛНИ" in captured[1]


def test_execute_plan_runs_independent_steps_and_survives_failure(platform):
    """Сбой одного шага не отменяет остальные."""
    from workflows.main_graph import Workflow

    class Boom:
        def __init__(self, spec, *args, **kwargs):
            self.spec = spec

        def run(self, prompt, context_summary="", allowed=None, mode=None):
            raise RuntimeError("модель упала")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("workflows.main_graph.Proposer", Boom)
    try:
        plan = Plan(steps=[
            PlanStep(step_id="s1", title="A", instruction="a", assignee="proposer_a"),
            PlanStep(step_id="s2", title="B", instruction="b", assignee="proposer_b"),
        ])
        state = {"task": Task(prompt="x", mode=ExecutionMode.ORCHESTRATED),
                 "context_summary": ""}
        results = Workflow(platform)._execute_plan(state, plan)
    finally:
        monkeypatch.undo()

    assert len(results) == 2
    assert all(not r.ok and "модель упала" in (r.error or "") for r in results)


def test_blackboard_stores_plan_and_steps(platform):
    """План и шаги должны переживать перезапуск: их читает экран истории."""
    task = Task(prompt="задача", mode=ExecutionMode.ORCHESTRATED)
    platform.blackboard.create_task(task)
    plan = Plan(steps=[PlanStep(step_id="s1", title="Шаг", instruction="сделай",
                                assignee="proposer_a", tools=["files"])],
                reasoning="замысел", model="planner-model",
                placement=OrchestratorPlacement.LOCAL)
    platform.blackboard.save_plan(task.task_id, plan)
    platform.blackboard.add_step_result(task.task_id, StepResult(
        step_id="s1", title="Шаг", assignee="proposer_a", model="m-a",
        answer="сделано", ok=True, latency_ms=120))

    record = platform.blackboard.load_record(task.task_id)
    assert record is not None
    assert record.plan is not None
    assert record.plan.steps[0].tools == ["files"]
    assert record.plan.placement == OrchestratorPlacement.LOCAL
    assert [s.step_id for s in record.step_results] == ["s1"]
    assert record.step_results[0].answer == "сделано"


# ---- состояние интерфейса -------------------------------------------------


def test_ui_state_tracks_step_progress():
    """
    Панель шагов на экране «Ход выполнения» строится из событий.
    Без этого пользователь минутами смотрел бы на пустой экран.
    """
    from ui.state import RunLog

    log = RunLog(prompt="x", mode="orchestrated", running=True)
    log.add({"kind": "node_start", "node": "orchestrator", "placement": "cloud"})
    log.add({"kind": "plan", "model": "planner", "reasoning": "замысел",
             "steps": [{"id": "s1", "title": "Первый", "assignee": "proposer_a",
                        "tools": ["files"], "depends_on": []},
                       {"id": "s2", "title": "Второй", "assignee": "proposer_b",
                        "tools": [], "depends_on": ["s1"]}]})
    assert log.placement == "cloud"
    assert [s["state"] for s in log.plan_steps] == ["waiting", "waiting"]

    log.add({"kind": "step_start", "step_id": "s1", "model": "m-a"})
    assert log.plan_steps[0]["state"] == "running"
    assert log.plan_steps[0]["model"] == "m-a"

    log.add({"kind": "step_end", "step_id": "s1", "ok": True, "ms": 900,
             "answer_preview": "готово"})
    log.add({"kind": "step_end", "step_id": "s2", "ok": False, "ms": 100,
             "error": "сбой модели"})
    assert log.plan_steps[0]["state"] == "done"
    assert log.plan_steps[0]["ms"] == 900
    assert log.plan_steps[1]["state"] == "failed"
    assert log.plan_steps[1]["error"] == "сбой модели"


def test_ui_state_ignores_unknown_step_ids():
    """События от посторонних шагов не должны ронять интерфейс."""
    from ui.state import RunLog

    log = RunLog()
    log.add({"kind": "plan", "steps": [{"id": "s1", "title": "A"}]})
    log.add({"kind": "step_end", "step_id": "s99", "ok": True, "ms": 5})
    assert log.plan_steps[0]["state"] == "waiting"
