"""
Регрессионные тесты Tool Registry и разбора вызовов инструментов.

Эти тесты не требуют ни QGIS, ни Ollama — они защищают контракт ядра
при добавлении новых инструментов и моделей.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.base_tool import BaseTool, ToolAction  # noqa: E402
from core.schemas import ToolCallRequest, ToolStatus  # noqa: E402
from core.tool_parsing import extract_tool_calls, parse_text_tool_calls  # noqa: E402
from core.tool_registry import ToolRegistry  # noqa: E402

PLUGIN_CODE = '''
from typing import Any
from core.base_tool import BaseTool, ToolAction

class EchoTool(BaseTool):
    name = "echo"
    version = "2.1.0"
    description = "Тестовый инструмент"

    def actions(self):
        return [ToolAction("say", "Повторить текст",
                {"type": "object", "properties": {"text": {"type": "string"}},
                 "required": ["text"]})]

    def execute(self, action: str, args: dict) -> dict:
        if args.get("text") == "boom":
            raise RuntimeError("специально падаем")
        return {"ok": True, "data": {"echo": args.get("text")}, "summary": "ок"}
'''

BROKEN_CODE = "this is not valid python ("


@pytest.fixture()
def tools_dir(tmp_path: Path) -> Path:
    """Временная папка плагинов: один рабочий, один сломанный, один выключенный."""
    good = tmp_path / "echo"
    good.mkdir()
    (good / "manifest.yaml").write_text(
        "name: echo\nversion: 1.0.0\nenabled: true\nentrypoint: tool.py\n", encoding="utf-8")
    (good / "tool.py").write_text(PLUGIN_CODE, encoding="utf-8")

    broken = tmp_path / "broken"
    broken.mkdir()
    (broken / "manifest.yaml").write_text(
        "name: broken\nenabled: true\nentrypoint: tool.py\n", encoding="utf-8")
    (broken / "tool.py").write_text(BROKEN_CODE, encoding="utf-8")

    off = tmp_path / "switched_off"
    off.mkdir()
    (off / "manifest.yaml").write_text(
        "name: switched_off\nenabled: false\nentrypoint: tool.py\n", encoding="utf-8")
    (off / "tool.py").write_text(PLUGIN_CODE, encoding="utf-8")
    return tmp_path


def test_discovery_finds_plugins(tools_dir: Path):
    reg = ToolRegistry(tools_dir)
    entries = reg.discover()
    assert set(entries) == {"echo", "broken", "switched_off"}


def test_broken_plugin_does_not_break_others(tools_dir: Path):
    reg = ToolRegistry(tools_dir)
    reg.discover()
    assert reg.get("broken").status is ToolStatus.ERROR
    assert reg.get("echo").usable is True
    assert [e.name for e in reg.available()] == ["echo"]


def test_disabled_plugin_is_not_loaded(tools_dir: Path):
    reg = ToolRegistry(tools_dir)
    reg.discover()
    entry = reg.get("switched_off")
    assert entry.status is ToolStatus.DISABLED
    assert entry.instance is None


def test_version_from_code_wins_over_manifest(tools_dir: Path):
    reg = ToolRegistry(tools_dir)
    reg.discover()
    assert reg.get("echo").manifest.version == "2.1.0"


def test_openai_tools_schema_shape(tools_dir: Path):
    reg = ToolRegistry(tools_dir)
    reg.discover()
    tools = reg.openai_tools()
    assert [t["function"]["name"] for t in tools] == ["echo__say"]
    assert tools[0]["function"]["parameters"]["required"] == ["text"]


def test_call_success_and_failure(tools_dir: Path):
    reg = ToolRegistry(tools_dir)
    reg.discover()

    ok = reg.call(ToolCallRequest(tool="echo", action="say", args={"text": "привет"}))
    assert ok.ok and ok.data["echo"] == "привет" and ok.tool_version == "2.1.0"

    # исключение внутри инструмента превращается в результат, а не падение
    crash = reg.call(ToolCallRequest(tool="echo", action="say", args={"text": "boom"}))
    assert crash.ok is False and "специально падаем" in crash.error

    unknown_action = reg.call(ToolCallRequest(tool="echo", action="nope", args={}))
    assert unknown_action.ok is False and "Неизвестное действие" in unknown_action.error

    unknown_tool = reg.call(ToolCallRequest(tool="ghost", action="say", args={}))
    assert unknown_tool.ok is False and "не найден" in unknown_tool.error


def test_call_by_openai_function_name(tools_dir: Path):
    reg = ToolRegistry(tools_dir)
    reg.discover()
    res = reg.call_openai_name("echo__say", {"text": "ок"})
    assert res.ok and res.action == "say"


# ---- разбор вызовов из ответов моделей ----------------------------------

KNOWN = {"qgis__layer_info", "echo__say"}


class _Fn:
    """Имитация Pydantic-объекта из клиента Ollama (не dict!)."""

    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments

    def model_dump(self):
        return {"name": self.name, "arguments": self.arguments}


class _Call:
    def __init__(self, fn):
        self.function = fn

    def model_dump(self):
        return {"function": self.function.model_dump()}


def test_native_tool_calls_from_pydantic_objects():
    """Регрессия: Ollama отдаёт объекты, а не словари."""
    message = {"tool_calls": [_Call(_Fn("qgis__layer_info", {"path": "a.shp"}))], "content": ""}
    calls = extract_tool_calls(message, KNOWN)
    assert len(calls) == 1
    assert calls[0].tool == "qgis" and calls[0].action == "layer_info"
    assert calls[0].args == {"path": "a.shp"}


@pytest.mark.parametrize("content", [
    '{"name": "qgis__layer_info", "arguments": {"path": "a.shp"}}',
    '<tool_call>{"name": "qgis__layer_info", "arguments": {"path": "a.shp"}}</tool_call>',
    'Сейчас вызову:\n```json\n{"name":"qgis__layer_info","arguments":{"path":"a.shp"}}\n```',
    '{"tool": "qgis", "action": "layer_info", "args": {"path": "a.shp"}}',
    '{"name": "qgis.layer_info", "parameters": {"path": "a.shp"}}',
    '{"name": "qgis__layer_info", "arguments": "{\\"path\\": \\"a.shp\\"}"}',
])
def test_text_tool_calls_variants(content: str):
    """Модели без нативного tool calling печатают вызов текстом в разных формах."""
    calls = parse_text_tool_calls(content, KNOWN)
    assert len(calls) == 1, content
    assert calls[0].tool == "qgis" and calls[0].args.get("path") == "a.shp"


def test_unknown_function_is_ignored():
    calls = parse_text_tool_calls('{"name": "hack__rm", "arguments": {}}', KNOWN)
    assert calls == []


def test_plain_prose_gives_no_calls():
    calls = parse_text_tool_calls("Я не могу выполнить это действие.", KNOWN)
    assert calls == []


def test_native_calls_take_priority_over_text():
    message = {
        "tool_calls": [_Call(_Fn("echo__say", {"text": "из поля"}))],
        "content": '{"name": "qgis__layer_info", "arguments": {"path": "из текста"}}',
    }
    calls = extract_tool_calls(message, KNOWN)
    assert len(calls) == 1 and calls[0].tool == "echo"


def test_base_tool_interface_is_enforced():
    """Класс без обязательных методов нельзя инстанцировать."""
    class Incomplete(BaseTool):
        name = "incomplete"

    with pytest.raises(TypeError):
        Incomplete()  # type: ignore[abstract]
