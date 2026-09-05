"""
Knowledge Base (вариант B, 25.08) — гибридная локальная база знаний.

Архитектура: SQLite FTS5 (точный/полнотекстовый поиск) + вектора bge-m3
(смысловой поиск), один файл data/knowledge.sqlite3 для текстов и метаданных,
вектора рядом в knowledge_vectors.npy.

Слои поиска:
  1. FTS5 — полнотекст по чанкам (быстро, точно по словам).
  2. Вектора — косинусная близость (смысл, синонимы).
  3. Метаданные — SQL-фильтры (источник, тип, дата, теги).

Гибрид: FTS даёт кандидатов, вектора переранжируют; либо вектора дают
кандидатов, FTS добавляет точные совпадения. Итог — единый список чанков.

Эмбеддинги: bge-m3 через Ollama (localhost), как в Retrieval Agent.
Всё локально: ни один байт не покидает машину.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

KB_VERSION = "1.0"
SCHEMA_VERSION = 1

CHUNK_SIZE = 500        # символов на чанк (примерно 120-160 токенов)
CHUNK_OVERLAP = 80      # перекрытие чанков, чтобы не резать мысли
EMBED_MODEL = "bge-m3"
EMBED_URL = "http://127.0.0.1:11434/api/embed"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Минимальный скор для попадания результата в контекст задачи.
# Слабые совпадения не подмешиваются (шум в промпте хуже их отсутствия).
MIN_CONTEXT_SCORE = 0.30


class KnowledgeBase:
    """Гибридная база знаний: SQLite FTS5 + вектора bge-m3."""

    def __init__(self, db_path: str | Path,
                 vectors_path: str | Path | None = None,
                 embed_backend: str = "ollama-bge") -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.vectors_path = (Path(vectors_path) if vectors_path
                             else self.path.parent / "knowledge_vectors.npy")
        self.embed_backend = embed_backend  # ollama-bge | local-pplx
        self._st_model: Any = None          # sentence-transformers (lazy)
        self._vector_ids: list[int] = []   # порядок строк в vectors.npy
        self._vectors_stale = False
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # ---- схема -----------------------------------------------------------

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS kb_meta (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS documents (
                    id       INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id   TEXT NOT NULL UNIQUE,      -- hash пути+имени
                    source   TEXT NOT NULL,             -- путь/URL
                    title    TEXT DEFAULT '',
                    doc_type TEXT DEFAULT 'text',       -- text/pdf/xlsx/csv/json/web/vision
                    tags     TEXT DEFAULT '',           -- 'тег1,тег2'
                    added_at TEXT NOT NULL,
                    chunk_count INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id     TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
                    chunk_no   INTEGER NOT NULL,
                    text       TEXT NOT NULL,
                    meta_json  TEXT DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
                CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                    text, content='chunks', content_rowid='id',
                    tokenize='unicode61 remove_diacritics 2'
                );
                -- триггеры синхронизации FTS с chunks
                CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                    INSERT INTO chunks_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                    INSERT INTO chunks_fts(chunks_fts, rowid, text)
                    VALUES ('delete', old.id, old.text);
                END;
            """)
            self._conn.execute(
                "INSERT OR REPLACE INTO kb_meta VALUES ('kb_version', ?)",
                (KB_VERSION,))
            self._conn.commit()

    # ---- эмбеддинги --------------------------------------------------------

    def _embed(self, texts: list[str]) -> list[list[float]]:
        """Эмбеддинги: ollama-bge (HTTP) или local-pplx (sentence-transformers).
        ВАЖНО: вектора разных бэкендов несовместимы — при смене бэкенда базу
        нужно переиндексировать."""
        if self.embed_backend == "local-pplx":
            return self._embed_pplx(texts)
        return self._embed_ollama(texts)

    def _embed_ollama(self, texts: list[str]) -> list[list[float]]:
        import urllib.request
        out: list[list[float]] = []
        # Ollama лучше переваривает пачки по ~16
        for i in range(0, len(texts), 16):
            batch = texts[i:i + 16]
            req = urllib.request.Request(
                EMBED_URL,
                data=json.dumps({"model": EMBED_MODEL,
                                 "input": batch}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out.extend(data["embeddings"])
        return out

    PPLX_MODEL = "perplexity-ai/pplx-embed-v1-0.6b"

    def _embed_pplx(self, texts: list[str]) -> list[list[float]]:
        """Локальный бэкенд pplx-embed-v1-0.6B (sentence-transformers, INT8).
        Модель грузится лениво один раз и живёт в RAM (CPU/GPU по умолчанию
        sentence-transformers)."""
        if self._st_model is None:
            from sentence_transformers import SentenceTransformer
            self._st_model = SentenceTransformer(
                self.PPLX_MODEL, trust_remote_code=True)
        emb = self._st_model.encode(texts)
        return [list(map(float, row)) for row in emb]

    def _load_vectors(self) -> Any:
        import numpy as np
        if self.vectors_path.is_file():
            return np.load(str(self.vectors_path))
        return None

    def _save_vectors(self, matrix: Any) -> None:
        import numpy as np
        np.save(str(self.vectors_path), matrix)

    # ---- добавление --------------------------------------------------------

    @staticmethod
    def chunk_text(text: str, size: int = CHUNK_SIZE,
                   overlap: int = CHUNK_OVERLAP) -> list[str]:
        """Резать текст на чанки по абзацам, с перекрытием."""
        text = re.sub(r"\r\n", "\n", text).strip()
        if len(text) <= size:
            return [text] if text else []
        chunks: list[str] = []
        start = 0
        while start < len(text):
            end = min(start + size, len(text))
            # резать по последнему \n внутри окна, чтобы не рывать абзацы
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut
            chunks.append(text[start:end].strip())
            start = end - overlap if end < len(text) else end
        return [c for c in chunks if c]

    def add_document(self, source: str, text: str,
                     title: str = "", doc_type: str = "text",
                     tags: str = "", meta: dict[str, Any] | None = None
                     ) -> dict[str, Any]:
        """
        Добавить документ: режется на чанки, чанки в SQLite+FTS,
        эмбеддинги в векторный файл. Возвращает сводку.
        """
        doc_id = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
        chunks = self.chunk_text(text)
        if not chunks:
            return {"ok": False, "error": "Пустой текст после извлечения.",
                    "doc_id": doc_id, "chunks": 0}

        with self._lock:
            # повторное добавление = замена
            self._conn.execute(
                "DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            self._conn.execute(
                "INSERT INTO documents (doc_id, source, title, doc_type, "
                "tags, added_at, chunk_count) VALUES (?,?,?,?,?,?,?)",
                (doc_id, source, title or Path(source).name, doc_type,
                 tags, _utcnow(), len(chunks)))
            for n, ch in enumerate(chunks):
                self._conn.execute(
                    "INSERT INTO chunks (doc_id, chunk_no, text, meta_json) "
                    "VALUES (?,?,?,?)",
                    (doc_id, n, ch,
                     json.dumps(meta or {}, ensure_ascii=False)))
            self._conn.commit()

        # вектора: пересобираем матрицу (простота > скорость на локальных
        # объёмах; при >50к чанков перейти на добавление по одному)
        try:
            vectors = self._embed(chunks)
            self._merge_vectors(doc_id, vectors)
        except Exception as exc:  # noqa: BLE001
            # текст уже в FTS — смысловой поиск не работает, точный работает
            return {"ok": True, "doc_id": doc_id, "chunks": len(chunks),
                    "warning": f"Эмбеддинги не удались ({exc}); "
                               "доступен только FTS-поиск"}

        return {"ok": True, "doc_id": doc_id, "chunks": len(chunks),
                "vectors": len(vectors)}

    def _merge_vectors(self, doc_id: str, vectors: list[list[float]]) -> None:
        import numpy as np
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, doc_id FROM chunks ORDER BY id").fetchall()
            old = self._load_vectors()
            keep_ids = [r["id"] for r in rows]
            keep_docs = [r["doc_id"] for r in rows]
            # старые вектора: оставляем только для выживших чанков
            if old is not None and len(old) >= 1:
                old_map = {cid: i for i, cid in enumerate(self._vector_ids)}
                kept = [old[old_map[cid]] for cid in keep_ids
                        if cid in old_map]
            else:
                kept = []
            new_ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM chunks WHERE doc_id = ? ORDER BY chunk_no",
                (doc_id,)).fetchall()]
            # новые чанки этого doc_id: берём последние len(new_ids) векторов
            new_vecs = vectors[-len(new_ids):] if new_ids else []
            matrix = np.array(kept + new_vecs, dtype="float32")
            if len(matrix):
                norms = np.linalg.norm(matrix, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                matrix = matrix / norms
            self._vector_ids = keep_ids
            self._save_vectors(matrix)


    def _ensure_vector_ids(self) -> None:
        if not self._vector_ids:
            with self._lock:
                rows = self._conn.execute(
                    "SELECT id FROM chunks ORDER BY id").fetchall()
                self._vector_ids = [r["id"] for r in rows]
                old = self._load_vectors()
                if old is not None and len(old) != len(self._vector_ids):
                    # рассинхрон — сбрасываем векторы, FTS остаётся
                    self._vectors_stale = True

    # ---- поиск --------------------------------------------------------------

    def search(self, query: str, top_k: int = 5,
               doc_type: str = "", tag: str = "",
               mode: str = "hybrid") -> list[dict[str, Any]]:
        """
        Гибридный поиск. mode: 'fts' | 'vector' | 'hybrid'.
        Возвращает чанки: {text, source, title, score, chunk_no}.
        """
        results: dict[int, dict[str, Any]] = {}

        with self._lock:
            where, params = "", []
            filters = []
            if doc_type:
                filters.append("d.doc_type = ?")
                params.append(doc_type)
            if tag:
                filters.append("d.tags LIKE ?")
                params.append(f"%{tag}%")
            where = (" AND " + " AND ".join(filters)) if filters else ""

            # 1) FTS
            if mode in ("fts", "hybrid"):
                try:
                    fts_q = " ".join(re.findall(r"[\wа-яё]+", query.lower()))
                    rows = self._conn.execute(
                        "SELECT c.id, c.text, c.chunk_no, d.source, d.title, "
                        "d.doc_type, d.tags, bm25(chunks_fts) AS rank "
                        "FROM chunks_fts f "
                        "JOIN chunks c ON c.id = f.rowid "
                        "JOIN documents d ON d.doc_id = c.doc_id "
                        "WHERE chunks_fts MATCH ?" + where +
                        " ORDER BY rank LIMIT ?",
                        [fts_q] + params + [top_k * 3]).fetchall()
                    for r in rows:
                        results[r["id"]] = {
                            "text": r["text"], "source": r["source"],
                            "title": r["title"], "chunk_no": r["chunk_no"],
                            "doc_type": r["doc_type"], "tags": r["tags"],
                            "score": max(0.0, 1.0 / (1.0 + abs(r["rank"]))),
                            "via": "fts"}
                except Exception:  # noqa: BLE001 — пустой/невалидный запрос
                    pass

            # 2) вектора
            if mode in ("vector", "hybrid"):
                try:
                    import numpy as np
                    qvec = self._embed([query])[0]
                    matrix = self._load_vectors()
                    self._ensure_vector_ids()
                    if matrix is not None and len(self._vector_ids):
                        q = np.array(qvec, dtype="float32")
                        n = np.linalg.norm(q)
                        if n:
                            q = q / n
                        scores = matrix @ q
                        top = np.argsort(scores)[::-1][:top_k * 3]
                        id_by_pos = {i: cid for i, cid in
                                     enumerate(self._vector_ids)}
                        for pos in top:
                            cid = id_by_pos.get(int(pos))
                            if cid is None or cid in results:
                                continue
                            r = self._conn.execute(
                                "SELECT c.text, c.chunk_no, d.source, "
                                "d.title, d.doc_type, d.tags "
                                "FROM chunks c JOIN documents d "
                                "ON d.doc_id = c.doc_id WHERE c.id = ?",
                                (cid,)).fetchone()
                            if r is None:
                                continue
                            if doc_type and r["doc_type"] != doc_type:
                                continue
                            results[cid] = {
                                "text": r["text"], "source": r["source"],
                                "title": r["title"],
                                "chunk_no": r["chunk_no"],
                                "doc_type": r["doc_type"],
                                "tags": r["tags"],
                                "score": float(scores[pos]), "via": "vector"}
                except Exception:  # noqa: BLE001 — Ollama недоступна и т.п.
                    pass

        ranked = sorted(results.values(),
                        key=lambda x: x["score"], reverse=True)
        return ranked[:top_k]

    # ---- управление ----------------------------------------------------------

    def list_documents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT doc_id, source, title, doc_type, tags, added_at, "
                "chunk_count FROM documents ORDER BY added_at DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_document(self, doc_id: str) -> dict[str, Any]:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            self._conn.commit()
            # пересобрать векторный файл без удалённых
            rows = self._conn.execute(
                "SELECT id FROM chunks ORDER BY id").fetchall()
            keep_ids = [r["id"] for r in rows]
            old = self._load_vectors()
            if old is not None and hasattr(self, "_vector_ids"):
                import numpy as np
                id_pos = {cid: i for i, cid in enumerate(self._vector_ids)}
                kept = [old[id_pos[cid]] for cid in keep_ids if cid in id_pos]
                if kept:
                    self._save_vectors(np.array(kept, dtype="float32"))
                else:
                    self.vectors_path.unlink(missing_ok=True)
            self._vector_ids = keep_ids
        if cur.rowcount:
            return {"ok": True, "deleted": doc_id}
        return {"ok": False, "error": f"Документ {doc_id} не найден"}

    def retag(self, doc_id: str, tags: str) -> dict[str, Any]:
        """Изменить теги документа без переиндексации."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE documents SET tags = ? WHERE doc_id = ?",
                (tags, doc_id))
            self._conn.commit()
        if cur.rowcount:
            return {"ok": True, "doc_id": doc_id, "tags": tags}
        return {"ok": False, "error": f"Документ {doc_id} не найден"}

    def stats(self) -> dict[str, int]:
        with self._lock:
            docs = self._conn.execute(
                "SELECT COUNT(*) c FROM documents").fetchone()["c"]
            chunks = self._conn.execute(
                "SELECT COUNT(*) c FROM chunks").fetchone()["c"]
        return {"documents": docs, "chunks": chunks}

    def close(self) -> None:
        with self._lock:
            self._conn.close()
