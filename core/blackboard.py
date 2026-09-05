"""
Secretary / Blackboard (слой 2) — общая память платформы.

Хранилище: SQLite (файл из config.paths.blackboard_db).
Управление состоянием — КОДОМ, не LLM: сводка контекста собирается
детерминированно, поэтому её нельзя «сгаллюцинировать».

Схема версионируется: таблица schema_meta + список MIGRATIONS.
Добавление поля = новая миграция, старые базы открываются без потери данных.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from core.schemas import (
    FinalDecision,
    Plan,
    Proposal,
    RoutingDecision,
    StepResult,
    Task,
    TaskRecord,
    ToolCallResult,
    Verdict,
)

BLACKBOARD_VERSION = "1.0"
SCHEMA_VERSION = 7  # целевая версия схемы БД


# --------------------------------------------------------------------------
# Миграции схемы
# --------------------------------------------------------------------------


def _migration_1(conn: sqlite3.Connection) -> None:
    """v1: базовые таблицы задач, вызовов инструментов и решений."""
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            task_id         TEXT PRIMARY KEY,
            prompt          TEXT NOT NULL,
            mode            TEXT NOT NULL,
            status          TEXT NOT NULL DEFAULT 'created',
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            context_summary TEXT DEFAULT '',
            routing_json    TEXT,
            verdict_json    TEXT,
            final_json      TEXT,
            error           TEXT,
            metadata_json   TEXT DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS proposals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL,
            proposer_id TEXT NOT NULL,
            model       TEXT NOT NULL,
            provider    TEXT NOT NULL,
            answer      TEXT,
            ok          INTEGER NOT NULL DEFAULT 1,
            error       TEXT,
            latency_ms  INTEGER DEFAULT 0,
            payload_json TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(task_id)
        );

        CREATE TABLE IF NOT EXISTS tool_calls_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id      TEXT NOT NULL,
            caller       TEXT NOT NULL,
            tool         TEXT NOT NULL,
            action       TEXT NOT NULL,
            tool_version TEXT,
            ok           INTEGER NOT NULL,
            args_json    TEXT,
            summary      TEXT,
            error        TEXT,
            duration_ms  INTEGER DEFAULT 0,
            created_at   TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS decisions_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id    TEXT NOT NULL,
            stage      TEXT NOT NULL,
            actor      TEXT NOT NULL,
            summary    TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_proposals_task ON proposals(task_id);
        CREATE INDEX IF NOT EXISTS idx_tools_task ON tool_calls_log(task_id);
        CREATE INDEX IF NOT EXISTS idx_decisions_task ON decisions_log(task_id);
        """
    )


def _migration_2(conn: sqlite3.Connection) -> None:
    """
    v2: версии компонентов в журнале решений (нужно для трейсов Langfuse)
    и учёт использования облака у задачи.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(decisions_log)")}
    if "component_version" not in cols:
        conn.execute("ALTER TABLE decisions_log ADD COLUMN component_version TEXT DEFAULT ''")
    task_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "used_cloud" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN used_cloud INTEGER DEFAULT 0")
    if "trace_id" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN trace_id TEXT")


def _migration_3(conn: sqlite3.Connection) -> None:
    """
    v3: режим оркестратора — план шагов и результат каждого шага.

    Шаги хранятся ОТДЕЛЬНОЙ таблицей, а не одним JSON-полем: так
    интерфейс может показывать ход выполнения шаг за шагом и можно
    считать статистику по исполнителям обычным GROUP BY.
    """
    task_cols = {row[1] for row in conn.execute("PRAGMA table_info(tasks)")}
    if "plan_json" not in task_cols:
        conn.execute("ALTER TABLE tasks ADD COLUMN plan_json TEXT")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS plan_steps (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL,
            step_id     TEXT NOT NULL,
            title       TEXT DEFAULT '',
            assignee    TEXT DEFAULT '',
            model       TEXT DEFAULT '',
            answer      TEXT DEFAULT '',
            ok          INTEGER DEFAULT 1,
            error       TEXT,
            latency_ms  INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_plan_steps_task ON plan_steps(task_id)")


def _migration_4(conn: sqlite3.Connection) -> None:
    """
    v4: предупреждения к шагу (см. core/step_check.py) и счётчик повторов.

    Шаг может завершиться «успешно», ни разу не вызвав выданный инструмент —
    такой ответ построен на догадке. Помечаем это, чтобы видел и сборщик,
    и пользователь в интерфейсе.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plan_steps)")}
    if "warnings_json" not in cols:
        conn.execute("ALTER TABLE plan_steps ADD COLUMN warnings_json TEXT")
    if "suspicious" not in cols:
        conn.execute("ALTER TABLE plan_steps ADD COLUMN suspicious INTEGER DEFAULT 0")
    if "attempt" not in cols:
        conn.execute("ALTER TABLE plan_steps ADD COLUMN attempt INTEGER DEFAULT 1")


