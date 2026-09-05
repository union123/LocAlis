"""Тесты Retrieval Agent: нарезка, индексация, поиск. Эмбеддинги — заглушка."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.retrieval import RetrievalAgent, VectorIndex  # noqa: E402


class FakeEmbedGateway:
    """Простые «эмбеддинги»: вектор из частот символов — детерминированно."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            vector = [0.0] * 8
            for ch in text.lower():
                vector[ord(ch) % 8] += 1.0
            vectors.append(vector)
        return vectors


@pytest.fixture()
def agent(tmp_path: Path) -> RetrievalAgent:
    return RetrievalAgent(FakeEmbedGateway(), tmp_path / "index", dim=8)


def test_chunking_with_overlap():
    text = "абвгде" * 400
    chunks = RetrievalAgent.chunk(text, size=1000, overlap=100)
    assert len(chunks) > 1
    assert all(len(c) <= 1000 for c in chunks)


def test_empty_text_gives_no_chunks():
    assert RetrievalAgent.chunk("   ") == []


def test_index_and_search(agent: RetrievalAgent):
    added = agent.index_text("Габбро — основная интрузивная порода", {"name": "порода.txt"})
    assert added == 1
    hits = agent.search("габбро порода", top_k=3)
    assert hits and hits[0]["metadata"]["name"] == "порода.txt"
    assert 0.0 <= hits[0]["score"] <= 1.001


def test_search_on_empty_index_returns_empty(agent: RetrievalAgent):
    assert agent.search("что угодно") == []
    assert agent.context_block("что угодно") == ""


def test_context_block_is_bounded(agent: RetrievalAgent):
    for i in range(5):
        agent.index_text("описание " * 200, {"name": f"файл{i}.txt"})
    block = agent.context_block("описание", top_k=5, max_chars=1200)
    assert block.startswith("НАЙДЕНО В ЛОКАЛЬНОЙ БАЗЕ ЗНАНИЙ")
    assert len(block) <= 1500


def test_index_persists_between_sessions(tmp_path: Path):
    first = RetrievalAgent(FakeEmbedGateway(), tmp_path / "idx", dim=8)
    first.index_text("уникальный текст про кварц", {"name": "кварц.txt"})
    reopened = RetrievalAgent(FakeEmbedGateway(), tmp_path / "idx", dim=8)
    assert len(reopened.index) == 1
    assert reopened.search("кварц")[0]["metadata"]["name"] == "кварц.txt"


def test_index_file(tmp_path: Path):
    source = tmp_path / "отчёт.txt"
    source.write_text("Содержимое отчёта о скважинах", encoding="utf-8")
    agent = RetrievalAgent(FakeEmbedGateway(), tmp_path / "idx2", dim=8)
    assert agent.index_file(source) == 1
    assert agent.search("скважины")[0]["metadata"]["name"] == "отчёт.txt"


def test_vector_index_len(tmp_path: Path):
    index = VectorIndex(tmp_path / "vi", dim=4)
    assert len(index) == 0
    index.add(["a", "b"], [[1, 0, 0, 0], [0, 1, 0, 0]])
    assert len(index) == 2
