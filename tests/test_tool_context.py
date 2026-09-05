"""
Тесты контекста инструментов: внедрение возможностей реестром,
обратная совместимость со старыми плагинами, защита от рекурсии агентов.
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
from core.tool_context import (  # noqa: E402
    ALL_CAPABILITIES,
    ASK_LOCAL_MODEL,
    RECALL,
    REMEMBER,
    RUN_LOCAL_AGENT,
    ScratchMemory,
    build_context,
)
from core.tool_registry import ToolRegistry  # noqa: E402

# Плагин нового вида: принимает context
CONTEXT_PLUGIN = '''
from core.base_tool import BaseTool, ToolAction
from core.schemas import ToolStatus

class CtxTool(BaseTool):
    name = "ctx"
    version = "1.0.0"
    requires_context = True

    def health(self):
        if self.capability("ask_local_model") is None:
            return ToolStatus.UNAVAILABLE, "нет контекста"
        return ToolStatus.READY, "контекст есть"

    def actions(self):
        return [ToolAction("ping", "проверка", {"type": "object", "properties": {}})]

    def execute(self, action, args):
        answer = self.require("ask_local_model")("m1", "привет")
        return {"ok": True, "data": answer, "summary": str(answer.get("text"))}
'''

# Плагин старого вида: __init__ знает только config (обратная совместимость)
LEGACY_PLUGIN = '''
from core.base_tool import BaseTool, ToolAction

class LegacyTool(BaseTool):
    name = "legacy"
    version = "0.9.0"

    def __init__(self, config=None):
        super().__init__(config)

    def actions(self):
        return [ToolAction("noop", "ничего", {"type": "object", "properties": {}})]

    def execute(self, action, args):
        return {"ok": True, "data": {}, "summary": "ок"}
'''


@pytest.fixture()
def tools_dir(tmp_path: Path) -> Path:
    for name, code in (("ctx", CONTEXT_PLUGIN), ("legacy", LEGACY_PLUGIN)):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "manifest.yaml").write_text(
            f"name: {name}\nenabled: true\nentrypoint: tool.py\n", encoding="utf-8")
        (folder / "tool.py").write_text(code, encoding="utf-8")
    return tmp_path


def fake_context() -> dict:
    return build_context(
        ask_local_model=lambda mid, prompt, system="", temperature=None: {
            "ok": True, "text": f"ответ от {mid}"},
        list_local_models=lambda: [{"id": "m1", "model": "модель-1"}],
        memory=ScratchMemory(),
    )


# ---- рабочая память ------------------------------------------------------


def test_scratch_memory_basic():
    memory = ScratchMemory()
    memory.remember("a", "значение")
    assert memory.recall("a") == "значение"
    assert memory.recall() == {"a": "значение"}
    assert len(memory) == 1
    memory.clear()
    assert len(memory) == 0


def test_scratch_memory_missing_key():
    assert ScratchMemory().recall("нет") is None


def test_memory_overwrite():
    memory = ScratchMemory()
    memory.remember("k", "первое")
    memory.remember("k", "второе")
    assert memory.recall("k") == "второе"


# ---- сборка контекста ----------------------------------------------------


def test_build_context_always_has_memory():
    context = build_context()
    assert REMEMBER in context and RECALL in context


def test_build_context_skips_missing_capabilities():
    """Отсутствующая возможность не должна попадать в контекст как None."""
    context = build_context(ask_local_model=None)
    assert ASK_LOCAL_MODEL not in context


def test_capability_keys_are_unique():
    assert len(ALL_CAPABILITIES) == len(set(ALL_CAPABILITIES))


def test_capability_returns_none_when_absent():
    class T(BaseTool):
        name = "t"

        def actions(self):
            return []

        def execute(self, action, args):
            return {"ok": True}

    tool = T(context={})
    assert tool.capability(RUN_LOCAL_AGENT) is None
    with pytest.raises(RuntimeError, match="недоступна возможность"):
        tool.require(RUN_LOCAL_AGENT)


# ---- внедрение реестром --------------------------------------------------


def test_registry_injects_context_at_discovery(tools_dir: Path):
    registry = ToolRegistry(tools_dir, context=fake_context())
    registry.discover()
    entry = registry.get("ctx")
    assert entry.status is ToolStatus.READY
    result = registry.call(ToolCallRequest(tool="ctx", action="ping", args={}))
    assert result.ok and result.summary == "ответ от m1"


def test_legacy_plugin_still_loads(tools_dir: Path):
    """Старый плагин без параметра context не должен ломаться."""
    registry = ToolRegistry(tools_dir, context=fake_context())
    registry.discover()
    entry = registry.get("legacy")
    assert entry.status is ToolStatus.READY
    assert entry.instance.context  # контекст выставлен атрибутом
    assert registry.call(ToolCallRequest(tool="legacy", action="noop", args={})).ok


def test_set_context_after_discovery_updates_health(tools_dir: Path):
    """
    Регрессия: реестр создаётся раньше шлюза, поэтому первый health-check
    идёт без контекста. После set_context статус обязан пересчитаться,
    иначе инструмент-агент навсегда остался бы «недоступен».
    """
    registry = ToolRegistry(tools_dir)          # без контекста
    registry.discover()
    assert registry.get("ctx").status is ToolStatus.UNAVAILABLE

    registry.set_context(fake_context())
    assert registry.get("ctx").status is ToolStatus.READY
    assert "ctx" in [e.name for e in registry.available()]


def test_set_context_does_not_enable_disabled_plugin(tmp_path: Path):
    folder = tmp_path / "switched_off"
    folder.mkdir()
    (folder / "manifest.yaml").write_text(
        "name: switched_off\nenabled: false\nentrypoint: tool.py\n", encoding="utf-8")
    (folder / "tool.py").write_text(CONTEXT_PLUGIN, encoding="utf-8")
    registry = ToolRegistry(tmp_path)
    registry.discover()
    registry.set_context(fake_context())
    assert registry.get("switched_off").status is ToolStatus.DISABLED


def test_health_exception_after_context_is_caught(tmp_path: Path):
    folder = tmp_path / "bad"
    folder.mkdir()
    (folder / "manifest.yaml").write_text(
        "name: bad\nenabled: true\nentrypoint: tool.py\n", encoding="utf-8")
    (folder / "tool.py").write_text('''
from core.base_tool import BaseTool, ToolAction

class BadHealth(BaseTool):
    name = "bad"

    def health(self):
        raise RuntimeError("health сломан")

    def actions(self):
        return [ToolAction("x", "y", {"type": "object", "properties": {}})]

    def execute(self, action, args):
        return {"ok": True}
''', encoding="utf-8")
    registry = ToolRegistry(tmp_path)
    registry.discover()
    registry.set_context(fake_context())
    entry = registry.get("bad")
    assert entry.status is ToolStatus.ERROR and "health сломан" in entry.message


def test_yaml_boolean_name_is_coerced(tmp_path: Path):
    """
    YAML-ловушка: 'name: off' читается как булево False, 'version: 1.0' как число.
    Манифест обязан это выдержать, а не падать ошибкой валидации.
    """
    folder = tmp_path / "weird"
    folder.mkdir()
    (folder / "manifest.yaml").write_text(
        "\n".join(["name: off", "version: 1.0", "enabled: true",
                   "entrypoint: tool.py"]) + "\n",
        encoding="utf-8")
    (folder / "tool.py").write_text(LEGACY_PLUGIN, encoding="utf-8")
    registry = ToolRegistry(tmp_path)
    entries = registry.discover()
    entry = list(entries.values())[0]
    assert entry.status is not ToolStatus.ERROR, entry.message
    assert isinstance(entry.manifest.version, str)


def test_stale_core_gives_helpful_message(tmp_path: Path):
    """
    Регрессия из реального использования: приложение было запущено ДО
    обновления ядра и держало в памяти старый BaseTool без capability().
    Пользователь видел сырой AttributeError. Теперь подсказываем перезапуск.
    """
    folder = tmp_path / "stale"
    folder.mkdir()
    (folder / "manifest.yaml").write_text(
        "name: stale\nenabled: true\nentrypoint: tool.py\n", encoding="utf-8")
    (folder / "tool.py").write_text('''
from core.base_tool import BaseTool, ToolAction

class StaleTool(BaseTool):
    name = "stale"

    def health(self):
        # Имитируем старое ядро: метода capability() ещё не существует
        raise AttributeError("'StaleTool' object has no attribute 'capability'")

    def actions(self):
        return [ToolAction("x", "y", {"type": "object", "properties": {}})]

    def execute(self, action, args):
        return {"ok": True}
''', encoding="utf-8")
    registry = ToolRegistry(tmp_path)
    registry.discover()
    entry = registry.get("stale")
    assert entry.status is ToolStatus.ERROR
    assert "Перезапустите приложение" in entry.message
    assert "capability" in entry.message
    # Сырой AttributeError пользователю больше не показывается
    assert "AttributeError" not in entry.message
