"""
PersistentMemory — долговечная память платформы.

Три типа памяти, поверх одного SQLite-файла:

1. PROFILE — постоянные факты о пользователе и его предпочтениях.
   (роль, рабочие папки, любимые инструменты, конвенции).
   Обогащает контекст КАЖДОЙ задачи через Secretary.

2. FACTS — факты/знания, которые агенты сами запоминают и вспоминают
   МЕЖДУ задачами (долговечный ScratchMemory). В отличие от
   ScratchMemory в RAM, живут в БД и переживают перезапуск.

3. PAST RESULTS — обзор прошлых задач: вопрос + итоговый ответ + путь
   результата. Позволяет агенту найти похожее прошлое решение и
   переиспользовать его вместо выполнения с нуля.

Хранилище: SQLite (data/memory.sqlite3), отдельное от Blackboard,
чтобы не смешивать "решения задач" (Blackboard) и "память о мире"
(этот модуль).

Управление состоянием — КОДОМ, не LLM: так же, как Secretary/Blackboard,
память нельзя "сгаллюцинировать". LLM только вызывает remember/recall
через инструменты.
"""

from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

MEMORY_VERSION = "1.0"
SCHEMA_VERSION = 1


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _normalize(text: str) -> str:
    """Нижний регистр + только буквы/цифры для простого совпадения."""
    return re.sub(r"[^a-zа-я0-9]+", " ", str(text).lower()).strip()


