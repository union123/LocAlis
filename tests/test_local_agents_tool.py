"""
Тесты инструмента «Локальные агенты».

Модели не вызываются: контекст платформы подменяется заглушками, поэтому
тесты быстрые и проверяют именно логику инструмента.

Главное, что защищаем:
  - инструмент честно сообщает об отсутствии контекста, а не падает;
  - неточный идентификатор агента от модели не ломает вызов;
  - под-агенту не выдаётся сам local_agents (защита от рекурсии);
  - сбой одного агента не отменяет остальных.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schemas import ToolCallRequest, ToolStatus  # noqa: E402
from core.tool_context import ScratchMemory, build_context  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "local_agents_under_test", ROOT / "tools" / "local_agents" / "agents_tool.py")
module = importlib.util.module_from_spec(_spec)
sys.modules["local_agents_under_test"] = module
_spec.loader.exec_module(module)
LocalAgentsTool = module.LocalAgentsTool

AGENTS = [
    {"id": "proposer_a", "model": "ministral-3:8b", "purpose": "универсальный"},
    {"id": "proposer_b", "model": "OxW/Qwen3-8b-ru", "purpose": "русский язык"},
    {"id": "proposer_c", "model": "qwen2.5-coder:7b", "purpose": "структуры"},
]


class Fakes:
    """Заглушки возможностей платформы + журнал обращений."""

    def __init__(self, fail_ids: set[str] | None = None):
        self.fail_ids = fail_ids or set()
        self.asked: list[tuple[str, str]] = []
        self.ran: list[tuple[str, str, list[str] | None]] = []
        self.events: list[dict] = []

    def ask(self, model_id, prompt, system="", temperature=None):
        self.asked.append((model_id, prompt))
        if model_id in self.fail_ids:
            return {"ok": False, "error": "модель не отвечает"}
        return {"ok": True, "text": f"ответ {model_id}: одинаковый вывод про данные",
                "model": f"m-{model_id}", "agent_id": model_id, "latency_ms": 5}

    def run(self, model_id, task, allowed_tools=None):
        self.ran.append((model_id, task, allowed_tools))
        if model_id in self.fail_ids:
            return {"ok": False, "error": "под-агент упал"}
        return {"ok": True, "text": f"выполнено {model_id}", "model": f"m-{model_id}",
                "tool_facts": ["qgis.layer_info: 1 объект, EPSG:32636"]}

    def list_models(self):
        return list(AGENTS)


def make_tool(fakes: Fakes | None = None, with_run: bool = True,
              config: dict | None = None) -> LocalAgentsTool:
    fakes = fakes or Fakes()
    context = build_context(
        ask_local_model=fakes.ask,
        list_local_models=fakes.list_models,
        run_local_agent=fakes.run if with_run else None,
        memory=ScratchMemory(),
        emit_event=fakes.events.append,
    )
    tool = LocalAgentsTool(config=config or {}, context=context)
    tool._fakes = fakes  # для проверок в тестах
    return tool


# ---- готовность и контракт ----------------------------------------------


def test_no_context_reports_unavailable():
    """Без контекста инструмент не должен выглядеть работоспособным."""
    status, message = LocalAgentsTool().health()
    assert status is ToolStatus.UNAVAILABLE
    assert "контекста" in message


def test_ready_with_context():
    status, message = make_tool().health()
    assert status is ToolStatus.READY and "3" in message


def test_empty_model_list_is_unavailable():
    fakes = Fakes()
    fakes.list_models = lambda: []
    tool = make_tool(fakes)
    assert tool.health()[0] is ToolStatus.UNAVAILABLE


def test_all_actions_have_valid_schema():
    tool = make_tool()
    names = {a.name for a in tool.actions()}
    assert names == {"list_agents", "ask_agent", "run_agent", "second_opinion",
                     "debate", "delegate_subtasks", "remember", "recall",
                     "remember_fact", "recall_fact", "search_facts", "search_past",
                     "get_task_history"}
    for action in tool.actions():
        assert action.parameters["type"] == "object"
        assert action.description


def test_requires_context_flag_is_set():
    assert LocalAgentsTool.requires_context is True


# ---- выбор агента --------------------------------------------------------


@pytest.mark.parametrize("given,expected", [
    ("proposer_b", "proposer_b"),
    ("proposer b", "proposer_b"),        # модель пишет с пробелом
    ("PROPOSER_C", "proposer_c"),        # регистр
    ("ministral", "proposer_a"),         # по имени модели
    ("qwen2.5-coder:7b", "proposer_c"),  # полное имя модели
    ("", "proposer_a"),                  # не указан -> первый
    ("несуществующий", "proposer_a"),    # неизвестный -> первый
])
def test_agent_id_resolution(given, expected):
    assert make_tool()._resolve(given) == expected


# ---- ask_agent ----------------------------------------------------------


def test_ask_agent_returns_answer():
    tool = make_tool()
    result = tool.run(ToolCallRequest(tool="local_agents", action="ask_agent",
                                      args={"agent_id": "proposer_b", "question": "вопрос"}))
    assert result.ok and result.data["agent_id"] == "proposer_b"
    assert "proposer_b" in result.data["answer"]


def test_ask_agent_requires_question():
    result = make_tool().run(ToolCallRequest(tool="local_agents", action="ask_agent",
                                             args={"agent_id": "proposer_a"}))
    assert result.ok is False and "вопрос" in result.error


def test_ask_agent_failure_is_reported():
    tool = make_tool(Fakes(fail_ids={"proposer_a"}))
    result = tool.run(ToolCallRequest(tool="local_agents", action="ask_agent",
                                      args={"agent_id": "proposer_a", "question": "в"}))
    assert result.ok is False and "не ответил" in result.error


def test_role_is_passed_as_system_prompt():
    tool = make_tool()
    tool.execute("ask_agent", {"agent_id": "proposer_a", "question": "в", "role": "геолог"})
    assert "геолог" in tool._role_system("геолог")


def test_events_emitted_for_ui():
    tool = make_tool()
    tool.execute("ask_agent", {"agent_id": "proposer_a", "question": "в"})
    kinds = {e["kind"] for e in tool._fakes.events}
    assert {"subagent_call", "subagent_result"} <= kinds


# ---- run_agent (под-агент с инструментами) -------------------------------


def test_run_agent_uses_tools():
    tool = make_tool()
    result = tool.run(ToolCallRequest(tool="local_agents", action="run_agent",
                                      args={"agent_id": "proposer_c", "task": "задача",
                                            "tools": ["qgis"]}))
    assert result.ok
    assert tool._fakes.ran == [("proposer_c", "задача", ["qgis"])]
    assert "EPSG:32636" in result.summary


def test_run_agent_accepts_comma_separated_tools():
    """Модели иногда присылают строку вместо списка."""
    tool = make_tool()
    tool.execute("run_agent", {"agent_id": "proposer_a", "task": "з", "tools": "qgis, excel"})
    assert tool._fakes.ran[0][2] == ["qgis", "excel"]


def test_run_agent_falls_back_to_ask_when_nested_tools_disabled():
    """Флаг в манифесте запрещает под-агентам инструменты — работаем без них."""
    tool = make_tool(config={"allow_nested_tools": False})
    result = tool.run(ToolCallRequest(tool="local_agents", action="run_agent",
                                      args={"agent_id": "proposer_a", "task": "задача"}))
    assert result.ok
    assert tool._fakes.ran == []            # инструментальный путь не использован
    assert tool._fakes.asked                 # использован обычный вопрос
    assert "отключены" in result.summary


def test_run_agent_without_capability_falls_back():
    tool = make_tool(with_run=False)
    result = tool.run(ToolCallRequest(tool="local_agents", action="run_agent",
                                      args={"task": "задача"}))
    assert result.ok and tool._fakes.asked


def test_run_agent_failure_reported():
    tool = make_tool(Fakes(fail_ids={"proposer_a"}))
    result = tool.run(ToolCallRequest(tool="local_agents", action="run_agent",
                                      args={"agent_id": "proposer_a", "task": "з"}))
    assert result.ok is False and "не справился" in result.error


def test_run_agent_exception_does_not_escape():
    fakes = Fakes()

    def boom(*_args, **_kwargs):
        raise RuntimeError("внутренний сбой")

    fakes.run = boom
    tool = make_tool(fakes)
    result = tool.run(ToolCallRequest(tool="local_agents", action="run_agent",
                                      args={"task": "з"}))
    assert result.ok is False and "внутренний сбой" in result.error


# ---- second_opinion ------------------------------------------------------


def test_second_opinion_polls_all_agents():
    tool = make_tool()
    result = tool.run(ToolCallRequest(tool="local_agents", action="second_opinion",
                                      args={"question": "вопрос"}))
    assert result.ok
    assert set(result.data["answers"]) == {"proposer_a", "proposer_b", "proposer_c"}
    assert 0.0 <= result.data["agreement"] <= 1.0


def test_identical_answers_give_full_agreement():
    fakes = Fakes()
    fakes.ask = lambda model_id, prompt, system="", temperature=None: {
        "ok": True, "text": "в слое один объект, система координат EPSG:32636",
        "model": model_id}
    result = make_tool(fakes).execute("second_opinion", {"question": "в"})
    assert result["data"]["verdict"] == "мнения совпадают"
    assert result["data"]["agreement"] == 1.0


def test_second_opinion_respects_participant_limit():
    tool = make_tool(config={"max_debate_participants": 2})
    result = tool.run(ToolCallRequest(tool="local_agents", action="second_opinion",
                                      args={"question": "в"}))
    assert len(result.data["answers"]) == 2


def test_second_opinion_specific_agents():
    tool = make_tool()
    result = tool.run(ToolCallRequest(tool="local_agents", action="second_opinion",
                                      args={"question": "в", "agents": ["proposer_b"]}))
    assert list(result.data["answers"]) == ["proposer_b"]


def test_second_opinion_survives_partial_failure():
    """Сбой одной модели не должен отменять опрос остальных."""
    tool = make_tool(Fakes(fail_ids={"proposer_b"}))
    result = tool.run(ToolCallRequest(tool="local_agents", action="second_opinion",
                                      args={"question": "в"}))
    assert result.ok
    assert "proposer_b" in result.data["failed"]
    assert len(result.data["answers"]) == 2


def test_second_opinion_all_failed():
    tool = make_tool(Fakes(fail_ids={"proposer_a", "proposer_b", "proposer_c"}))
    result = tool.run(ToolCallRequest(tool="local_agents", action="second_opinion",
                                      args={"question": "в"}))
    assert result.ok is False and "Ни один" in result.error


def test_disagreement_is_detected():
    fakes = Fakes()
    texts = {"proposer_a": "объектов ровно один, система координат UTM зона 36",
             "proposer_b": "совершенно другое утверждение про погоду и облака",
             "proposer_c": "третий несвязанный текст о музыке и театре"}

    def ask(model_id, prompt, system="", temperature=None):
        return {"ok": True, "text": texts[model_id], "model": model_id}

    fakes.ask = ask
    result = make_tool(fakes).execute("second_opinion", {"question": "в"})
    assert result["data"]["verdict"] == "мнения существенно расходятся"
    assert result["data"]["agreement"] < 0.5


# ---- debate --------------------------------------------------------------


def test_debate_runs_three_stages():
    """Предложение -> критика -> уточнение с учётом критики."""
    tool = make_tool()
    result = tool.run(ToolCallRequest(tool="local_agents", action="debate",
                                      args={"question": "спорный вопрос",
                                            "proposer": "proposer_a",
                                            "critic": "proposer_b"}))
    assert result.ok
    assert result.data["proposer"] == "proposer_a"
    assert result.data["critic"] == "proposer_b"
    assert len(tool._fakes.asked) == 3
    # второй вызов должен содержать текст предложения (это и есть критика)
    assert "ПРЕДЛОЖЕННОЕ РЕШЕНИЕ" in tool._fakes.asked[1][1]
    # третий — критику
    assert "КРИТИКА" in tool._fakes.asked[2][1]


def test_debate_critic_differs_from_proposer():
    """Критик обязан отличаться от автора, иначе критика бессмысленна."""
    tool = make_tool()
    result = tool.execute("debate", {"question": "в", "proposer": "proposer_a",
                                     "critic": "proposer_a"})
    assert result["data"]["critic"] != result["data"]["proposer"]


def test_debate_needs_question():
    result = make_tool().execute("debate", {})
    assert result["ok"] is False and "обсуждения" in result["error"]


def test_debate_fails_if_author_silent():
    tool = make_tool(Fakes(fail_ids={"proposer_a"}))
    result = tool.execute("debate", {"question": "в", "proposer": "proposer_a"})
    assert result["ok"] is False and "не ответил" in result["error"]


def test_debate_survives_silent_critic():
    """Если критик молчит, остаётся исходное предложение — не ошибка."""
    tool = make_tool(Fakes(fail_ids={"proposer_b"}))
    result = tool.execute("debate", {"question": "в", "proposer": "proposer_a",
                                     "critic": "proposer_b"})
    assert result["ok"] is True
    assert result["data"]["critique"] == ""
    assert result["data"]["final"] == result["data"]["proposal"]


def test_debate_needs_two_models():
    fakes = Fakes()
    fakes.list_models = lambda: [AGENTS[0]]
    result = make_tool(fakes).execute("debate", {"question": "в"})
    assert result["ok"] is False and "две разные" in result["error"]


# ---- delegate_subtasks ---------------------------------------------------


def test_subtasks_distributed_round_robin():
    """Без явных исполнителей подзадачи раскладываются по разным моделям."""
    tool = make_tool()
    result = tool.run(ToolCallRequest(tool="local_agents", action="delegate_subtasks",
                                      args={"subtasks": [{"task": "первая"},
                                                         {"task": "вторая"},
                                                         {"task": "третья"}]}))
    assert result.ok and result.data["completed"] == 3
    assigned = [r["agent_id"] for r in result.data["results"]]
    assert assigned == ["proposer_a", "proposer_b", "proposer_c"]


def test_subtasks_keep_original_order():
    tool = make_tool()
    result = tool.execute("delegate_subtasks", {"subtasks": [
        {"task": "первая"}, {"task": "вторая"}]})
    tasks = [r["task"] for r in result["data"]["results"]]
    assert tasks == ["первая", "вторая"]


def test_subtasks_respect_limit():
    tool = make_tool(config={"max_subtasks": 2})
    result = tool.execute("delegate_subtasks",
                          {"subtasks": [{"task": f"з{i}"} for i in range(5)]})
    assert len(result["data"]["results"]) == 2
    assert "Пропущено сверх лимита: 3" in result["summary"]


def test_subtasks_with_tools_flag():
    tool = make_tool()
    tool.execute("delegate_subtasks", {"subtasks": [
        {"task": "с инструментами", "use_tools": True},
        {"task": "без инструментов"}]})
    assert len(tool._fakes.ran) == 1 and len(tool._fakes.asked) == 1


def test_subtasks_partial_failure_still_ok():
    tool = make_tool(Fakes(fail_ids={"proposer_b"}))
    result = tool.execute("delegate_subtasks", {"subtasks": [
        {"agent_id": "proposer_a", "task": "ок"},
        {"agent_id": "proposer_b", "task": "упадёт"}]})
    assert result["ok"] is True and result["data"]["completed"] == 1
    assert "Неудачи" in result["summary"]


def test_subtasks_all_failed():
    tool = make_tool(Fakes(fail_ids={"proposer_a"}))
    result = tool.execute("delegate_subtasks",
                          {"subtasks": [{"agent_id": "proposer_a", "task": "з"}]})
    assert result["ok"] is False


@pytest.mark.parametrize("payload", [
    {"subtasks": []},
    {"subtasks": [{"task": "   "}]},
    {},
])
def test_subtasks_rejects_empty_input(payload):
    result = make_tool().execute("delegate_subtasks", payload)
    assert result["ok"] is False


def test_subtasks_accepts_single_object_or_string():
    """Модели часто присылают объект или строку вместо списка."""
    tool = make_tool()
    assert tool.execute("delegate_subtasks", {"subtasks": {"task": "одна"}})["ok"]
    assert tool.execute("delegate_subtasks", {"subtasks": "строкой"})["ok"]


# ---- память --------------------------------------------------------------


def test_remember_and_recall_roundtrip():
    tool = make_tool()
    tool.execute("remember", {"key": "вывод", "value": "слой в EPSG:32636"})
    single = tool.execute("recall", {"key": "вывод"})
    assert single["ok"] and "EPSG:32636" in single["data"]["value"]
    everything = tool.execute("recall", {})
    assert everything["data"]["memory"] == {"вывод": "слой в EPSG:32636"}


def test_recall_missing_key():
    result = make_tool().execute("recall", {"key": "нет-такого"})
    assert result["ok"] is False and "нет записи" in result["error"]


def test_recall_empty_memory():
    result = make_tool().execute("recall", {})
    assert result["ok"] and "пуста" in result["summary"]


def test_remember_rejects_empty():
    assert make_tool().execute("remember", {"key": "", "value": "x"})["ok"] is False


def test_memory_shared_between_actions():
    """Один агент запомнил — другой должен прочитать (обмен внутри задачи).""" 
    tool = make_tool()
    tool.execute("remember", {"key": "общее", "value": "факт"})
    assert tool.execute("recall", {"key": "общее"})["data"]["value"] == "факт"


def test_unknown_action():
    result = make_tool().run(ToolCallRequest(tool="local_agents", action="нет_такого", args={}))
    assert result.ok is False


def test_missing_capability_gives_clear_error():
    """Без возможности инструмент объясняет причину, а не падает с AttributeError."""
    tool = LocalAgentsTool(context={})
    result = tool.run(ToolCallRequest(tool="local_agents", action="remember",
                                      args={"key": "k", "value": "v"}))
    assert result.ok is False
    assert "контекста платформы" in result.error
