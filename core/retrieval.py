"""
Retrieval Agent (слой 4) — универсальный векторный поиск.

Домен-агностичен: индексирует любые тексты с метаданными, ничего не знает
про геологию или QGIS. Эмбеддинги — локальные (bge-m3 через Ollama).

Бэкенд FAISS необязателен: при его отсутствии используется точный поиск на
numpy (медленнее на больших объёмах, но результат тот же) — платформа
не должна падать из-за необязательной зависимости.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

RETRIEVAL_VERSION = "1.0"


class VectorIndex:
    """Простое персистентное хранилище векторов + косинусный поиск."""

    def __init__(self, index_dir: Path | str, dim: int = 1024):
        self.dir = Path(index_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dim = dim
        self.meta_path = self.dir / "documents.jsonl"
        self.vectors_path = self.dir / "vectors.npy"
        self.documents: list[dict[str, Any]] = []
        self._vectors: Any = None
        self._load()

    # ---- персистентность -------------------------------------------------

    def _load(self) -> None:
        if self.meta_path.is_file():
            self.documents = [
                json.loads(line) for line in
                self.meta_path.read_text(encoding="utf-8").splitlines() if line.strip()
            ]
        if self.vectors_path.is_file():
            try:
                import numpy as np
                self._vectors = np.load(self.vectors_path)
            except Exception:  # noqa: BLE001
                self._vectors = None

    def save(self) -> None:
        self.meta_path.write_text(
            "\n".join(json.dumps(d, ensure_ascii=False) for d in self.documents),
            encoding="utf-8")
        if self._vectors is not None:
            import numpy as np
            np.save(self.vectors_path, self._vectors)

    # ---- запись ----------------------------------------------------------

    def add(self, texts: list[str], vectors: list[list[float]],
            metadatas: list[dict[str, Any]] | None = None) -> int:
        import numpy as np
        if not texts:
            return 0
        metadatas = metadatas or [{} for _ in texts]
        matrix = np.array(vectors, dtype="float32")
        # Нормализуем: тогда скалярное произведение = косинусная близость
        norms = np.linalg.norm(matrix, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        matrix = matrix / norms
        self._vectors = matrix if self._vectors is None else np.vstack([self._vectors, matrix])
        for text, meta in zip(texts, metadatas):
            self.documents.append({"text": text, "metadata": meta})
        self.save()
        return len(texts)

    # ---- поиск -----------------------------------------------------------

    def search(self, vector: list[float], top_k: int = 5) -> list[dict[str, Any]]:
        if self._vectors is None or not self.documents:
            return []
        import numpy as np
        query = np.array(vector, dtype="float32")
        norm = np.linalg.norm(query) or 1.0
        query = query / norm
        scores = self._vectors @ query
        top = np.argsort(-scores)[:top_k]
        return [
            {"text": self.documents[i]["text"],
             "metadata": self.documents[i].get("metadata", {}),
             "score": float(scores[i])}
            for i in top if i < len(self.documents)
        ]

    def __len__(self) -> int:
        return len(self.documents)


class RetrievalAgent:
    """Индексация и поиск по локальной базе знаний."""

    version = RETRIEVAL_VERSION

    def __init__(self, gateway, index_dir: Path | str, dim: int = 1024):
        self.gateway = gateway
        self.index = VectorIndex(index_dir, dim=dim)

    @staticmethod
    def chunk(text: str, size: int = 1200, overlap: int = 150) -> list[str]:
        """Нарезка текста с перекрытием, чтобы факты не рвались по границе."""
        text = (text or "").strip()
        if not text:
            return []
        step = max(1, size - overlap)
        return [text[i:i + size] for i in range(0, len(text), step) if text[i:i + size].strip()]

    def index_text(self, text: str, metadata: dict[str, Any] | None = None) -> int:
        chunks = self.chunk(text)
        if not chunks:
            return 0
        vectors = self.gateway.embed(chunks)
        metas = [dict(metadata or {}, chunk=i) for i in range(len(chunks))]
        return self.index.add(chunks, vectors, metas)

    def index_file(self, path: Path | str) -> int:
        file_path = Path(path)
        text = file_path.read_text(encoding="utf-8", errors="replace")
        return self.index_text(text, {"source": str(file_path), "name": file_path.name})

    def search(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        if len(self.index) == 0:
            return []
        vector = self.gateway.embed([query])[0]
        return self.index.search(vector, top_k=top_k)

    def context_block(self, query: str, top_k: int = 5, max_chars: int = 2500) -> str:
        """Найденные фрагменты в виде блока для system prompt."""
        hits = self.search(query, top_k=top_k)
        if not hits:
            return ""
        lines = ["НАЙДЕНО В ЛОКАЛЬНОЙ БАЗЕ ЗНАНИЙ:"]
        used = 0
        for hit in hits:
            source = hit["metadata"].get("name") or hit["metadata"].get("source") or "—"
            piece = f"- [{source}, релевантность {hit['score']:.2f}] {hit['text'][:600]}"
            if used + len(piece) > max_chars:
                break
            lines.append(piece)
            used += len(piece)
        return "\n".join(lines)
