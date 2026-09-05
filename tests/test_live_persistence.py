"""
Регрессионный тест: результат шага (и связанный с ним журнал вызовов
инструментов) должен попадать в блэкборд СРАЗУ по готовности шага, а не
одним пакетом после завершения ВСЕГО плана.

Баг воспроизведён и подтверждён на живой задаче 87eef3701975 (20.08.2026):
шаг s8 упал, но answer/error/tool_calls_log оставались пустыми, пока не
завершились независимые s9/s10/s11 — задачу было невозможно диагностировать
во время выполнения.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.loader import load_models, load_routing_rules, load_settings  # noqa: E402
from core.schemas import ExecutionMode, Plan, PlanStep, Task  # noqa: E402
from workflows.main_graph import Platform, Workflow  # noqa: E402


@pytest.fixture()
def platform(tmp_path, monkeypatch):
    settings = load_settings()
    settings["paths"] = dict(settings.get("paths") or {})
    settings["paths"]["blackboard_db"] = str(tmp_path / "test.sqlite3")
    settings["paths"]["memory_db"] = str(tmp_path / "memory.sqlite3")
    plat = Platform(settings=settings, models=load_models(),
                    routing_rules=load_routing_rules())
    yield plat
    plat.blackboard.close()


def test_step_result_is_persisted_before_plan_finishes(platform, monkeypatch):
    """
    s2 зависит от s1, поэтому выполняется в отдельной, более поздней волне.
    В момент, когда исполнитель s2 реально запускается, результат s1 уже
    должен быть виден в блэкборде — до того, как весь план (обе волны)
    завершится и вернёт управление в _execute_plan/run().
    """
    task = Task(prompt="x", mode=ExecutionMode.ORCHESTRATED)
    platform.blackboard.create_task(task)

    class FakeProposal:
        def __init__(self, text: str):
            self.answer = text
            self.ok = True
            self.error = None
            self.tool_results = []

    seen_s1_before_s2_started = {"value": None}

    class FakeProposer:
        def __init__(self, spec, *args, **kwargs):
            self.spec = spec

        def run(self, prompt, context_summary="", allowed=None, mode=None):
            if "ТЕПЕРЬ ВЫПОЛНИ" in prompt or "Результат шага s1" in prompt:
                # Это вызов для s2 (зависимого шага) — проверяем, что s1
                # уже сохранён в БД именно СЕЙЧАС, а не после всего плана.
                rows = platform.blackboard.get_step_results(task.task_id)
                seen_s1_before_s2_started["value"] = any(
                    r["step_id"] == "s1" and r.get("answer") for r in rows)
                return FakeProposal("готово s2")
            return FakeProposal("готово s1")

    monkeypatch.setattr("workflows.main_graph.Proposer", FakeProposer)

    plan = Plan(steps=[
        PlanStep(step_id="s1", title="A", instruction="a", assignee="proposer_a"),
        PlanStep(step_id="s2", title="B", instruction="b",
                 assignee="proposer_b", depends_on=["s1"]),
    ])
    state = {"task": task, "context_summary": ""}

    results = Workflow(platform)._execute_plan(state, plan)

    assert [r.step_id for r in results] == ["s1", "s2"]
    assert seen_s1_before_s2_started["value"] is True, (
        "Результат шага s1 должен быть записан в блэкборд до запуска "
        "исполнителя s2, а не только после завершения всего плана")

    # И финальная проверка: обе строки реально лежат в БД после выполнения.
    rows = platform.blackboard.get_step_results(task.task_id)
    assert {r["step_id"] for r in rows} == {"s1", "s2"}
