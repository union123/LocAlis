"""
Тесты Blackboard: миграции, сохранение всех этапов, сборка сводки контекста.
Работают на временном файле БД, без внешних зависимостей.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.blackboard import SCHEMA_VERSION, Blackboard, _migration_1  # noqa: E402
from core.schemas import (  # noqa: E402
    ExecutionMode,
    FinalDecision,
    Proposal,
    RoutingDecision,
    Task,
    TaskComplexity,
    ToolCallResult,
    Verdict,
)


@pytest.fixture()
def bb(tmp_path: Path) -> Blackboard:
    board = Blackboard(tmp_path / "bb.sqlite3")
    yield board
    board.close()


def test_migration_creates_current_schema(bb: Blackboard):
    assert bb.schema_version == SCHEMA_VERSION


def test_migrate_is_idempotent(bb: Blackboard):
    assert bb.migrate() == SCHEMA_VERSION
    assert bb.migrate() == SCHEMA_VERSION


def test_upgrade_from_older_schema(tmp_path: Path):
    """Старая база v1 должна доехать до текущей версии без потери данных."""
    db = tmp_path / "old.sqlite3"
    conn = sqlite3.connect(str(db))
    _migration_1(conn)
    conn.execute(
        "CREATE TABLE schema_meta (id INTEGER PRIMARY KEY CHECK (id=1),"
        " schema_version INTEGER NOT NULL, updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO schema_meta VALUES (1, 1, '2020-01-01T00:00:00Z')")
    conn.execute(
        "INSERT INTO tasks (task_id, prompt, mode, status, created_at, updated_at)"
        " VALUES ('old1','старая задача','auto','done','2020-01-01','2020-01-01')")
    conn.commit()
    conn.close()

    board = Blackboard(db)
    assert board.schema_version == SCHEMA_VERSION
    task = board.get_task("old1")
    assert task is not None and task["prompt"] == "старая задача"
    # новая колонка появилась и имеет значение по умолчанию
    assert task["used_cloud"] == 0
    board.close()


def test_full_task_lifecycle(bb: Blackboard):
    task = Task(prompt="Опиши слой скважин", mode=ExecutionMode.LOCAL_ONLY)
    bb.create_task(task)
    bb.set_status(task.task_id, "running")

    routing = RoutingDecision(
        complexity=TaskComplexity.NEEDS_VERIFICATION, needs_tools=True,
        suggested_tools=["qgis"], reason="геоданные", decided_by="rules")
    bb.save_routing(task.task_id, routing)

    result = ToolCallResult(
        call_id="c1", tool="qgis", action="layer_info", ok=True,
        summary="Слой: 1 объект, EPSG:32636", tool_version="1.0.0", duration_ms=1800)
    proposal = Proposal(
        proposer_id="proposer_a", model="ministral-3:8b", answer="1 объект, EPSG:32636",
        tool_results=[result], latency_ms=2500)
    bb.add_proposal(task.task_id, proposal)

    verdict = Verdict(consensus=True, agreeing=["proposer_a", "proposer_b"],
                      chosen_answer="1 объект, EPSG:32636", escalate_to_judge=False,
                      reason="2 из 3 согласны", model="deepseek-v2:16b")
    bb.save_verdict(task.task_id, verdict)

    final = FinalDecision(answer="1 объект, EPSG:32636", rationale="консенсус",
                          decided_by="verifier_consensus", model="deepseek-v2:16b")
    bb.save_final(task.task_id, final)

    stored = bb.get_task(task.task_id)
    assert stored["status"] == "done"
    assert stored["routing"]["needs_tools"] is True
    assert stored["verdict"]["consensus"] is True
    assert stored["final"]["decided_by"] == "verifier_consensus"

    # вызовы инструментов попали в журнал автоматически вместе с proposal
    calls = bb.get_tool_calls(task.task_id)
    assert len(calls) == 1 and calls[0]["tool_version"] == "1.0.0"

    # журнал решений: created, routing, verify, final
    stages = [d["stage"] for d in bb.get_decisions(task.task_id)]
    assert stages == ["created", "routing", "verify", "final"]


def test_load_record_roundtrip(bb: Blackboard):
    task = Task(prompt="тест", mode=ExecutionMode.AUTO)
    bb.create_task(task)
    bb.add_proposal(task.task_id, Proposal(
        proposer_id="p1", model="m1", answer="ответ", latency_ms=10))
    record = bb.load_record(task.task_id)
    assert record is not None
    assert record.task.prompt == "тест"
    assert record.proposals[0].answer == "ответ"


def test_context_summary_is_deterministic_and_bounded(bb: Blackboard):
    task = Task(prompt="Проверь атрибуты", mode=ExecutionMode.LOCAL_ONLY)
    bb.create_task(task)
    bb.save_routing(task.task_id, RoutingDecision(needs_tools=True, reason="геоданные"))
    for i in range(8):
        bb.log_tool_call(task.task_id, "proposer_a", ToolCallResult(
            call_id=f"c{i}", tool="qgis", action="read_attributes", ok=True,
            summary=f"Прочитано {i} строк"))
    bb.log_tool_call(task.task_id, "proposer_a", ToolCallResult(
        call_id="cerr", tool="qgis", action="layer_info", ok=False,
        error="Файл слоя не найден"))

    first = bb.build_context_summary(task.task_id, target_tokens=400)
    second = bb.build_context_summary(task.task_id, target_tokens=400)
    assert first == second                      # детерминированность
    assert len(first) <= 400 * 3                # укладывается в бюджет
    assert "local_only" in first
    assert "ОШИБКА" in first                    # неудачи тоже видны модели
    assert "Прочитано 7 строк" in first         # показаны последние результаты


def test_refresh_context_summary_persists(bb: Blackboard):
    task = Task(prompt="тест", mode=ExecutionMode.AUTO)
    bb.create_task(task)
    summary = bb.refresh_context_summary(task.task_id)
    assert bb.get_task(task.task_id)["context_summary"] == summary


def test_stats_and_history(bb: Blackboard):
    for i in range(3):
        task = Task(prompt=f"задача {i}")
        bb.create_task(task)
        if i == 0:
            bb.save_final(task.task_id, FinalDecision(
                answer="a", decided_by="judge_cloud", used_cloud=True))
    stats = bb.stats()
    assert stats["tasks_total"] == 3
    assert stats["cloud_tasks"] == 1
    assert stats["by_status"]["created"] == 2
    history = bb.list_tasks(limit=2)
    assert len(history) == 2


def test_missing_task_returns_none(bb: Blackboard):
    assert bb.get_task("нет-такой") is None
    assert bb.load_record("нет-такой") is None
