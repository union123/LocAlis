"""
Тесты инструментов и решений, добавленных 05.08:
charts, чтение PDF, кеш вызовов, проверка шагов, роль планировщика.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.schemas import PlanStep, StepResult, ToolCallRequest, ToolCallResult  # noqa: E402
from core.step_check import check_step  # noqa: E402
from tools.charts.charts_tool import ChartsTool  # noqa: E402


# ---- charts ---------------------------------------------------------------


@pytest.fixture()
def charts() -> ChartsTool:
    return ChartsTool(config={"width_inch": 6, "height_inch": 4, "dpi": 90,
                              "max_points": 50, "allowed_roots": []})


@pytest.mark.parametrize("kind", ["bar", "barh", "line", "pie", "scatter"])
def test_charts_all_kinds_create_png(charts: ChartsTool, tmp_path: Path, kind: str):
    result = charts.run(ToolCallRequest(tool="charts", action="plot", args={
        "output_path": str(tmp_path / f"{kind}.png"), "kind": kind,
        "labels": ["а", "б", "в"], "values": [3, 5, 2], "title": "Проверка"}))
    assert result.ok, result.error
    created = Path(result.data["path"])
    assert created.exists() and created.stat().st_size > 1000


def test_charts_accepts_numbers_as_strings(charts: ChartsTool, tmp_path: Path):
    """Модели присылают числа строками — падать из-за этого нельзя."""
    result = charts.run(ToolCallRequest(tool="charts", action="plot", args={
        "output_path": str(tmp_path / "s.png"), "kind": "bar",
        "labels": ["a", "b"], "values": ["4", 7]}))
    assert result.ok and result.data["points"] == 2


def test_charts_aligns_mismatched_lengths(charts: ChartsTool, tmp_path: Path):
    """5 подписей и 3 значения — обычная ошибка модели, выравниваем."""
    result = charts.run(ToolCallRequest(tool="charts", action="plot", args={
        "output_path": str(tmp_path / "m.png"), "kind": "bar",
        "labels": ["a", "b", "c", "d", "e"], "values": [1, 2, 3]}))
    assert result.ok and result.data["points"] == 3


def test_charts_multiple_series(charts: ChartsTool, tmp_path: Path):
    result = charts.run(ToolCallRequest(tool="charts", action="plot", args={
        "output_path": str(tmp_path / "multi.png"), "kind": "line",
        "labels": ["янв", "фев"], "series": {"план": [1, 2], "факт": [2, 1]}}))
    assert result.ok and len(result.data["series"]) == 2


def test_charts_rejects_pie_with_many_series(charts: ChartsTool, tmp_path: Path):
    result = charts.run(ToolCallRequest(tool="charts", action="plot", args={
        "output_path": str(tmp_path / "p.png"), "kind": "pie",
        "labels": ["a", "b"], "series": {"один": [1, 2], "два": [3, 4]}}))
    assert not result.ok and "одной серии" in result.error


def test_charts_rejects_empty_data(charts: ChartsTool, tmp_path: Path):
    result = charts.run(ToolCallRequest(tool="charts", action="plot", args={
        "output_path": str(tmp_path / "e.png"), "kind": "bar", "values": []}))
    assert not result.ok and "числовых данных" in result.error


def test_charts_protects_existing_file(charts: ChartsTool, tmp_path: Path):
    target = tmp_path / "exists.png"
    target.write_bytes(b"x")
    result = charts.run(ToolCallRequest(tool="charts", action="plot", args={
        "output_path": str(target), "kind": "bar", "values": [1]}))
    assert not result.ok and "уже существует" in result.error


def test_charts_forces_png_extension(charts: ChartsTool, tmp_path: Path):
    result = charts.run(ToolCallRequest(tool="charts", action="plot", args={
        "output_path": str(tmp_path / "no_ext"), "kind": "bar", "values": [1, 2]}))
    assert result.ok and result.data["path"].endswith(".png")


# ---- проверка шагов -------------------------------------------------------


def test_step_check_flags_unused_tools():
    """Главный случай: инструменты выданы, но модель ни один не вызвала."""
    step = PlanStep(step_id="s1", instruction="прочитай файл", tools=["files"])
    result = StepResult(step_id="s1", ok=True, answer="В файле 100 строк")
    checked = check_step(step, result)
    assert checked.suspicious
    assert any("не вызван" in w for w in checked.warnings)


def test_step_check_passes_honest_step():
    step = PlanStep(step_id="s1", instruction="прочитай", tools=["files"])
    result = StepResult(step_id="s1", ok=True, answer="В файле 100 строк",
                        tool_results=[ToolCallResult(call_id="c", tool="files",
                                                     action="read_text", ok=True)])
    assert not check_step(step, result).suspicious


def test_step_check_flags_all_failed_tools():
    step = PlanStep(step_id="s1", instruction="прочитай", tools=["files"])
    result = StepResult(step_id="s1", ok=True, answer="Файл содержит данные",
                        tool_results=[ToolCallResult(call_id="c", tool="files",
                                                     action="read_text", ok=False,
                                                     error="путь не найден")])
    checked = check_step(step, result)
    assert checked.suspicious and "ошибкой" in " ".join(checked.warnings)


def test_step_check_flags_refusal():
    step = PlanStep(step_id="s1", instruction="прочитай")
    result = StepResult(step_id="s1", ok=True,
                        answer="У меня нет доступа к файловой системе, пришлите файл")
    assert check_step(step, result).suspicious


def test_step_check_ignores_failed_step():
    """У упавшего шага и так есть ошибка — предупреждения не нужны."""
    step = PlanStep(step_id="s1", instruction="x", tools=["files"])
    result = StepResult(step_id="s1", ok=False, error="модель упала")
    assert not check_step(step, result).warnings


# ---- кеш вызовов инструментов ---------------------------------------------


class _CountingTool:
    """Инструмент-счётчик: показывает, был ли реальный вызов."""

    def __init__(self):
        self.calls = 0

    def run(self, request):
        self.calls += 1
        return ToolCallResult(call_id=request.call_id, tool=request.tool,
                              action=request.action, ok=True,
                              summary=f"вызов номер {self.calls}")


@pytest.fixture()
def registry_with_counter():
    from core.tool_registry import ToolEntry, ToolRegistry
    from core.schemas import ToolManifest, ToolStatus

    registry = ToolRegistry()
    tool = _CountingTool()
    # name у ToolEntry — вычисляемое свойство из манифеста, не поле
    entry = ToolEntry(
        path=Path("."),
        manifest=ToolManifest(name="demo", version="1.0.0", description="",
                              entrypoint="x.py", class_name="X"),
        instance=tool, status=ToolStatus.READY, message="ok")
    registry.entries["demo"] = entry
    return registry, tool


def test_cache_serves_repeated_reads(registry_with_counter):
    """Повторное чтение с теми же аргументами не должно доходить до инструмента."""
    registry, tool = registry_with_counter
    args = {"path": "a.txt"}
    first = registry.call(ToolCallRequest(tool="demo", action="read_text", args=args))
    second = registry.call(ToolCallRequest(tool="demo", action="read_text", args=args))
    assert tool.calls == 1
    assert first.from_cache is False and second.from_cache is True
    assert second.summary == first.summary


def test_cache_distinguishes_arguments(registry_with_counter):
    registry, tool = registry_with_counter
    registry.call(ToolCallRequest(tool="demo", action="read_text", args={"path": "a"}))
    registry.call(ToolCallRequest(tool="demo", action="read_text", args={"path": "b"}))
    assert tool.calls == 2


def test_cache_never_caches_writes(registry_with_counter):
    """Запись должна выполняться каждый раз, иначе файл молча не изменится."""
    registry, tool = registry_with_counter
    args = {"output_path": "a.txt", "content": "x"}
    registry.call(ToolCallRequest(tool="demo", action="write_text", args=args))
    registry.call(ToolCallRequest(tool="demo", action="write_text", args=args))
    assert tool.calls == 2


def test_clear_cache_forces_new_call(registry_with_counter):
    """Между задачами файлы могли измениться — кеш обязан сбрасываться."""
    registry, tool = registry_with_counter
    args = {"path": "a.txt"}
    registry.call(ToolCallRequest(tool="demo", action="read_text", args=args))
    registry.clear_cache()
    registry.call(ToolCallRequest(tool="demo", action="read_text", args=args))
    assert tool.calls == 2


def test_cache_keeps_distinct_call_ids(registry_with_counter):
    """Иначе журнал вызовов склеит два обращения в одно."""
    registry, _ = registry_with_counter
    args = {"path": "a.txt"}
    first = registry.call(ToolCallRequest(tool="demo", action="read_text", args=args))
    second = registry.call(ToolCallRequest(tool="demo", action="read_text", args=args))
    assert first.call_id != second.call_id


# ---- роль планировщика ----------------------------------------------------


def test_orchestrator_role_is_separate_from_judge():
    """
    Планировщику важна скорость, арбитру — качество. Раньше они делили
    одну модель, и ускорить планирование было нельзя.
    """
    from core.llm_gateway import LLMGateway
    from config.resilience import ModeController

    config = {
        "orchestrator": {"local": {"provider": "ollama", "model": "planner-model"}},
        "judge": {"local_fallback": {"provider": "ollama", "model": "judge-model"}},
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
    }
    gateway = LLMGateway(config, ModeController.from_config({}, config))
    assert gateway.orchestrator_spec().model == "planner-model"
    assert gateway.judge_specs()[1].model == "judge-model"


def test_orchestrator_role_falls_back_to_judge_when_absent():
    """Старые конфиги без роли orchestrator должны продолжать работать."""
    from core.llm_gateway import LLMGateway
    from config.resilience import ModeController

    config = {
        "judge": {"local_fallback": {"provider": "ollama", "model": "judge-model"}},
        "providers": {"ollama": {"base_url": "http://127.0.0.1:11434"}},
    }
    gateway = LLMGateway(config, ModeController.from_config({}, config))
    assert gateway.orchestrator_spec().model == "judge-model"


# ---- чтение PDF -----------------------------------------------------------


@pytest.fixture()
def files_tool():
    from tools.files.files_tool import FilesTool
    return FilesTool(config={"max_chars": 5000, "max_entries": 50,
                             "blocked_extensions": [".exe"], "allowed_roots": []})


def test_pdf_reading_reports_scanned_pages(files_tool, tmp_path: Path):
    """
    Страницы без текста (сканы) должны отмечаться честно.
    Иначе модель решит, что документ пустой, и выдумает содержание.
    """
    from pypdf import PdfWriter

    target = tmp_path / "scan.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.add_blank_page(width=200, height=200)
    with open(target, "wb") as handle:
        writer.write(handle)

    result = files_tool.run(ToolCallRequest(tool="files", action="read_text",
                                            args={"path": str(target)}))
    assert result.ok
    assert "без извлекаемого текста" in result.summary
    assert "2" in result.summary


def test_pdf_is_listed_as_readable(files_tool, tmp_path: Path):
    from pypdf import PdfWriter

    target = tmp_path / "doc.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with open(target, "wb") as handle:
        writer.write(handle)

    info = files_tool.run(ToolCallRequest(tool="files", action="file_info",
                                          args={"path": str(target)}))
    assert info.ok and info.data["readable_as_text"] is True


def test_router_sends_pdf_to_files():
    """Без этого правила запрос про .pdf шёл бы без инструментов."""
    from config.loader import load_routing_rules
    from core.router import Router
    from core.schemas import Task

    decision = Router(load_routing_rules()).route(
        Task(prompt="прочитай конспект.pdf и сделай выжимку"))
    assert decision.needs_tools is True
    assert "files" in decision.suggested_tools


def test_router_sends_charts_request_to_charts():
    from config.loader import load_routing_rules
    from core.router import Router
    from core.schemas import Task

    decision = Router(load_routing_rules()).route(
        Task(prompt="построй столбчатую диаграмму по типам файлов"))
    assert "charts" in decision.suggested_tools


def test_router_sends_geoprocessing_to_qgis():
    from config.loader import load_routing_rules
    from core.router import Router
    from core.schemas import Task

    decision = Router(load_routing_rules()).route(
        Task(prompt="сделай буфер 100 метров вокруг объектов слоя"))
    assert "qgis" in decision.suggested_tools