def _migration_5(conn: sqlite3.Connection) -> None:
    """
    v5: живой статус шага (waiting/running/done/failed).

    Раньше шаги писались в БД только батчем в конце всего плана — поэтому
    «Ход выполнения» в панели молчал, а статус задачи висел в created весь
    прогон. Теперь каждый шаг обновляется по мере выполнения, чтобы любой
    процесс (UI или CLI) видел прогресс в реалтайме.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(plan_steps)")}
    if "status" not in cols:
        conn.execute("ALTER TABLE plan_steps ADD COLUMN status TEXT DEFAULT 'waiting'")


# Порядок важен: применяются подряд от текущей версии до SCHEMA_VERSION
def _migration_6(conn: sqlite3.Connection) -> None:
    """v6: применена ранее в боевой БД (см. schema_meta); пусто для консистентности."""


def _migration_7(conn: sqlite3.Connection) -> None:
    """v7: журнал LLM-вызовов — success rate, tps и латентность по моделям."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_calls (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT,
            model       TEXT NOT NULL,
            provider    TEXT NOT NULL,
            purpose     TEXT,
            ok          INTEGER NOT NULL DEFAULT 1,
            latency_ms  INTEGER DEFAULT 0,
            eval_count  INTEGER DEFAULT 0,
            eval_duration_ms INTEGER DEFAULT 0,
            prompt_eval_count INTEGER DEFAULT 0,
            tps         REAL DEFAULT 0,
            created_at  TEXT NOT NULL
        )
    """)


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {
    1: _migration_1,
    2: _migration_2,
    3: _migration_3,
    4: _migration_4,
    5: _migration_5,
    6: _migration_6,
    7: _migration_7,
}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


class Blackboard:
    """
    Общая память задач.

    Потокобезопасен: одно соединение + Lock, режим WAL.
    Все операции — короткие транзакции, чтобы UI мог читать во время работы графа.
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self.migrate()

    # ---- схема и миграции ----------------------------------------------

    def _current_version(self) -> int:
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta ("
            " id INTEGER PRIMARY KEY CHECK (id = 1),"
            " schema_version INTEGER NOT NULL,"
            " updated_at TEXT NOT NULL)"
        )
        row = self._conn.execute("SELECT schema_version FROM schema_meta WHERE id = 1").fetchone()
        return int(row["schema_version"]) if row else 0

    def migrate(self) -> int:
        """Привести БД к SCHEMA_VERSION. Идемпотентно."""
        with self._lock:
            version = self._current_version()
            for target in range(version + 1, SCHEMA_VERSION + 1):
                migration = MIGRATIONS.get(target)
                if migration is None:
                    raise RuntimeError(f"Нет миграции Blackboard до версии {target}")
                migration(self._conn)
                self._conn.execute(
                    "INSERT INTO schema_meta (id, schema_version, updated_at) VALUES (1, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET schema_version=excluded.schema_version,"
                    " updated_at=excluded.updated_at",
                    (target, _utcnow_iso()),
                )
                self._conn.commit()
            return self._current_version()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._current_version()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- задачи ---------------------------------------------------------

    def create_task(self, task: Task) -> str:
        """Зарегистрировать новую задачу."""
        now = _utcnow_iso()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (task_id, prompt, mode, status, created_at, updated_at,"
                "  context_summary, metadata_json)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (task.task_id, task.prompt, task.mode.value, "created",
                 task.created_at.isoformat(), now, "", _dumps(task.metadata)),
            )
            self._conn.commit()
        self.log_decision(task.task_id, "created", "secretary",
                          f"Задача принята (режим {task.mode.value})",
                          {"prompt_length": len(task.prompt)}, BLACKBOARD_VERSION)
        return task.task_id

    def fail_orphaned_running_tasks(self, grace_minutes: int = 10) -> list[str]:
        """
        Пометить failed задачи, застрявшие в running: их рабочий поток умер
        (например, панель перезапустили посреди выполнения). Вызывается при
        старте платформы — если процесс только что поднялся, ни один поток
        ещё не мог писать 'running' задачу до этого запуска.
        """
        now = datetime.utcnow()
        cutoff = (now - timedelta(minutes=grace_minutes)).isoformat()
        rows = self._conn.execute(
            "SELECT task_id FROM tasks WHERE status='running' AND created_at < ?",
            (cutoff,),
        ).fetchall()
        fixed = []
        for (task_id,) in rows:
            self._conn.execute(
                "UPDATE tasks SET status='failed', error=? WHERE task_id=?",
                ("поток задачи умер (панель перезапущена посреди выполнения) — "
                 "обнаружено при старте платформы", task_id),
            )
            self.log_decision(task_id, "orphan_cleanup", "platform",
                              "Задача застряла в running после рестарта панели — "
                              "помечена failed при старте платформы")
            fixed.append(task_id)
        if fixed:
            self._conn.commit()
        return fixed

    def get_running_task(self) -> dict[str, Any] | None:
        """
        Последняя задача со статусом 'running' (для экрана «Ход выполнения»).

        Позволяет панели показывать живую задачу, запущенную даже из другого
        процесса (CLI), — раньше она видела только состояние своего процесса.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT task_id, prompt, mode, status, created_at, error, metadata_json"
                " FROM tasks WHERE status='running'"
                " ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            item = dict(row)
            item["plan"] = self._load_json(
                "SELECT plan_json FROM tasks WHERE task_id=?",
                (item["task_id"],))
            item["steps"] = self.get_step_results(item["task_id"])
            return item

    def _load_json(self, query: str, params: tuple) -> Any:
        """Прочитать JSON-столбец из строки задачи."""
        with self._lock:
            row = self._conn.execute(query, params).fetchone()
            if not row or not row[0]:
                return None
            return _loads(row[0])

    def set_status(self, task_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
                (status, error, _utcnow_iso(), task_id),
            )
            self._conn.commit()

    def save_routing(self, task_id: str, routing: RoutingDecision) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET routing_json=?, updated_at=? WHERE task_id=?",
                (_dumps(routing.model_dump(mode="json")), _utcnow_iso(), task_id),
            )
            self._conn.commit()
        self.log_decision(task_id, "routing", "router", routing.reason,
                          routing.model_dump(mode="json"), BLACKBOARD_VERSION)

    def save_context_summary(self, task_id: str, summary: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET context_summary=?, updated_at=? WHERE task_id=?",
                (summary, _utcnow_iso(), task_id),
            )
            self._conn.commit()

    def add_proposal(self, task_id: str, proposal: Proposal) -> None:
        """Сохранить ответ одного Proposer вместе с его вызовами инструментов."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO proposals (task_id, proposer_id, model, provider, answer,"
                " ok, error, latency_ms, payload_json, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, proposal.proposer_id, proposal.model, proposal.provider,
                 proposal.answer, int(proposal.ok), proposal.error, proposal.latency_ms,
                 _dumps(proposal.model_dump(mode="json")), _utcnow_iso()),
            )
            self._conn.commit()
        for result in proposal.tool_results:
            self.log_tool_call(task_id, proposal.proposer_id, result)

    def save_verdict(self, task_id: str, verdict: Verdict) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET verdict_json=?, updated_at=? WHERE task_id=?",
                (_dumps(verdict.model_dump(mode="json")), _utcnow_iso(), task_id),
            )
            self._conn.commit()
        self.log_decision(task_id, "verify", "verifier", verdict.reason,
                          verdict.model_dump(mode="json"), BLACKBOARD_VERSION)

    # ---- оркестрация ---------------------------------------------------

    def save_plan(self, task_id: str, plan: Plan) -> None:
        """Сохранить план шагов (режим оркестратора)."""
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET plan_json=?, updated_at=? WHERE task_id=?",
                (_dumps(plan.model_dump(mode="json")), _utcnow_iso(), task_id),
            )
            self._conn.commit()
        self.log_decision(task_id, "plan", "orchestrator",
                          plan.reasoning or (plan.error or ""),
                          plan.model_dump(mode="json"), BLACKBOARD_VERSION)

    def upsert_plan_step(self, task_id: str, step: PlanStep,
                         status: str = "waiting") -> None:
        """
        Создать или обновить строку шага с его ЖИВЫМ статусом.

        Вызывается на каждом этапе выполнения: сначала 'running' при старте,
        потом 'done'/'failed' по завершении. Благодаря этому панель «Ход
        выполнения» и история видят прогресс даже из другого процесса (CLI).
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT id FROM plan_steps WHERE task_id=? AND step_id=?",
                (task_id, step.step_id)).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO plan_steps (task_id, step_id, title, assignee,"
                    " model, status, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (task_id, step.step_id, step.title, step.assignee,
                     step.model or "", status, _utcnow_iso()),
                )
            else:
                self._conn.execute(
                    "UPDATE plan_steps SET status=?, updated_at=? "
                    "WHERE task_id=? AND step_id=?",
                    (status, _utcnow_iso(), task_id, step.step_id),
                )
            self._conn.commit()

    def add_step_result(self, task_id: str, result: StepResult) -> None:
        """Записать итог одного шага и его вызовы инструментов."""
        with self._lock:
            # attempt: сколько раз этот шаг уже выполнялся (кнопка «повторить»)
            previous = self._conn.execute(
                "SELECT COUNT(*) FROM plan_steps WHERE task_id=? AND step_id=?",
                (task_id, result.step_id)).fetchone()[0]
            # Обновляем существующую строку (она создана upsert_plan_step при старте),
            # а не вставляем вторую: так история не дублирует шаги.
            existing = self._conn.execute(
                "SELECT id FROM plan_steps WHERE task_id=? AND step_id=?",
                (task_id, result.step_id)).fetchone()
            if existing:
                self._conn.execute(
                    "UPDATE plan_steps SET title=?, assignee=?, model=?,"
                    " answer=?, ok=?, error=?, latency_ms=?, status=?,"
                    " warnings_json=?, suspicious=?, attempt=? WHERE task_id=? AND step_id=?",
                    (result.title, result.assignee, result.model,
                     result.answer, int(result.ok), result.error, result.latency_ms,
                     "done" if result.ok else "failed",
                     _dumps(result.warnings), int(result.suspicious),
                     int(previous) + 1, task_id, result.step_id),
                )
            else:
                self._conn.execute(
                    "INSERT INTO plan_steps (task_id, step_id, title, assignee, model,"
                    " answer, ok, error, latency_ms, status, created_at, warnings_json,"
                    " suspicious, attempt)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, result.step_id, result.title, result.assignee,
                     result.model, result.answer, int(result.ok), result.error,
                     result.latency_ms,
                     "done" if result.ok else "failed",
                     _utcnow_iso(), _dumps(result.warnings), int(result.suspicious),
                     int(previous) + 1),
                )
            self._conn.commit()
        for call in result.tool_results:
            self.log_tool_call(task_id, result.assignee or "orchestrator", call)

    def get_step_results(self, task_id: str) -> list[dict[str, Any]]:
        """Шаги задачи в порядке выполнения — для экрана «История»."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT step_id, title, assignee, model, answer, ok, error, latency_ms,"
                " warnings_json, suspicious, attempt, status"
                " FROM plan_steps WHERE task_id=? ORDER BY id", (task_id,)
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["warnings"] = _loads(item.pop("warnings_json", None)) or []
            out.append(item)
        return out

    def save_final(self, task_id: str, final: FinalDecision) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET final_json=?, status='done', used_cloud=?, updated_at=?"
                " WHERE task_id=?",
                (_dumps(final.model_dump(mode="json")), int(final.used_cloud),
                 _utcnow_iso(), task_id),
            )
            self._conn.commit()
        self.log_decision(task_id, "final", final.decided_by, final.rationale,
                          final.model_dump(mode="json"), BLACKBOARD_VERSION)

    # ---- журналы --------------------------------------------------------

    def log_tool_call(self, task_id: str, caller: str, result: ToolCallResult,
                      args: dict[str, Any] | None = None) -> None:
        """Журнал вызовов инструментов — виден на экране «Ход выполнения»."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO tool_calls_log (task_id, caller, tool, action, tool_version,"
                " ok, args_json, summary, error, duration_ms, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (task_id, caller, result.tool, result.action, result.tool_version,
                 int(result.ok), _dumps(args or {}), result.summary, result.error,
                 result.duration_ms, _utcnow_iso()),
            )
            self._conn.commit()

    def log_llm_call(self, model: str, provider: str, ok: bool,
                     latency_ms: int, task_id: str = "",
                     purpose: str = "", eval_count: int = 0,
                     eval_duration_ms: int = 0, prompt_eval_count: int = 0) -> None:
        """Журнал LLM-вызовов: latency + eval-статистика для tps по моделям."""
        tps = 0.0
        if eval_count and eval_duration_ms:
            tps = round(eval_count / (eval_duration_ms / 1000), 1)
        try:
            with self._lock:
                self._conn.execute(
                    "INSERT INTO llm_calls (task_id, model, provider, purpose, ok,"
                    " latency_ms, eval_count, eval_duration_ms, prompt_eval_count,"
                    " tps, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (task_id, model, provider, purpose, int(ok), latency_ms,
                     eval_count, eval_duration_ms, prompt_eval_count, tps,
                     datetime.now(timezone.utc).isoformat()))
                self._conn.commit()
        except Exception:  # noqa: BLE001 - лог не должен ронять задачу
            pass

    def llm_stats(self) -> list[dict[str, Any]]:
        """Агрегат по llm_calls за 14 дней: n, avg latency, avg tps по моделям."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT model, COUNT(*) n, AVG(latency_ms) ms, AVG(tps) tps "
                "FROM llm_calls WHERE ok=1 AND created_at >= "
                "datetime('now', '-14 days') GROUP BY model ORDER BY n DESC LIMIT 8"
            ).fetchall()
        return [dict(r) for r in rows]

    def log_decision(self, task_id: str, stage: str, actor: str, summary: str,
                     details: dict[str, Any] | None = None,
                     component_version: str = "") -> None:
        """Журнал решений: кто, на каком этапе и почему так решил."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO decisions_log (task_id, stage, actor, summary, details_json,"
                " created_at, component_version) VALUES (?,?,?,?,?,?,?)",
                (task_id, stage, actor, summary or "", _dumps(details or {}),
                 _utcnow_iso(), component_version),
            )
            self._conn.commit()

    def set_trace_id(self, task_id: str, trace_id: str) -> None:
        """Связать задачу с трейсом Langfuse."""
        with self._lock:
            self._conn.execute("UPDATE tasks SET trace_id=? WHERE task_id=?", (trace_id, task_id))
            self._conn.commit()

    # ---- чтение ---------------------------------------------------------

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["routing"] = _loads(data.pop("routing_json", None))
        data["verdict"] = _loads(data.pop("verdict_json", None))
        data["final"] = _loads(data.pop("final_json", None))
        data["plan"] = _loads(data.pop("plan_json", None))
        data["metadata"] = _loads(data.pop("metadata_json", None)) or {}
        return data

    def get_proposals(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM proposals WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["payload"] = _loads(item.pop("payload_json", None)) or {}
            item["ok"] = bool(item["ok"])
            out.append(item)
        return out

    def get_tool_calls(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tool_calls_log WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [dict(r) | {"ok": bool(r["ok"]), "args": _loads(r["args_json"]) or {}} for r in rows]

    def get_decisions(self, task_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM decisions_log WHERE task_id=? ORDER BY id", (task_id,)).fetchall()
        return [dict(r) | {"details": _loads(r["details_json"]) or {}} for r in rows]

    def list_tasks(self, limit: int = 50, offset: int = 0,
                   status: str | None = None) -> list[dict[str, Any]]:
        """История задач для соответствующего экрана UI."""
        query = "SELECT task_id, prompt, mode, status, created_at, updated_at, used_cloud FROM tasks"
        params: list[Any] = []
        if status:
            query += " WHERE status=?"
            params.append(status)
        query += " ORDER BY datetime(created_at) DESC LIMIT ? OFFSET ?"
        params += [limit, offset]
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def stats(self) -> dict[str, Any]:
        """Агрегаты для «Панели управления» (COUNT/GROUP BY, не выгрузка строк)."""
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]
            by_status = {
                r["status"]: r["c"] for r in
                self._conn.execute("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status")
            }
            cloud = self._conn.execute(
                "SELECT COUNT(*) AS c FROM tasks WHERE used_cloud=1").fetchone()["c"]
            tools = self._conn.execute(
                "SELECT tool, COUNT(*) AS c, SUM(ok) AS ok_count FROM tool_calls_log"
                " GROUP BY tool").fetchall()
        return {
            "tasks_total": total,
            "by_status": by_status,
            "cloud_tasks": cloud,
            "tool_usage": [
                {"tool": r["tool"], "calls": r["c"], "ok": r["ok_count"] or 0} for r in tools
            ],
            "schema_version": self.schema_version,
        }

    def load_record(self, task_id: str) -> TaskRecord | None:
        """Собрать полную типизированную запись задачи (Pydantic)."""
        raw = self.get_task(task_id)
        if raw is None:
            return None
        task = Task(
            task_id=raw["task_id"], prompt=raw["prompt"], mode=raw["mode"],
            metadata=raw.get("metadata") or {},
        )
        proposals = []
        for item in self.get_proposals(task_id):
            payload = item.get("payload") or {}
            try:
                proposals.append(Proposal(**payload))
            except Exception:  # noqa: BLE001 — старый формат не должен ломать чтение
                proposals.append(Proposal(
                    proposer_id=item["proposer_id"], model=item["model"],
                    provider=item["provider"], answer=item.get("answer") or "",
                    ok=item["ok"], error=item.get("error"),
                    latency_ms=item.get("latency_ms") or 0,
                ))
        steps: list[StepResult] = []
        for row in self.get_step_results(task_id):
            steps.append(StepResult(
                step_id=row["step_id"], title=row["title"] or "",
                assignee=row["assignee"] or "", model=row["model"] or "",
                answer=row["answer"] or "", ok=bool(row["ok"]),
                error=row.get("error"), latency_ms=row.get("latency_ms") or 0,
                warnings=row.get("warnings") or [],
                suspicious=bool(row.get("suspicious")),
            ))
        return TaskRecord(
            task=task,
            routing=RoutingDecision(**raw["routing"]) if raw.get("routing") else None,
            context_summary=raw.get("context_summary") or "",
            proposals=proposals,
            verdict=Verdict(**raw["verdict"]) if raw.get("verdict") else None,
            final=FinalDecision(**raw["final"]) if raw.get("final") else None,
            plan=Plan(**raw["plan"]) if raw.get("plan") else None,
            step_results=steps,
            status=raw.get("status") or "created",
            error=raw.get("error"),
        )

    # ---- сводка контекста для Proposers --------------------------------

    def build_context_summary(self, task_id: str, target_tokens: int = 400) -> str:
        """
        Детерминированная сводка контекста для system prompt Proposers.

        Собирается КОДОМ из фактов в БД (не генерируется LLM), поэтому не
        содержит выдумок. Ограничение ~target_tokens: для смеси русского и
        английского берём консервативно ~3 символа на токен.
        """
        budget_chars = max(300, target_tokens * 3)
        task = self.get_task(task_id)
        if task is None:
            return ""

        lines: list[str] = ["КОНТЕКСТ ЗАДАЧИ (сформирован системой, факты проверены):"]
        lines.append(f"- Режим выполнения: {task['mode']}")

        routing = task.get("routing") or {}
        if routing:
            lines.append(
                f"- Классификация: {routing.get('complexity', '—')}; "
                f"инструменты: {'да' if routing.get('needs_tools') else 'нет'}"
            )

        tool_calls = self.get_tool_calls(task_id)
        if tool_calls:
            lines.append(f"- Выполнено вызовов инструментов: {len(tool_calls)}")
            # Показываем последние успешные результаты: именно они несут факты
            successful = [c for c in tool_calls if c["ok"]][-5:]
            for call in successful:
                lines.append(f"  • {call['tool']}.{call['action']}: {call['summary']}")
            failed = [c for c in tool_calls if not c["ok"]][-2:]
            for call in failed:
                error = (call.get("error") or "")[:160]
                lines.append(f"  • ОШИБКА {call['tool']}.{call['action']}: {error}")

        proposals = self.get_proposals(task_id)
        if proposals:
            lines.append(f"- Предыдущих ответов моделей: {len(proposals)}")

        decisions = self.get_decisions(task_id)
        if decisions:
            last = decisions[-1]
            lines.append(f"- Последний этап: {last['stage']} ({last['actor']}): "
                         f"{(last['summary'] or '')[:120]}")

        summary = "\n".join(lines)
        if len(summary) > budget_chars:
            summary = summary[:budget_chars - 3].rstrip() + "..."
        return summary

    def refresh_context_summary(self, task_id: str, target_tokens: int = 400) -> str:
        """Пересобрать сводку и сохранить её в задаче."""
        summary = self.build_context_summary(task_id, target_tokens)
        self.save_context_summary(task_id, summary)
        return summary
