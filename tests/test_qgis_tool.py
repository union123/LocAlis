"""
Тесты QGIS-плагина.

Разделены на две группы:
  - чистые (нормализация аргументов, разбор ответа) — работают везде;
  - интеграционные — пропускаются, если QGIS на машине не найден.
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
from core.tool_registry import ToolRegistry  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "qgis_tool_under_test", ROOT / "tools" / "qgis" / "qgis_tool.py")
qgis_module = importlib.util.module_from_spec(_spec)
sys.modules["qgis_tool_under_test"] = qgis_module
_spec.loader.exec_module(qgis_module)
QgisTool = qgis_module.QgisTool

SAMPLE_SHP = Path(r"C:\path\to\sample.shp")


@pytest.fixture()
def tool() -> "QgisTool":
    return QgisTool()


# ---- чистые тесты --------------------------------------------------------

def test_actions_have_valid_json_schema(tool):
    names = {a.name for a in tool.actions()}
    assert names == {"list_layers", "layer_info", "read_attributes",
                     "export_geojson", "update_attributes",
                     # добавлены 05.08: универсальный доступ к алгоритмам QGIS
                     "run_processing", "list_algorithms"}
    for action in tool.actions():
        assert action.parameters["type"] == "object"
        assert "properties" in action.parameters


def test_url_encoded_path_is_repaired(tool):
    """Регрессия: qwen2.5:3b возвращает путь с %20 и без буквы диска."""
    fixed = tool._fix_path(r"\path\to\sample%20test.shp")
    assert "%20" not in fixed
    assert fixed.endswith(r"POLIGON TEST 2.shp")
    if SAMPLE_SHP.exists():
        assert fixed.lower().startswith("c:")


@pytest.mark.parametrize("raw,expected_suffix", [
    ('"C:\\data\\layer.shp"', "layer.shp"),
    ("file:///C:/data/layer.shp", "layer.shp"),
    ("C:/data/layer.shp", "layer.shp"),
])
def test_path_variants(tool, raw, expected_suffix):
    assert tool._fix_path(raw).endswith(expected_suffix)


def test_layer_name_dropped_for_shapefile(tool):
    """Лишний layer_name от модели ломает открытие shapefile — он убирается."""
    args = tool._normalize_args({"path": r"C:\d\a.shp", "layer_name": "a"})
    assert "layer_name" not in args


def test_layer_name_kept_for_geopackage(tool):
    args = tool._normalize_args({"path": r"C:\d\a.gpkg", "layer_name": "wells"})
    assert args["layer_name"] == "wells"


def test_string_limit_is_coerced(tool):
    assert tool._normalize_args({"path": "a.shp", "limit": "10"})["limit"] == 10


def test_extract_result_ignores_qgis_noise(tool):
    marker = qgis_module.MARKER
    stdout = f'Warning: QGIS noise\n{marker}{{"ok": true, "data": {{}}, "summary": "s"}}{marker}\ntail'
    assert tool._extract_result(stdout)["ok"] is True
    assert tool._extract_result("только шум без маркера") is None


def test_command_quotes_paths_with_spaces(tool):
    """Регрессия: путь 'C:\\Program Files\\QGIS ...' разваливался без кавычек."""
    if tool.launcher is None:
        pytest.skip("QGIS не установлен")
    command, use_shell = tool._build_command()
    if sys.platform == "win32":
        assert use_shell is True and isinstance(command, str)
        assert command.count('"') == 4


# ---- интеграционные тесты ------------------------------------------------

requires_qgis = pytest.mark.skipif(
    QgisTool().launcher is None, reason="QGIS не найден на этой машине")
requires_sample = pytest.mark.skipif(
    not SAMPLE_SHP.exists(), reason="Нет тестового шейпфайла")


@requires_qgis
def test_health_reports_ready(tool):
    status, message = tool.health()
    assert status is ToolStatus.READY and "QGIS" in message


@requires_qgis
@requires_sample
def test_layer_info_real_file():
    reg = ToolRegistry()
    reg.discover()
    res = reg.call(ToolCallRequest(tool="qgis", action="layer_info",
                                   args={"path": str(SAMPLE_SHP)}))
    assert res.ok, res.error
    assert res.data["crs"].startswith("EPSG:")
    assert res.data["feature_count"] >= 1


@requires_qgis
@requires_sample
def test_missing_file_returns_clear_error():
    reg = ToolRegistry()
    reg.discover()
    res = reg.call(ToolCallRequest(tool="qgis", action="layer_info",
                                   args={"path": r"C:\nope\missing.shp"}))
    assert res.ok is False and "не найден" in res.error


@requires_qgis
@requires_sample
def test_export_geojson_with_reprojection(tmp_path):
    reg = ToolRegistry()
    reg.discover()
    out = tmp_path / "out.geojson"
    res = reg.call(ToolCallRequest(tool="qgis", action="export_geojson", args={
        "path": str(SAMPLE_SHP), "output_path": str(out), "target_crs": "EPSG:4326"}))
    assert res.ok, res.error
    assert out.exists() and out.stat().st_size > 0
    assert "FeatureCollection" in out.read_text(encoding="utf-8")
