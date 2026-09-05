"""
Тест: websearch.get_page_text должен уметь извлекать текст из PDF-ссылок,
а не отклонять их с ошибкой "Страница не HTML". Регрессионный тест для
дважды воспроизведённого в бою бага (task 5d3928f13036, task 87eef3701975):
модель находит международную хроностратиграфическую шкалу по ссылке,
но ссылка ведёт на PDF, и раньше действие сразу возвращало ok=False.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools.websearch.websearch_tool import WebSearchTool  # noqa: E402
from core.schemas import ToolCallRequest  # noqa: E402


@pytest.fixture()
def websearch() -> WebSearchTool:
    return WebSearchTool(config={})


def _make_pdf_bytes(pages_text: list[str]) -> bytes:
    from io import BytesIO
    from pypdf import PdfWriter
    try:
        from reportlab.pdfgen import canvas
        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=(300, 300))
        for text in pages_text:
            c.drawString(20, 150, text)
            c.showPage()
        c.save()
        return buf.getvalue()
    except ImportError:
        # reportlab может быть не установлен — тогда просто делаем пустые
        # страницы через pypdf; текстовую ветку проверим отдельным тестом
        # на "страниц без извлекаемого текста", а не на конкретный текст.
        writer = PdfWriter()
        for _ in pages_text:
            writer.add_blank_page(width=200, height=200)
        buf = BytesIO()
        writer.write(buf)
        return buf.getvalue()


def test_get_page_text_extracts_pdf_instead_of_rejecting(websearch: WebSearchTool):
    pdf_bytes = _make_pdf_bytes(["International Chronostratigraphic Chart K2gm"])

    fake_resp = MagicMock()
    fake_resp.headers = {"content-type": "application/pdf"}
    fake_resp.url = "https://example.org/ics_chart.pdf"
    fake_resp.content = pdf_bytes
    fake_resp.raise_for_status.return_value = None

    fake_client = MagicMock()
    fake_client.get.return_value = fake_resp
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    with patch("tools.websearch.websearch_tool.httpx.Client", return_value=fake_client):
        result = websearch.run(ToolCallRequest(
            tool="websearch", action="get_page_text",
            args={"url": "https://example.org/ics_chart.pdf"}))

    assert result.ok is True, f"PDF должен читаться, а не отклоняться: {result.error}"
    assert result.data.get("source_type") == "pdf"
    assert len(result.data.get("text", "")) > 0


def test_get_page_text_still_rejects_real_binary(websearch: WebSearchTool):
    """Не-HTML и не-PDF (например картинка) должны по-прежнему отклоняться понятной ошибкой."""
    fake_resp = MagicMock()
    fake_resp.headers = {"content-type": "image/png"}
    fake_resp.url = "https://example.org/pic.png"
    fake_resp.content = b"\x89PNG\r\n"
    fake_resp.raise_for_status.return_value = None

    fake_client = MagicMock()
    fake_client.get.return_value = fake_resp
    fake_client.__enter__.return_value = fake_client
    fake_client.__exit__.return_value = False

    with patch("tools.websearch.websearch_tool.httpx.Client", return_value=fake_client):
        result = websearch.run(ToolCallRequest(
            tool="websearch", action="get_page_text",
            args={"url": "https://example.org/pic.png"}))

    assert result.ok is False
    assert "HTML" in result.error and "PDF" in result.error
