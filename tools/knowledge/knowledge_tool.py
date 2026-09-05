"""
Инструмент knowledge: локальная база знаний для моделей.

Действия:
  search   — гибридный поиск (точный + смысловой) по базе.
  add_text — добавить текст напрямую (вывод задачи, описание, заметку).
  add_file — добавить файл (txt/md/pdf/csv/xlsx/json/geojson) с извлечением.
  list     — список документов базы.
  delete   — удалить документ по doc_id.
  stats    — статистика базы.

База: data/knowledge.sqlite3 (FTS5) + knowledge_vectors.npy (bge-m3).
Всё локально.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from core.base_tool import BaseTool, ToolAction, ToolStatus
from core.knowledge import KnowledgeBase


class KnowledgeTool(BaseTool):
    name = "knowledge"
    version = "1.0.0"
    description = (
        "Локальная база знаний: поиск (search) по добавленным документам — "
        "точный и смысловой; добавление знаний (add_text для текста, "
        "add_file для файлов: txt/md/pdf/csv/xlsx/json/geojson); список "
        "документов (list); удаление (delete); статистика (stats). "
        "Используй search перед ответом на вопросы, где могут быть прошлые "
        "знания, и add_text чтобы сохранить важный вывод на будущее."
    )
    requires_context = True

    def __init__(self, config: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, context=context)
        self._kb: KnowledgeBase | None = None
        self._lock = threading.Lock()

    def _kb_path(self) -> Path:
        raw = (self.config or {}).get("db_path", "data/knowledge.sqlite3")
        p = Path(raw)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent.parent / p
        return p

    @property
    def kb(self) -> KnowledgeBase:
        with self._lock:
            if self._kb is None:
                # приоритет: manifest config -> settings.yaml -> дефолт
                backend = str((self.config or {}).get("embed_backend") or "")
                if not backend:
                    try:
                        import yaml
                        s = yaml.safe_load(open(
                            Path(__file__).resolve().parent.parent.parent
                            / "config" / "settings.yaml", encoding="utf-8")) or {}
                        backend = str((s.get("paths") or {}).get(
                            "knowledge_embed_backend", "ollama-bge"))
                    except Exception:  # noqa: BLE001
                        backend = "ollama-bge"
                self._kb = KnowledgeBase(self._kb_path(),
                                         embed_backend=backend)
            return self._kb

    def _result(self, ok: bool, data: dict[str, Any],
                summary: str) -> dict[str, Any]:
        return {"ok": ok, "data": data, "summary": summary[:400],
                "error": None if ok else summary}

    # ---- извлечение текста из файлов -------------------------------------

    def _extract_file(self, path: Path) -> tuple[str, str]:
        """(текст, doc_type) по расширению. Бросает ValueError с понятной
        ошибкой, если формат не поддержан."""
        ext = path.suffix.lower()
        if ext in (".txt", ".md", ".log", ".gpx", ".wkt"):
            return path.read_text(encoding="utf-8", errors="replace"), "text"
        if ext == ".pdf":
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            pages = [(p.extract_text() or "") for p in reader.pages]
            return "\n\n".join(pages), "pdf"
        if ext == ".docx":
            import docx
            d = docx.Document(str(path))
            return "\n".join(p.text for p in d.paragraphs), "docx"
        if ext in (".csv", ".tsv"):
            import csv
            delim = "\t" if ext == ".tsv" else ","
            with open(path, encoding="utf-8-sig", errors="replace",
                      newline="") as f:
                rows = list(csv.reader(f, delimiter=delim))
            if not rows:
                raise ValueError("CSV пуст.")
            header = rows[0]
            lines = [" | ".join(header)]
            for row in rows[1:]:
                lines.append(" | ".join(row))
            return "\n".join(lines), "csv"
        if ext in (".xlsx", ".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True,
                                        data_only=True)
            parts: list[str] = []
            for ws in wb.worksheets:
                parts.append(f"### Лист: {ws.title}")
                for row in ws.iter_rows(values_only=True):
                    cells = ["" if c is None else str(c) for c in row]
                    if any(c.strip() for c in cells):
                        parts.append(" | ".join(cells))
            wb.close()
            return "\n".join(parts), "xlsx"
        if ext in (".json", ".geojson"):
            data = json.loads(path.read_text(encoding="utf-8"))
            # GeoJSON: вытащить свойства фич (текстовые атрибуты точек)
            feats = (data.get("features")
                     if isinstance(data, dict) else None)
            if feats:
                lines = []
                for f in feats[:5000]:
                    props = f.get("properties") or {}
                    if props:
                        lines.append(json.dumps(props, ensure_ascii=False))
                return "\n".join(lines), "geojson"
            return json.dumps(data, ensure_ascii=False, indent=1), "json"
        raise ValueError(
            f"Формат '{ext}' не поддержан. Доступны: txt, md, log, pdf, "
            "docx, csv, tsv, xlsx, json, geojson, gpx, wkt.")

    # ---- execute -----------------------------------------------------------

    def actions(self) -> list[ToolAction]:
        return [
            ToolAction(
                "search",
                "Гибридный поиск по базе знаний: точные слова + смысл. "
                "Ищи здесь перед ответом на вопросы, где могут быть прошлые "
                "знания проекта.",
                {"query": {"type": "string", "description": "Поисковый запрос"},
                 "top_k": {"type": "integer", "description": "Сколько "
                           "результатов (по умолчанию 5)", "optional": True},
                 "doc_type": {"type": "string", "description": "Фильтр по "
                              "типу: text/pdf/csv/xlsx/json", "optional": True},
                 "tag": {"type": "string", "description": "Фильтр по тегу",
                         "optional": True},
                 "mode": {"type": "string", "description": "hybrid (по "
                          "умолчанию) | fts | vector", "optional": True}},
            ),
            ToolAction(
                "add_text",
                "Добавить знание текстом. Используй, чтобы сохранить важный "
                "вывод задачи, описание, факт — на будущее.",
                {"text": {"type": "string", "description": "Текст знания"},
                 "title": {"type": "string", "description": "Короткое название",
                           "optional": True},
                 "tags": {"type": "string", "description": "Теги через запятую",
                          "optional": True}},
            ),
            ToolAction(
                "add_file",
                "Добавить файл в базу: txt, md, pdf, docx, csv, tsv, xlsx, "
                "json, geojson, gpx. Текст извлекается и индексируется.",
                {"path": {"type": "string", "description": "Полный путь к "
                          "файлу"},
                 "title": {"type": "string", "description": "Название",
                           "optional": True},
                 "tags": {"type": "string", "description": "Теги через запятую",
                          "optional": True}},
            ),
            ToolAction(
                "list",
                "Список документов базы с doc_id (нужен для delete).", {}),
            ToolAction(
                "delete",
                "Удалить документ из базы по doc_id (из list).",
                {"doc_id": {"type": "string", "description": "ID документа"}},
            ),
            ToolAction(
                "retag",
                "Изменить теги документа без переиндексации.",
                {"doc_id": {"type": "string", "description": "ID документа"},
                 "tags": {"type": "string", "description": "Новые теги через "
                          "запятую"}},
            ),
            ToolAction(
                "stats",
                "Статистика базы: сколько документов и чанков.", {}),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            if action == "search":
                query = str(args.get("query") or "").strip()
                if not query:
                    return self._result(False, {}, "Укажи query.")
                hits = self.kb.search(
                    query,
                    top_k=int(args.get("top_k") or 5),
                    doc_type=str(args.get("doc_type") or ""),
                    tag=str(args.get("tag") or ""),
                    mode=str(args.get("mode") or "hybrid"))
                if not hits:
                    return self._result(
                        True, {"results": []},
                        "Ничего не найдено. База может быть пуста — "
                        "добавь знания через add_text/add_file.")
                lines = []
                for i, h in enumerate(hits, 1):
                    lines.append(f"{i}. [{h['via']} {h['score']:.2f}] "
                                 f"{h['title']} (чанк {h['chunk_no']})\n"
                                 f"   {h['text'][:300]}")
                return self._result(
                    True, {"results": hits},
                    f"Найдено {len(hits)}:\n" + "\n".join(lines))

            if action == "add_text":
                text = str(args.get("text") or "").strip()
                if not text:
                    return self._result(False, {}, "Укажи text.")
                title = str(args.get("title") or "заметка")
                tags = str(args.get("tags") or "")
                source = f"note://{title}"
                r = self.kb.add_document(source, text, title=title,
                                         doc_type="text", tags=tags)
                if not r.get("ok"):
                    return self._result(False, {}, r.get("error", "ошибка"))
                return self._result(
                    True, r, f"Знание '{title}' добавлено: {r['chunks']} "
                             f"чанков, doc_id={r['doc_id']}")

            if action == "add_file":
                raw = str(args.get("path") or "").strip().strip('"')
                if not raw:
                    return self._result(False, {}, "Укажи path.")
                path = Path(raw)
                if not path.is_file():
                    return self._result(False, {}, f"Файл не найден: {raw}")
                try:
                    text, doc_type = self._extract_file(path)
                except ValueError as exc:
                    return self._result(False, {}, str(exc))
                title = str(args.get("title") or path.stem)
                tags = str(args.get("tags") or "")
                r = self.kb.add_document(str(path), text, title=title,
                                         doc_type=doc_type, tags=tags)
                if not r.get("ok"):
                    return self._result(False, {}, r.get("error", "ошибка"))
                msg = (f"Файл '{path.name}' добавлен: {r['chunks']} чанков "
                       f"(тип {doc_type}), doc_id={r['doc_id']}")
                if r.get("warning"):
                    msg += f". {r['warning']}"
                return self._result(True, r, msg)

            if action == "list":
                docs = self.kb.list_documents()
                if not docs:
                    return self._result(True, {"documents": []},
                                        "База пуста.")
                lines = [f"- {d['title']} [{d['doc_type']}] "
                         f"doc_id={d['doc_id']}, чанков: {d['chunk_count']}, "
                         f"теги: {d['tags'] or '—'}" for d in docs]
                return self._result(True, {"documents": docs},
                                    f"Документов: {len(docs)}\n"
                                    + "\n".join(lines))

            if action == "delete":
                doc_id = str(args.get("doc_id") or "").strip()
                if not doc_id:
                    return self._result(False, {}, "Укажи doc_id (из list).")
                r = self.kb.delete_document(doc_id)
                return (self._result(True, r, f"Удалён {doc_id}.")
                        if r.get("ok")
                        else self._result(False, {}, r.get("error", "")))

            if action == "retag":
                doc_id = str(args.get("doc_id") or "").strip()
                tags = str(args.get("tags") or "").strip()
                if not doc_id:
                    return self._result(False, {}, "Укажи doc_id (из list).")
                r = self.kb.retag(doc_id, tags)
                return (self._result(True, r, f"Теги {doc_id} -> '{tags}'.")
                        if r.get("ok")
                        else self._result(False, {}, r.get("error", "")))

            if action == "stats":
                s = self.kb.stats()
                return self._result(
                    True, s, f"База знаний: {s['documents']} документов, "
                             f"{s['chunks']} чанков.")

            return self._result(False, {}, f"Неизвестное действие: {action}")
        except Exception as exc:  # noqa: BLE001
            return self._result(False, {}, f"{type(exc).__name__}: {exc}")

    def health(self) -> tuple[ToolStatus, str]:
        try:
            import sqlite3
            c = sqlite3.connect(":memory:")
            c.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            c.close()
            import urllib.request
            req = urllib.request.Request(
                "http://127.0.0.1:11434/api/embed",
                data=json.dumps({"model": "bge-m3", "input": "тест"}).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=10)
            backend = str((self.config or {}).get(
                "embed_backend", "ollama-bge"))
            return ToolStatus.READY, f"FTS5 + {backend} доступны"
        except Exception as exc:  # noqa: BLE001
            return (ToolStatus.UNAVAILABLE,
                    f"FTS5/Ollama недоступны: {exc}. Смысловой поиск не "
                    "работает, точный — да.")
