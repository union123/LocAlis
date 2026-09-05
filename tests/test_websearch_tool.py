"""
Тесты инструмента websearch.

Разделены на две группы:
  - чистые (схема действий, эвристика извлечения текста, валидация аргументов) —
    работают всегда, без сети;
  - интеграционные (реальный поиск в интернете) — пропускаются, если пакет
    ddgs не установлен или недоступна сеть.
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
    "websearch_tool_under_test", ROOT / "tools" / "websearch" / "websearch_tool.py")
websearch_module = importlib.util.module_from_spec(_spec)
sys.modules["websearch_tool_under_test"] = websearch_module
_spec.loader.exec_module(websearch_module)
WebSearchTool = websearch_module.WebSearchTool


@pytest.fixture()
def tool() -> "WebSearchTool":
    return WebSearchTool(config={
        "default_max_results": 6, "max_max_results": 20,
        "fetch_timeout_sec": 15, "default_max_chars": 8000, "max_max_chars": 40000,
    })


# ---- чистые тесты ----------------------------------------------------------

def test_actions_have_valid_json_schema(tool):
    names = {a.name for a in tool.actions()}
    assert names == {"search_web", "get_page_text"}
    for action in tool.actions():
        assert action.parameters["type"] == "object"
        assert "properties" in action.parameters


def test_search_web_rejects_empty_query(tool):
    res = tool.execute("search_web", {"query": ""})
    assert res["ok"] is False
    assert "запрос" in res["error"].lower() or "query" in res["error"].lower()


def test_get_page_text_rejects_empty_url(tool):
    res = tool.execute("get_page_text", {"url": ""})
    assert res["ok"] is False


def test_unknown_action_returns_error(tool):
    res = tool.execute("bogus_action", {})
    assert res["ok"] is False and "bogus_action" in res["error"]


def test_extract_main_text_skips_nav_and_footer():
    html_str = """
    <html><head><title>Test Article</title></head>
    <body>
      <nav><a href="/">Home</a><a href="/about">About</a><a href="/x">X</a></nav>
      <header><a href="/1">menu1</a><a href="/2">menu2</a></header>
      <article>
        <p>Это основной текст статьи про геологию Крыма и стратиграфию.
        Он достаточно длинный, чтобы алгоритм посчитал его главным блоком
        страницы, а не навигацией или подвалом сайта. Повторим ещё раз для
        длины: геология, стратиграфия, картирование, разрезы, полевые работы.</p>
        <p>Второй абзац продолжает основную мысль без единой ссылки внутри.</p>
      </article>
      <footer><a href="/priv">Privacy</a><a href="/tos">Terms</a><a href="/c">Contact</a></footer>
      <script>var x = 1;</script>
    </body></html>
    """
    title, text = websearch_module._extract_main_text(html_str, base_url="https://example.com")
    assert title == "Test Article"
    assert "геологию Крыма" in text
    assert "Home" not in text
    assert "Privacy" not in text


def test_link_density_pure_links_is_high():
    from lxml import html as lxml_html
    el = lxml_html.fromstring("<div><a href='#'>link one</a><a href='#'>link two</a></div>")
    density = websearch_module._link_density(el)
    assert density > 0.9


def test_link_density_pure_text_is_zero():
    from lxml import html as lxml_html
    el = lxml_html.fromstring("<div>just plain text, no links at all here</div>")
    density = websearch_module._link_density(el)
    assert density == 0.0


# ---- интеграционные тесты --------------------------------------------------

requires_ddgs = pytest.mark.skipif(
    websearch_module.DDGS is None, reason="ddgs не установлен")


@requires_ddgs
def test_health_reports_ready(tool):
    status, _ = tool.health()
    assert status is ToolStatus.READY


@requires_ddgs
def test_search_web_real_query():
    reg = ToolRegistry()
    reg.discover()
    res = reg.call(ToolCallRequest(tool="websearch", action="search_web",
                                   args={"query": "QGIS geology plugin", "max_results": 3}))
    assert res.ok, res.error
    assert len(res.data["results"]) > 0
    assert res.data["results"][0]["url"].startswith("http")


@requires_ddgs
def test_get_page_text_real_page():
    reg = ToolRegistry()
    reg.discover()
    res = reg.call(ToolCallRequest(tool="websearch", action="get_page_text",
                                   args={"url": "https://example.com", "max_chars": 2000}))
    assert res.ok, res.error
    assert len(res.data["text"]) > 0
