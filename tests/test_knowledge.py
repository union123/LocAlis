# -*- coding: utf-8 -*-
"""Мок-тесты knowledge: retag, MIN_CONTEXT_SCORE, массовое добавление, delete."""
import sys
sys.path.insert(0, r"C:/Users/mgosh/localis-public")
sys.path.insert(1, r"C:/Users/mgosh/localis-public/.venv/Lib/site-packages")
import tempfile
from pathlib import Path
from core.knowledge import KnowledgeBase


def _fake_embed(texts):
    import hashlib
    out = []
    for t in texts:
        first = (t.split() or ["x"])[0]
        h = int(hashlib.md5(first.encode()).hexdigest(), 16)
        v = [(h >> i) & 1 for i in range(64)]
        out.append([float(b) for b in v] + [0.0] * 960)
    return out


def test_retag_and_mass_and_delete():
    tmp = Path(tempfile.mkdtemp())
    kb = KnowledgeBase(tmp / "kb.sqlite3", tmp / "vec.npy",
                       embed_backend="ollama-bge")
    kb._embed = _fake_embed

    r = kb.add_document("t://a", "Золото на месторождении Голубинка", tags="старое")
    assert r["ok"]
    doc_id = r["doc_id"]

    assert kb.retag(doc_id, "новое,золото")["ok"]
    docs = kb.list_documents()
    assert docs[0]["tags"] == "новое,золото"
    assert not kb.retag("неттакого", "x")["ok"]
    print("retag OK")

    for i in range(20):
        kb.add_document(f"t://bulk{i}", f"Документ {i} про породу {i}",
                        title=f"bulk{i}")
    s = kb.stats()
    assert s["documents"] == 21, s
    print("mass add OK:", s)

    kb.delete_document(doc_id)
    assert kb.stats()["documents"] == 20
    print("delete OK")

    hits = kb.search("порода 5", top_k=3)
    assert isinstance(hits, list)
    print("search after delete OK:", len(hits))
    kb.close()


def test_min_context_score_constant():
    from core.knowledge import MIN_CONTEXT_SCORE
    assert 0 < MIN_CONTEXT_SCORE < 1
