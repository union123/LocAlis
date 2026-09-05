"""
Тесты инструмента vision.

Разделены на две группы:
  - чистые (схема действий, разбор источника изображения, эвристика имени
    модели) — работают всегда, без Ollama;
  - интеграционные (реальный вызов Ollama) — пропускаются, если Ollama
    недоступен или не готов (см. health()).
"""

from __future__ import annotations

import base64
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
    "vision_tool_under_test", ROOT / "tools" / "vision" / "vision_tool.py")
vision_module = importlib.util.module_from_spec(_spec)
sys.modules["vision_tool_under_test"] = vision_module
_spec.loader.exec_module(vision_module)
VisionTool = vision_module.VisionTool


@pytest.fixture()
def tool() -> "VisionTool":
    return VisionTool(config={
        "ollama_url": "http://localhost:11434",
        "model": "qwen2.5vl:7b",
        "request_timeout_sec": 180,
        "vision_name_hints": ["vl", "vision", "llava", "moondream", "bakllava", "minicpm-v"],
    })


# ---- чистые тесты ----------------------------------------------------------

def test_actions_have_valid_json_schema(tool):
    names = {a.name for a in tool.actions()}
    assert names == {"list_vision_models", "ask_about_image"}
    for action in tool.actions():
        assert action.parameters["type"] == "object"
        assert "properties" in action.parameters


def test_unknown_action_returns_error(tool):
    res = tool.execute("bogus_action", {})
    assert res["ok"] is False and "bogus_action" in res["error"]


def test_no_image_source_is_rejected(tool):
    b64, err = tool._resolve_image_base64({})
    assert b64 is None
    assert "path" in err or "url" in err or "image_base64" in err


def test_two_image_sources_is_rejected(tool):
    b64, err = tool._resolve_image_base64({"path": "a.png", "url": "http://x/y.png"})
    assert b64 is None
    assert "ОДИН" in err or "один" in err.lower()


def test_missing_file_path_gives_clear_error(tool):
    b64, err = tool._resolve_image_base64({"path": "/no/such/file.png"})
    assert b64 is None
    assert "не найден" in err


def test_image_base64_passthrough(tool):
    b64, err = tool._resolve_image_base64({"image_base64": "QUJD"})
    assert err is None
    assert b64 == "QUJD"


def test_path_source_reads_and_encodes_file(tool, tmp_path):
    raw = b"\x89PNG-fake-bytes-for-test"
    f = tmp_path / "img.png"
    f.write_bytes(raw)
    b64, err = tool._resolve_image_base64({"path": str(f)})
    assert err is None
    assert base64.b64decode(b64) == raw


@pytest.mark.parametrize("name,expected", [
    ("qwen2.5vl:7b", True),
    ("moondream:latest", True),
    ("llava:13b", True),
    ("llama3.1:8b", False),
    ("bge-m3:latest", False),
])
def test_looks_like_vision_model(tool, name, expected):
    assert tool._looks_like_vision_model(name) is expected


def test_ask_about_image_requires_a_source(tool):
    res = tool.execute("ask_about_image", {})
    assert res["ok"] is False


# ---- интеграционные тесты --------------------------------------------------

def _ollama_ready() -> bool:
    t = VisionTool(config={"ollama_url": "http://localhost:11434", "model": "qwen2.5vl:7b"})
    status, _ = t.health()
    return status is ToolStatus.READY


requires_ollama_with_vision_model = pytest.mark.skipif(
    not _ollama_ready(), reason="Ollama недоступен или нет мультимодальной модели")


@requires_ollama_with_vision_model
def test_ask_about_image_real_call(tmp_path):
    # 1x1 px белый PNG, валиден для отправки в Ollama.
    png_1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")
    f = tmp_path / "pixel.png"
    f.write_bytes(png_1x1)

    reg = ToolRegistry()
    reg.discover()
    res = reg.call(ToolCallRequest(tool="vision", action="ask_about_image",
                                   args={"path": str(f), "question": "Что на этой картинке?"}))
    assert res.ok, res.error
    assert len(res.data["answer"]) > 0


def test_list_vision_models_action_runs_without_crashing(tool):
    # Не требует готовой модели — только доступного Ollama; если Ollama
    # тоже недоступен, действие должно вернуть понятную ошибку, а не упасть.
    res = tool.execute("list_vision_models", {})
    assert "ok" in res
