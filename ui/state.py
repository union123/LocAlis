"""
Общее состояние UI: единственный экземпляр платформы + журнал текущей задачи.

Отделено от app.py, чтобы новые экраны (ui/pages/*.py) могли пользоваться
тем же состоянием, не импортируя главный модуль (без циклических зависимостей).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable

UI_STATE_VERSION = "1.0"


@dataclass
class RunLog:
    """Журнал одного выполнения — источник данных для экрана «Ход выполнения»."""

    task_id: str = ""
    prompt: str = ""
    mode: str = "auto"
    placement: str = ""          # где работает планировщик (режим оркестратора)
    running: bool = False
    finished: bool = False
    events: list[dict[str, Any]] = field(default_factory=list)
    current_node: str = ""
    proposals: list[dict[str, Any]] = field(default_factory=list)
    # Режим оркестратора: план и живое состояние каждого шага.
    # Состояние обновляется из событий, а не по завершении: иначе
    # пользователь минутами смотрел бы на пустой экран.
    plan_steps: list[dict[str, Any]] = field(default_factory=list)
    plan_reasoning: str = ""
    plan_model: str = ""
    plan_error: str = ""
    final_answer: str = ""
    final_meta: str = ""
    error: str = ""

    def add(self, event: dict[str, Any]) -> None:
        self.events.append(event)
        kind = event.get("kind")
        if kind == "node_start":
            self.current_node = str(event.get("node", ""))
            if event.get("placement"):
                self.placement = str(event["placement"])
        elif kind == "meeting_round":
            self.current_node = (f"Совещание, раунд {event.get('round', '?')}"
                                 f" из {event.get('of', '?')}")
        elif kind == "meeting_speech":
            self.current_node = (f"Совещание: {event.get('speaker', '?')} "
                                 f"высказался")
        elif kind == "meeting_done":
            self.current_node = "Совещание: консенсус достигнут"
            self.plan_reasoning = str(event.get("consensus_preview") or "")
        elif kind == "cross_review_done":
            self.current_node = ("Крест-накрест: " +
                                 ("чисто" if event.get("clean")
                                  else f"{event.get('issues', '?')} проблем"))
        elif kind == "checkpoint":
            self.current_node = f"Итерация {event.get('iteration', '?')}"
        elif kind == "lead_fallback":
            self.current_node = (f"Fallback: {event.get('from','?')} "
                                 f"недоступна")
        elif kind == "knowledge_context":
            self.current_node = (f"База знаний: {event.get('chunks', 0)} "
                                 f"релевантных чанков")
        elif kind == "knowledge_enriched":
            self.current_node = "Итог добавлен в базу знаний"
        elif kind == "task_end":
            self.current_node = "готово"
        elif kind == "plan":
            self.plan_reasoning = str(event.get("reasoning") or "")
            self.plan_model = str(event.get("model") or "")
            self.plan_error = str(event.get("error") or "")
            self.plan_steps = [
                {**step, "state": "waiting", "answer": "", "ms": 0, "error": ""}
                for step in (event.get("steps") or [])
            ]
        elif kind == "step_start":
            self._update_step(event.get("step_id"), state="running",
                              model=event.get("model", ""))
        elif kind == "step_end":
            self._update_step(
                event.get("step_id"),
                state="done" if event.get("ok") else "failed",
                answer=str(event.get("answer_preview") or ""),
                ms=int(event.get("ms") or 0),
                error=str(event.get("error") or ""))

    def _update_step(self, step_id: Any, **changes: Any) -> None:
        """Обновить шаг по идентификатору; неизвестные шаги игнорируем."""
        if not step_id:
            return
        for step in self.plan_steps:
            if step.get("id") == step_id:
                step.update(changes)
                return


class UIState:
    """Синглтон состояния интерфейса."""

    def __init__(self) -> None:
        self._platform: Any = None
        self.log = RunLog()
        self.lock = threading.Lock()
        self.listeners: list[Callable[[], None]] = []
        # Событие аварийной остановки выполняющейся задачи (24.08).
        # Worker-поток проверяет его в цикле; инструменты не прерываются
        # посреди вызова, но следующий ход модели не начинается.
        import threading as _t
        self.stop_event = _t.Event()

    # ---- платформа -------------------------------------------------------

    @property
    def platform(self):
        """Ленивая инициализация: UI открывается мгновенно, модели грузятся потом."""
        if self._platform is None:
            from workflows.main_graph import Platform
            self._platform = Platform(on_event=self.on_event)
        return self._platform

    def reload_platform(self) -> None:
        """Перечитать конфиги и пересобрать платформу (после смены настроек)."""
        if self._platform is not None:
            try:
                self._platform.blackboard.close()
            except Exception:  # noqa: BLE001
                pass
        self._platform = None

    # ---- события ---------------------------------------------------------

    def on_event(self, event: dict[str, Any]) -> None:
        with self.lock:
            self.log.add(event)

    def start_run(self, prompt: str, mode: str, placement: str = "") -> RunLog:
        with self.lock:
            self.log = RunLog(prompt=prompt, mode=mode, placement=placement, running=True)
        self.stop_event.clear()
        return self.log

    def request_stop(self) -> None:
        """Пользователь нажал «Стоп»: пишем флаг-файл, который шлюз проверяет
        перед КАЖДЫМ вызовом модели (в т.ч. из worker-потока и CLI-прогонов).
        Текущий вызов модели доиграет, следующий не начнётся."""
        self.stop_event.set()
        try:
            from pathlib import Path
            flag = Path(__file__).resolve().parent.parent / "data" / "stop_flag"
            flag.parent.mkdir(parents=True, exist_ok=True)
            flag.write_text("stop", encoding="utf-8")
        except OSError:
            pass

    def snapshot(self) -> RunLog:
        with self.lock:
            return self.log


STATE = UIState()