class PersistentMemory:
    """
    Долговечная память платформы (SQLite).

    Потокобезопасен: пишущие операции закрыты локалем (модели и шаги
    графа вызывают память из разных потоков).
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS profile (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS facts (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    key        TEXT NOT NULL UNIQUE,
                    value      TEXT NOT NULL,
                    source     TEXT DEFAULT 'agent',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS past_results (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id    TEXT NOT NULL UNIQUE,
                    prompt     TEXT NOT NULL,
                    answer     TEXT NOT NULL,
                    rationale  TEXT DEFAULT '',
                    mode       TEXT DEFAULT '',
                    decided_by TEXT DEFAULT '',
                    model      TEXT DEFAULT '',
                    result_path TEXT DEFAULT '',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_facts_key ON facts(key);
                CREATE INDEX IF NOT EXISTS idx_past_prompt ON past_results(prompt);
                """
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            self._conn.commit()

    # ------------------------------------------------------------------
    # PROFILE — факты о пользователе
    # ------------------------------------------------------------------

    def set_profile(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO profile(key, value, updated_at) VALUES(?, ?, ?)",
                (str(key).strip().lower(), str(value), _utcnow()),
            )
            self._conn.commit()

    def get_profile(self, key: str | None = None) -> dict[str, str]:
        with self._lock:
            if key is None:
                rows = self._conn.execute("SELECT key, value FROM profile").fetchall()
                return {r["key"]: r["value"] for r in rows}
            row = self._conn.execute(
                "SELECT value FROM profile WHERE key=?", (str(key).strip().lower(),)
            ).fetchone()
            return {str(key).strip().lower(): row["value"]} if row else {}

    def profile_text(self) -> str:
        """Сводка профиля для system prompt (детерминированно)."""
        data = self.get_profile()
        if not data:
            return ""
        lines = [f"- {k}: {v}" for k, v in data.items()]
        return "ПРОФИЛЬ ПОЛЬЗОВАТЕЛЯ:\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # FACTS — знания, которые агенты запоминают между задачами
    # ------------------------------------------------------------------

    def remember_fact(self, key: str, value: str, source: str = "agent") -> None:
        """Сохранить факт. Перезаписывает по ключу."""
        with self._lock:
            now = _utcnow()
            self._conn.execute(
                """
                INSERT INTO facts(key, value, source, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, source=excluded.source, updated_at=excluded.updated_at
                """,
                (str(key).strip().lower(), str(value), source, now, now),
            )
            self._conn.commit()

    def recall_fact(self, key: str | None = None) -> dict[str, str]:
        with self._lock:
            if key is None:
                rows = self._conn.execute(
                    "SELECT key, value FROM facts ORDER BY updated_at DESC"
                ).fetchall()
                return {r["key"]: r["value"] for r in rows}
            row = self._conn.execute(
                "SELECT value FROM facts WHERE key=?", (str(key).strip().lower(),)
            ).fetchone()
            return {str(key).strip().lower(): row["value"]} if row else {}

    def search_facts(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        """Простой текстовый поиск фактов по ключу и значению."""
        terms = _normalize(query).split()
        if not terms:
            return []
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM facts").fetchall()
            scored: list[tuple[int, dict[str, Any]]] = []
            for r in rows:
                hay = _normalize(f"{r['key']} {r['value']}")
                score = sum(1 for t in terms if t in hay)
                if score:
                    scored.append((score, {"key": r["key"], "value": r["value"]}))
            scored.sort(key=lambda x: -x[0])
            return [d for _, d in scored[:top_k]]

    def facts_text(self, top_k: int = 10) -> str:
        """Сводка самых свежих фактов для system prompt."""
        data = self.recall_fact()
        if not data:
            return ""
        items = list(data.items())[:top_k]
        lines = [f"- {k}: {v}" for k, v in items]
        return "ИЗВЕСТНЫЕ ФАКТЫ (память):\n" + "\n".join(lines)

    # ------------------------------------------------------------------
    # PAST RESULTS — результаты прошлых задач
    # ------------------------------------------------------------------

    def save_past_result(
        self,
        task_id: str,
        prompt: str,
        answer: str,
        rationale: str = "",
        mode: str = "",
        decided_by: str = "",
        model: str = "",
        result_path: str = "",
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO past_results(
                    task_id, prompt, answer, rationale, mode, decided_by,
                    model, result_path, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, prompt, answer, rationale, mode, decided_by,
                 model, result_path, _utcnow()),
            )
            self._conn.commit()

    def search_past(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        """Найти похожие прошлые задачи по вопросу (простое совпадение слов)."""
        terms = _normalize(query).split()
        if not terms:
            return []
        with self._lock:
            rows = self._conn.execute(
                "SELECT prompt, answer, rationale, mode, model, result_path, "
                "created_at FROM past_results ORDER BY created_at DESC"
            ).fetchall()
            scored: list[tuple[int, dict[str, Any]]] = []
            for r in rows:
                hay = _normalize(f"{r['prompt']} {r['answer']}")
                score = sum(1 for t in terms if t in hay)
                if score:
                    scored.append((score, {
                        "prompt": r["prompt"],
                        "answer": r["answer"],
                        "rationale": r["rationale"],
                        "mode": r["mode"],
                        "model": r["model"],
                        "result_path": r["result_path"],
                        "created_at": r["created_at"],
                    }))
            scored.sort(key=lambda x: -x[0])
            return [d for _, d in scored[:top_k]]

    def past_results_text(self, query: str, top_k: int = 2) -> str:
        """Блок «похожие прошлые решения» для system prompt агента."""
        hits = self.search_past(query, top_k=top_k)
        if not hits:
            return ""
        blocks = []
        for h in hits:
            blocks.append(
                f"ВОПРОС: {h['prompt'][:200]}\n"
                f"ОТВЕТ: {h['answer'][:300]}"
                + (f"\nПУТЬ: {h['result_path']}" if h.get("result_path") else "")
            )
        return "ПОХОЖИЕ ПРОШЛЫЕ РЕШЕНИЯ:\n" + "\n\n".join(blocks)

    # ------------------------------------------------------------------
    # Общий контекст для агентов (детерминированный)
    # ------------------------------------------------------------------

    def context_block(self, query: str = "") -> str:
        """Собрать блок памяти для system prompt агентов."""
        parts: list[str] = []
        p = self.profile_text()
        if p:
            parts.append(p)
        f = self.facts_text()
        if f:
            parts.append(f)
        if query:
            past = self.past_results_text(query)
            if past:
                parts.append(past)
        return "\n\n".join(parts)

    def clear_all(self) -> None:
        with self._lock:
            for table in ("profile", "facts", "past_results"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    def stats(self) -> dict[str, int]:
        with self._lock:
            counts = {}
            for table in ("profile", "facts", "past_results"):
                row = self._conn.execute(f"SELECT COUNT(*) c FROM {table}").fetchone()
                counts[table] = row["c"]
            return counts

    def close(self) -> None:
        with self._lock:
            self._conn.close()
