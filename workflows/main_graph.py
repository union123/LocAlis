"""
Главный workflow (LangGraph).

Router -> Secretary -> [Retrieval] -> [Tools] -> Proposers (параллельно, 3 модели)
       -> Verifier (2 из 3) -> [Judge: облако с суб-агентами -> локальный fallback]
       -> Secretary (запись результата)

Узлы включаются/выключаются ФИЧА-ФЛАГАМИ из config/settings.yaml —
без правки кода. Отключённый узел просто пропускается, граф пересобирается.

Если langgraph не установлен, используется встроенный последовательный
исполнитель с тем же порядком узлов (платформа не должна зависеть
от необязательной библиотеки).
"""

from __future__ import annotations

import concurrent.futures
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypedDict

from agents.judge import Judge
from agents.orchestrator import Orchestrator
from agents.proposer import Proposer
from agents.verifier import Verifier
from config.loader import feature_enabled, load_models, load_routing_rules, load_settings, resolve_path
from config.resilience import ModeController
from core.blackboard import Blackboard
from core.llm_gateway import LLMGateway
from core.memory import PersistentMemory
from core.router import Router
from core.step_check import check_step
from core.schemas import (
    ExecutionMode,
    FinalDecision,
    OrchestratorPlacement,
    Plan,
    Proposal,
    RoutingDecision,
    StepResult,
    Task,
    TaskComplexity,
    Verdict,
)
from core.tool_context import ScratchMemory, build_context
from core.tool_registry import ToolRegistry

GRAPH_VERSION = "1.0"


def _apply_autocheck_verdict(candidate: StepResult, max_attempts: int) -> StepResult:
    """Итоговое решение по автопроверке шага.

    core.step_check.check_step намеренно не трогает candidate.ok/status —
    оно только выставляет suspicious/warnings и оставляет решение
    вызывающей стороне. Вызывать эту функцию надо только когда шаг
    подозрителен и попытки уже исчерпаны: раньше такой шаг всё равно
    уходил как ok=True/status="done", и сборщик итога принимал догадку
    модели за факт (наблюдалось на задаче 5d3928f13036, шаги 7–8: write_excel/
    write_geojson не вызваны/сломаны, а итог всё равно отрапортовал выдуманное
    объяснение успеха).
    """
    candidate.ok = False
    candidate.error = ("Автопроверка отклонила результат после "
                       f"{max_attempts} попыток: " + "; ".join(candidate.warnings))
    return candidate


class GraphState(TypedDict, total=False):
    """Состояние графа. Все поля опциональны — узлы дописывают своё."""

    task: Task
    routing: RoutingDecision
    context_summary: str
    retrieval_context: str
    proposals: list[Proposal]
    verdict: Verdict
    final: FinalDecision
    plan: Plan
    step_results: list[StepResult]
    events: list[dict[str, Any]]
    error: str


@dataclass
class Platform:
    """
    Сборка всех компонентов платформы.

    Создаётся один раз (в UI или CLI) и переиспользуется между задачами.
    """

    settings: dict[str, Any] = field(default_factory=load_settings)
    models: dict[str, Any] = field(default_factory=load_models)
    routing_rules: dict[str, Any] = field(default_factory=load_routing_rules)
    on_event: Callable[[dict[str, Any]], None] | None = None

    mode_controller: ModeController = field(init=False)
    gateway: LLMGateway = field(init=False)
    registry: ToolRegistry = field(init=False)
    blackboard: Blackboard = field(init=False)
    router: Router = field(init=False)
    persistent_memory: PersistentMemory = field(init=False)

    def __post_init__(self) -> None:
        self.mode_controller = ModeController.from_config(self.settings, self.models)
        self.gateway = LLMGateway(self.models, self.mode_controller)
        # События арбитра VRAM (выгрузки, слоты) идут в общий поток панели
        self.gateway.set_event_sink(self.emit)
        self.registry = ToolRegistry()
        self.registry.discover()
        db_path = resolve_path((self.settings.get("paths") or {}).get(
            "blackboard_db", "data/blackboard.sqlite3"))
        self.blackboard = Blackboard(db_path)
        # Задачи, застрявшие в running после рестарта панели, — пометить failed.
        # Иначе они висят вечно (e1624df7 / bd2e37eb / 28fde479).
        try:
            orphans = self.blackboard.fail_orphaned_running_tasks(grace_minutes=10)
            if orphans:
                self.emit({"kind": "orphan_tasks_cleaned", "count": len(orphans)})
        except Exception:  # noqa: BLE001
            pass
        # Долговечная память (профиль + факты + прошлые решения)
        mem_path = resolve_path((self.settings.get("paths") or {}).get(
            "memory_db", "data/memory.sqlite3"))
        self.persistent_memory = PersistentMemory(mem_path)
        # Рабочая память для инструментов-агентов (общая на задачу)
        self.memory = ScratchMemory()
        self._current_task_id: str = ""
        # Метаданные текущей задачи — источник для get_allowed_roots(),
        # чтобы инструменты файловой системы могли жёстко ограничить задачу
        # её собственной папкой проекта, даже если модель проигнорирует запрет
        # в промпте (см. GET_ALLOWED_ROOTS в core/tool_context.py).
        self._current_task_metadata: dict[str, Any] = {}
        # Контекст раздаём ПОСЛЕ сборки шлюза: иначе была бы круговая
        # зависимость реестр <-> шлюз
        self.refresh_tool_context()
        self.router = Router(self.routing_rules, self.gateway)

    # ---- вспомогательное -------------------------------------------------

    def rebind_gateway(self, gateway: Any) -> None:
        """
        Заменить шлюз моделей (используется в тестах и при смене настроек).

        Router держит ссылку на шлюз, поэтому его нужно пересоздать —
        иначе он продолжит обращаться к прежнему шлюзу.
        """
        self.gateway = gateway
        self.router = Router(self.routing_rules, gateway)
        # Инструменты-агенты должны увидеть новый шлюз, а не старый
        self.refresh_tool_context()

    def flag(self, name: str, default: bool = True) -> bool:
        return feature_enabled(self.settings, name, default)

    def limit(self, name: str, default: int) -> int:
        return int((self.settings.get("limits") or {}).get(name, default))

    def emit(self, event: dict[str, Any]) -> None:
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                pass

    def is_sensitive(self, task: Task) -> bool:
        """Есть ли в тексте задачи маркеры конфиденциальности."""
        privacy = self.settings.get("privacy") or {}
        if not privacy.get("force_local_for_sensitive", True):
            return False
        lowered = task.prompt.lower()
        return any(str(marker).lower() in lowered
                   for marker in privacy.get("sensitive_markers") or [])

    def effective_mode(self, task: Task) -> str:
        """
        Итоговый режим с учётом политики конфиденциальности.
        Маркеры чувствительности в тексте задачи понижают режим до local_only.

        Режим orchestrated — об АРХИТЕКТУРЕ выполнения, а не о доступе к сети.
        Для контроля облака он трактуется как auto (или local_only при маркерах),
        а где именно работает планировщик, решает orchestrator_placement.
        """
        if self.is_sensitive(task):
            return "local_only"
        requested = task.mode.value
        if requested == ExecutionMode.ORCHESTRATED.value:
            requested = ExecutionMode.AUTO.value
        return self.mode_controller.effective_mode(requested)

    def orchestrator_placement(self, task: Task) -> OrchestratorPlacement:
        """
        Где разместить планировщика.

        Приоритет: маркеры конфиденциальности -> выбор в самой задаче ->
        настройки. Конфиденциальность выше явного выбора облака: текст такой
        задачи не должен покидать компьютер даже по прямому указанию.
        """
        if self.is_sensitive(task):
            return OrchestratorPlacement.LOCAL
        requested = task.metadata.get("orchestrator_placement")
        if not requested:
            requested = (self.settings.get("orchestrator") or {}).get("placement", "auto")
        try:
            return OrchestratorPlacement(str(requested))
        except ValueError:
            return OrchestratorPlacement.AUTO

    def orchestrator_planner_model(self, task: Task) -> str | None:
        """Явный выбор модели планировщика через metadata задачи."""
        return task.metadata.get("orchestrator_planner_model")

    # ---- контекст для инструментов-агентов --------------------------------

    # Инструменты, которые НЕ выдаются под-агентам: иначе агент вызовет
    # агента, тот — снова агента, и на 8 ГБ VRAM это встанет насмерть.
    # vision добавлен по той же причине предосторожности по VRAM: он сам
    # загружает в Ollama отдельную мультимодальную модель, и разрешать
    # под-агентам дёргать его без контроля рискует конкуренцией за VRAM
    # с уже работающей основной моделью. websearch НЕ в списке: это чистый
    # внешний HTTP (DuckDuckGo/страницы), VRAM не трогает.
    AGENT_TOOLS_BLOCKLIST = ("local_agents", "vision")

    def refresh_tool_context(self) -> None:
        """Собрать и раздать инструментам возможности платформы."""
        context = build_context(
            ask_local_model=self._cap_ask_local_model,
            list_local_models=self._cap_list_local_models,
            run_local_agent=self._cap_run_local_agent,
            search_knowledge=self._cap_search_knowledge,
            memory=self.memory,
            task_id=lambda: self._current_task_id,
            emit_event=self.emit,
            persistent_memory=self.persistent_memory,
            get_task_history=self._cap_get_task_history,
            get_allowed_roots=lambda: (self._current_task_metadata or {}).get("allowed_roots"),
        )
        self.registry.set_context(context)

    def _local_specs(self) -> list[Any]:
        """Только локальные модели: под-агенты не должны тратить облачные токены.
        llamacpp (llama-server на localhost) тоже локальная — Ornith-1.5 и Coder-Next."""
        specs = self.gateway.proposer_specs(include_cloud=False)
        return [s for s in specs
                if s.provider in ("ollama", "stub", "llamacpp")]

    def _cap_list_local_models(self) -> list[dict[str, Any]]:
        # Когнитивные стили моделей (замерено тестами на этой машине).
        # Планировщик использует их, чтобы ставить модель под подходящую задачу.
        purpose = {
            "proposer_a": "Gemma4-26B: интерпретативный, эмпатийный стиль, "
                          "улавливает неоднозначность полевых данных; анализ "
                          "смысла/подтекста, несколько вариантов с self-correction",
            "proposer_b": "Ornith-1.5-35B-A3B: сильный агентный кодер (Terminal-Bench "
                          "67.8), 262K контекст; строгий инструкшн-фолловинг, "
                          "процедурные задачи. Считает себя Claude — игнорировать.",
            "proposer_c": "gpt-oss-20B: линейный минималистичный поток, "
                          "максимальная скорость, простые однозначные задачи, "
                          "быстрый вызов инструментов",
        }
        return [
            {"id": spec.id, "model": spec.model,
             "supports_tools": bool(getattr(spec, "supports_tools", True)),
             "purpose": purpose.get(spec.id, "локальная модель")}
            for spec in self._local_specs()
        ]

    def _spec_by_id(self, agent_id: str) -> Any | None:
        specs = self._local_specs()
        if not specs:
            return None
        for spec in specs:
            if spec.id == agent_id:
                return spec
        return specs[0]

    def _cap_ask_local_model(self, model_id: str, prompt: str, system: str = "",
                             temperature: float | None = None) -> dict[str, Any]:
        """Один вызов локальной модели без инструментов — быстро и безопасно."""
        spec = self._spec_by_id(model_id)
        if spec is None:
            return {"ok": False, "error": "Нет доступных локальных моделей"}
        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        # task_mode=local_only: под-агент физически не может уйти в облако
        response = self.gateway.chat(spec, messages, tools=None, task_mode="local_only")
        if not response.ok:
            return {"ok": False, "error": response.error, "model": spec.model}
        return {"ok": True, "text": response.text, "model": spec.model,
                "agent_id": spec.id, "latency_ms": response.latency_ms}

    def _cap_run_local_agent(self, model_id: str, task: str,
                             allowed_tools: list[str] | None = None) -> dict[str, Any]:
        """Под-агент с доступом к инструментам платформы (кроме агентских)."""
        from agents.proposer import Proposer

        spec = self._spec_by_id(model_id)
        if spec is None:
            return {"ok": False, "error": "Нет доступных локальных моделей"}

        registry = self.registry if self.flag("tools") else None
        allowed = allowed_tools
        if registry is not None:
            usable = [e.name for e in registry.available()
                      if e.name not in self.AGENT_TOOLS_BLOCKLIST]
            if allowed:
                allowed = [t for t in allowed if t in usable]
                if not allowed:
                    return {"ok": False,
                            "error": f"Запрошенные инструменты недоступны. Доступны: {usable}"}
            else:
                allowed = usable

        proposer = Proposer(spec, self.gateway, registry,
                            self.limit("max_tool_iterations", 3), on_event=self.emit)
        result = proposer.run(task, "", allowed, "local_only")
        if not result.ok:
            return {"ok": False, "error": result.error, "model": spec.model}
        return {
            "ok": True, "text": result.answer, "model": spec.model, "agent_id": spec.id,
            "tool_facts": [f"{r.tool}.{r.action}: {r.summary or r.error}"
                           for r in result.tool_results],
        }

    def _cap_search_knowledge(self, query: str, top_k: int = 5) -> list[dict[str, Any]]:
        try:
            return self.retrieval_agent().search(query, top_k=top_k)
        except Exception:  # noqa: BLE001 — поиск необязателен для ответа
            return []

    def _cap_get_task_history(self, limit: int = 15,
                              status: str | None = None) -> dict[str, Any]:
        """
        Сводка реального журнала задач из Blackboard.

        Позволяет агенту анализировать настоящую историю выполнения (в т.ч.
        сбойные прогоны и причины), а не искать несуществующий файл .log.
        Возвращает: список задач со статусом/ошибкой + вызовы инструментов
        сбойных задач для поиска повторяющихся проблем.
        """
        try:
            tasks = self.blackboard.list_tasks(limit=limit, status=status)
            result: dict[str, Any] = {"tasks": tasks, "tool_calls": []}
            # Для сбойных задач подтягиваем вызовы инструментов — там видны причины
            failed_ids = [t["task_id"] for t in tasks if t.get("status") in ("failed",)]
            for tid in failed_ids[:5]:
                calls = self.blackboard.get_tool_calls(tid)
                n_err = sum(1 for c in calls if not c.get("ok"))
                if n_err:
                    result["tool_calls"].append({
                        "task_id": tid,
                        "errors": n_err,
                        "total": len(calls),
                        "sample": [{"tool": c.get("tool"), "action": c.get("action"),
                                    "ok": c.get("ok"),
                                    "text": (c.get("error") or c.get("summary") or "")[:120]}
                                   for c in calls[-3:]],
                    })
            return result
        except Exception as exc:  # noqa: BLE001
            return {"error": str(exc), "tasks": []}

    def retrieval_agent(self):
        """Ленивая инициализация — эмбеддинги нужны не каждой задаче."""
        from core.retrieval import RetrievalAgent
        index_dir = resolve_path((self.settings.get("paths") or {}).get(
            "vector_index", "data/index"))
        dim = int((self.models.get("embeddings") or {}).get("dim", 1024))
        return RetrievalAgent(self.gateway, index_dir, dim=dim)




def state_context_summary(platform: Any, task: Task) -> str:
    """Сводка контекста задачи из Blackboard (для совещания команды)."""
    try:
        rec = platform.blackboard.load_record(task.task_id)
        return (rec.context_summary or "") if rec else ""
    except Exception:  # noqa: BLE001
        return ""


def orchestrator_adversarial(gateway: Any, task_prompt: str,
                             final_answer: str, review_fn: Any) -> dict[str, Any]:
    """Adversarial-ревью (делегирует в Orchestrator.adversarial_review)."""
    from agents.orchestrator import Orchestrator
    o = object.__new__(Orchestrator)
    return o.adversarial_review(task_prompt, final_answer, review_fn)


class Workflow:
    """Узлы графа + сборка. Каждый узел — чистая функция состояния."""

    version = GRAPH_VERSION

    def __init__(self, platform: Platform):
        self.p = platform

    # ---- план узлов и сборка (LangGraph-совместимо) -----------------------

    def node_plan(self, task: Task | None = None) -> list[tuple[str, Callable[[GraphState], GraphState]]]:
        """
        Детерминированный план узлов в порядке выполнения.

        Возвращает [(имя, функция_узла), ...]. Планировщик (последовательный
        или LangGraph) идёт по этому списку, уважая фича-флаги:
        - router: выключен -> полный цикл (узел всё равно нужен для записи)
        - retrieval: только при флаге (добавляется ДО секретаря, чтобы
          результаты поиска попали в сводку контекста)
        - proposers: выключен -> узел убирается (роутер поднимает флаг)
        - verifier/judge: выключены -> узлы остаются, но пропускают работу

        Если задан task в режиме ORCHESTRATED — proposers/verifier/judge
        заменяются на оркестратор (шаги решают РАЗНЫЕ подзадачи, голосование
        не имеет смысла).
        """
        plan: list[tuple[str, Callable[[GraphState], GraphState]]] = [
            ("router", self.node_router),
            ("secretary", self.node_secretary),
            ("proposers", self.node_proposers),
            ("verifier", self.node_verifier),
            ("judge", self.node_judge),
            ("secretary_persist", self.node_secretary_persist),
        ]
        # Режим оркестратора: голосование не нужно, шаги решают разные подзадачи
        if task is not None and task.mode == ExecutionMode.ORCHESTRATED:
            plan = [(n, f) for n, f in plan
                    if n not in ("proposers", "verifier", "judge")]
            plan.insert(2, ("orchestrator", self.node_orchestrator))
        if self.p.flag("retrieval", False):
            plan.insert(1, ("retrieval", self.node_retrieval))
        if not self.p.flag("proposers"):
            plan = [(n, f) for n, f in plan if n != "proposers"]
        return plan

    def node_secretary_persist(self, state: GraphState) -> GraphState:
        """Терминальный узел: финальная запись результата в Blackboard и память."""
        task = state["task"]
        final = state.get("final")
        if not final:
            return state
        try:
            self.p.blackboard.save_final(task.task_id, final)
            self._remember_past_result(task, final)
        except Exception:  # noqa: BLE001
            pass
        return state

    def _remember_past_result(self, task: Task, final: FinalDecision) -> None:
        """Сохранить результат задачи в долговечную память."""
        result_path = ""
        try:
            calls = self.p.blackboard.get_tool_calls(task.task_id)
            for c in calls:
                s = (c.get("summary") or "") + (c.get("error") or "")
                for mark in ("C:", "D:", "E:", "F:"):
                    idx = s.find(mark)
                    if idx != -1:
                        result_path = s[idx:idx + 120].split(" ")[0]
                        break
                if result_path:
                    break
        except Exception:  # noqa: BLE001
            pass
        try:
            self.p.persistent_memory.save_past_result(
                task_id=task.task_id,
                prompt=task.prompt,
                answer=final.answer or "",
                rationale=final.rationale or "",
                mode=task.mode.value,
                decided_by=final.decided_by,
                model=final.model,
                result_path=result_path,
            )
        except Exception:  # noqa: BLE001
            pass

        # Автопополнение базы знаний (25.08): итог задачи — тоже знание.
        # Только для содержательных ответов (>200 символов), чтобы не мусорить.
        if final.answer and len(final.answer) > 200:
            try:
                from core.knowledge import KnowledgeBase
                kb_path = resolve_path((self.p.settings.get("paths") or {}).get(
                    "knowledge_db", "data/knowledge.sqlite3"))
                kb = KnowledgeBase(kb_path)
                kb.add_document(
                    f"task://{task.task_id}",
                    f"ЗАДАЧА: {task.prompt[:1500]}\n\nИТОГ: {final.answer[:6000]}",
                    title=f"Задача {task.task_id[:8]}: {task.prompt[:60]}",
                    doc_type="task", tags="задача,авто")
                kb.close()
                self.p.emit({"kind": "knowledge_enriched",
                             "task_id": task.task_id[:8]})
            except Exception:  # noqa: BLE001 — база недоступна, не критично
                pass

    def build_langgraph(self):
        """
        Собрать LangGraph-граф из node_plan (если langgraph установлен).

        Возвращает скомпилированный граф; при отсутствии langgraph возвращает
        None — платформа работает на встроенном последовательном исполнителе.
        """
        try:
            from langgraph.graph import StateGraph
        except ImportError:
            return None
        builder = StateGraph(GraphState)
        for name, fn in self.node_plan():
            builder.add_node(name, fn)
        # Последовательный проход по плану
        names = [n for n, _ in self.node_plan()]
        for i in range(len(names) - 1):
            builder.add_edge(names[i], names[i + 1])
        builder.set_entry_point(names[0])
        builder.set_finish_point(names[-1])
        return builder.compile()

    # ---- узлы ------------------------------------------------------------

    def node_router(self, state: GraphState) -> GraphState:
        task = state["task"]
        self.p.emit({"kind": "node_start", "node": "router"})
        if not self.p.flag("router"):
            routing = RoutingDecision(
                complexity=TaskComplexity.NEEDS_VERIFICATION, needs_tools=True,
                needs_full_debate=True, reason="Router отключён флагом — полный цикл",
                decided_by="fallback")
        else:
            routing = self.p.router.route(task)
        self.p.blackboard.save_routing(task.task_id, routing)
        self.p.emit({"kind": "node_end", "node": "router",
                     "complexity": routing.complexity.value,
                     "needs_tools": routing.needs_tools, "reason": routing.reason})
        return {"routing": routing}

    def node_secretary(self, state: GraphState) -> GraphState:
        """Сводка контекста (300-500 токенов) для system prompt Proposers."""
        task = state["task"]
        self.p.emit({"kind": "node_start", "node": "secretary"})
        target = self.p.limit("context_summary_tokens", 400)
        summary = self.p.blackboard.refresh_context_summary(task.task_id, target)
        # Долговечная память: профиль пользователя + факты + похожие решения
        memory_block = self.p.persistent_memory.context_block(query=task.prompt)
        if memory_block:
            summary = f"{memory_block}\n\n{summary}" if summary else memory_block
        # База знаний: релевантные чанки с порогом MIN_CONTEXT_SCORE (25.08).
        # Слабые совпадения не подмешиваем — шум в промпте хуже их отсутствия.
        try:
            from core.knowledge import KnowledgeBase, MIN_CONTEXT_SCORE
            kb_path = resolve_path((self.p.settings.get("paths") or {}).get(
                "knowledge_db", "data/knowledge.sqlite3"))
            kb = KnowledgeBase(kb_path)
            hits = kb.search(task.prompt, top_k=3, mode="hybrid")
            relevant = [h for h in hits if h.get("score", 0) >= MIN_CONTEXT_SCORE]
            kb.close()
            if relevant:
                lines = "\n".join(
                    f"- [{h['title']}] {h['text'][:300]}" for h in relevant)
                block = ("РЕЛЕВАНТНЫЕ ЗНАНИЯ ИЗ БАЗЫ:\n" + lines +
                         "\n(используй при ответе; источник указан в скобках)")
                summary = f"{summary}\n\n{block}" if summary else block
                self.p.emit({"kind": "knowledge_context",
                             "chunks": len(relevant)})
        except Exception:  # noqa: BLE001 — база недоступна не критична
            pass
        extra = state.get("retrieval_context") or ""
        if extra:
            summary = f"{summary}\n\n{extra}"
        self.p.emit({"kind": "node_end", "node": "secretary", "chars": len(summary)})
        return {"context_summary": summary}

    def node_retrieval(self, state: GraphState) -> GraphState:
        routing = state.get("routing")
        if not (self.p.flag("retrieval", False) and routing and routing.needs_retrieval):
            return {}
        self.p.emit({"kind": "node_start", "node": "retrieval"})
        try:
            block = self.p.retrieval_agent().context_block(state["task"].prompt)
        except Exception as exc:  # noqa: BLE001 — поиск не обязателен для ответа
            block = ""
            self.p.emit({"kind": "warning", "node": "retrieval",
                         "message": f"Поиск недоступен: {type(exc).__name__}: {exc}"})
        self.p.emit({"kind": "node_end", "node": "retrieval", "found": bool(block)})
        return {"retrieval_context": block}

    def node_proposers(self, state: GraphState) -> GraphState:
        """Три модели параллельно. Сбой одной не отменяет остальных."""
        task = state["task"]
        routing = state.get("routing")
        mode = self.p.effective_mode(task)
        self.p.emit({"kind": "node_start", "node": "proposers", "mode": mode})

        include_cloud = mode == "auto" and self.p.mode_controller.cloud_available(mode)
        try:
            specs = self.p.gateway.proposer_specs(include_cloud=include_cloud)
        except Exception as exc:  # noqa: BLE001 — сломанный конфиг не должен ронять граф
            return {"proposals": [], "error": f"{type(exc).__name__}: {exc}"}
        if not specs:
            return {"proposals": [], "error": "В конфиге нет активных Proposers"}

        # Простая задача -> достаточно одной модели (экономия времени и VRAM).
        # Облако для простых задач не привлекаем: локальных исполнителей хватает,
        # а вызов облачной модели ради "проверки" — трата денег (ломало тест
        # test_failing_proposer после включения lead_agent).
        if routing and not routing.needs_full_debate:
            specs = specs[:1]
            include_cloud = False

        registry = self.p.registry if (self.p.flag("tools") and routing and routing.needs_tools) else None
        allowed = None
        if routing and routing.suggested_tools:
            allowed = routing.suggested_tools
        if task.allowed_tools is not None:
            allowed = task.allowed_tools

        max_iter = self.p.limit("max_tool_iterations", 3)
        proposers = [
            Proposer(spec, self.p.gateway, registry, max_iter, on_event=self.p.emit)
            for spec in specs
        ]
        proposals: list[Proposal] = []
        # Локальные модели грузятся в VRAM по очереди, но запросы к Ollama
        # можно ставить параллельно — сервер сам сериализует загрузку.
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(proposers)) as pool:
            futures = {
                pool.submit(p.run, task.prompt, state.get("context_summary", ""),
                            allowed, mode): p
                for p in proposers
            }
            for future in concurrent.futures.as_completed(futures):
                proposer = futures[future]
                try:
                    proposals.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    proposals.append(Proposal(
                        proposer_id=proposer.spec.id, model=proposer.spec.model,
                        provider=proposer.spec.provider, ok=False,
                        error=f"{type(exc).__name__}: {exc}"))

        # Порядок как в конфиге — стабильный вывод в UI
        order = {spec.id: i for i, spec in enumerate(specs)}
        proposals.sort(key=lambda p: order.get(p.proposer_id, 99))
        for proposal in proposals:
            self.p.blackboard.add_proposal(task.task_id, proposal)
        self.p.emit({"kind": "node_end", "node": "proposers",
                     "ok": sum(1 for p in proposals if p.ok), "total": len(proposals)})
        return {"proposals": proposals}

    def node_verifier(self, state: GraphState) -> GraphState:
        task = state["task"]
        proposals = state.get("proposals") or []
        if not self.p.flag("verifier"):
            first = next((p for p in proposals if p.ok and p.answer), None)
            verdict = Verdict(consensus=bool(first), chosen_answer=first.answer if first else None,
                              escalate_to_judge=True, reason="Verifier отключён флагом",
                              model="none")
        else:
            self.p.emit({"kind": "node_start", "node": "verifier"})
            threshold = float((self.p.models.get("verifier") or {}).get(
                "similarity_threshold", 0.82))
            verifier = Verifier(self.p.gateway, threshold)
            verdict = verifier.verify(task.prompt, proposals)
        self.p.blackboard.save_verdict(task.task_id, verdict)
        self.p.emit({"kind": "node_end", "node": "verifier",
                     "consensus": verdict.consensus, "agreeing": verdict.agreeing,
                     "reason": verdict.reason})
        return {"verdict": verdict}

    def node_judge(self, state: GraphState) -> GraphState:
        task = state["task"]
        verdict = state.get("verdict")
        proposals = state.get("proposals") or []

        # Консенсус без расхождений -> Judge не нужен (экономим время и VRAM)
        if verdict and verdict.consensus and not verdict.escalate_to_judge:
            final = FinalDecision(
                answer=verdict.chosen_answer or "", rationale=verdict.reason,
                decided_by="verifier_consensus", model=verdict.model, used_cloud=False)
            self.p.emit({"kind": "node_skip", "node": "judge",
                         "reason": "Достигнут консенсус 2 из 3"})
            return {"final": final}

        if not self.p.flag("judge"):
            answer = (verdict.chosen_answer if verdict else "") or next(
                (p.answer for p in proposals if p.ok and p.answer), "")
            return {"final": FinalDecision(
                answer=answer, rationale="Judge отключён флагом",
                decided_by="fallback", model="none")}

        self.p.emit({"kind": "node_start", "node": "judge"})
        mode = self.p.effective_mode(task)
        judge = Judge(
            self.p.gateway,
            registry=self.p.registry if self.p.flag("tools") else None,
            subagent_runner=(self._make_subagent_runner(state)
                             if self.p.flag("judge_subagents") else None),
            allow_cloud=self.p.flag("cloud_judge"),
            allow_subagents=self.p.flag("judge_subagents"),
            on_event=self.p.emit,
        )
        final = judge.decide(
            task.prompt, proposals, verdict,
            context_summary=state.get("context_summary", ""),
            task_mode=mode,
            allowed_tools=task.allowed_tools,
        )
        self.p.emit({"kind": "node_end", "node": "judge",
                     "decided_by": final.decided_by, "used_cloud": final.used_cloud})
        return {"final": final}

    def _make_subagent_runner(self, state: GraphState):
        """Фабрика для запуска суб-агентов из Judge."""
        def runner(model_id: str, prompt: str, tools: list[str] | None = None) -> dict[str, Any]:
            return self.p._cap_run_local_agent(model_id, prompt, tools)
        return runner

    # ---- режим оркестратора ----------------------------------------------

    def node_orchestrator(self, state: GraphState) -> GraphState:
        """
        Планировщик делит задачу на шаги, локальные исполнители их выполняют.

        Голосования здесь нет: шаги решают РАЗНЫЕ подзадачи, сравнивать их
        между собой бессмысленно. Контроль качества обеспечивается тем, что
        факты приходят из инструментов, а сборщик видит все результаты сразу.

        Для SIMPLE задач (routing.complexity == SIMPLE) оркестратор не нужен —
        достаточно одного proposer'а. Это экономит VRAM/время и избегает
        овер-инжиниринга (планы на 3 шага для "придумай название").
        """
        task = state["task"]
        routing = state.get("routing")
        mode = self.p.effective_mode(task)
        placement = self.p.orchestrator_placement(task)
        cloud_ok = self.p.mode_controller.cloud_available(mode) and self.p.flag("cloud_judge")
        self.p.emit({"kind": "node_start", "node": "orchestrator",
                     "placement": placement.value, "cloud_available": cloud_ok})

        # SIMPLE задачи — не нужны планировщику, делаем один шаг через proposer_a
        if routing and routing.complexity == TaskComplexity.SIMPLE:
            self.p.emit({"kind": "node_end", "node": "orchestrator",
                         "reason": "SIMPLE задача — пропускаем оркестратор, запускаем один proposer",
                         "skipped": True})
            return self._run_simple_orchestrated(task, mode, state.get("context_summary", ""))

        # LEAD AGENT режим (24.08): один сильный исполнитель делает задачу
        # целиком с полным контекстом и всеми инструментами. Планировщик LLM
        # не вызывается вовсе. Основание: 5 провалов многошаговой схемы против
        # успеха fallback-одношаговика (задачи b75cdf92/de9dc117/aaa7ac95/2f073f8a
        # против cde8bfe0). Суб-агенты доступны модели через local_agents.
        if self.p.flag("lead_agent", False):
            # Режим «Команда» (24.08): совещание моделей + крест-накрест.
            # Приоритет над одиночным Lead Agent, включается флагом
            # team_mode или раундами совещания в metadata задачи.
            team_rounds = int((task.metadata or {}).get("meeting_rounds", 0))
            if self.p.flag("team_mode", False) or team_rounds > 0:
                return self._run_team(task, mode,
                                      meeting_rounds=team_rounds or 1)
            # Одиночный Lead Agent (25.08, фикс: return был утерян при правках,
            # и задача e2a9998d ушла в старый LLM-планировщик с облачным nemotron)
            return self._run_lead_agent(task, mode)

        agents = self.p._cap_list_local_models()
        if not agents:
            error = "Нет доступных локальных исполнителей"
            self.p.emit({"kind": "node_end", "node": "orchestrator", "error": error})
            return {"error": error}

        tools_info: list[dict[str, Any]] = []
        if self.p.flag("tools"):
            hints = self.p.registry.content_hints()
            tools_info = [
                {"name": entry.name,
                 "description": entry.manifest.description or "",
                 "content_hints": hints.get(entry.name, [])}
                for entry in self.p.registry.available()
                if entry.name not in self.p.AGENT_TOOLS_BLOCKLIST
                and (task.allowed_tools is None or entry.name in task.allowed_tools)
            ]

        orchestrator = Orchestrator(
            self.p.gateway, placement=placement, cloud_available=cloud_ok,
            max_steps=self.p.limit("max_plan_steps", 6), on_event=self.p.emit,
            planner_model=self.p.orchestrator_planner_model(task))

        plan = orchestrator.plan(task.prompt, agents, tools_info,
                                 context_summary=state.get("context_summary", ""),
                                 task_mode=mode)
        self.p.blackboard.save_plan(task.task_id, plan)
        self.p.emit({"kind": "plan", "node": "orchestrator", "ok": plan.ok,
                     "steps": [{"id": s.step_id, "title": s.title,
                                "assignee": s.assignee, "tools": s.tools,
                                "depends_on": s.depends_on} for s in plan.steps],
                     "reasoning": plan.reasoning, "model": plan.model,
                     "error": plan.error})

        # add_step_result теперь вызывается внутри _execute_plan сразу по готовности
        # каждого шага (живая персистенция). Повторный вызов здесь удвоил бы
        # строки tool_calls_log для каждого шага.
        results = self._execute_plan(state, plan)

        final = orchestrator.assemble(task.prompt, plan, results, task_mode=mode)

        # B5 (24.08): adversarial-ревью итога дешёвой локальной моделью.
        # Ловит «успех без результата»: сборщик пересказал намерения шагов,
        # а файлы фактически не созданы (задачи b75cdf92 / de9dc117 / aaa7ac95).
        if self.p.flag("orchestrator_review", True) and final and final.answer:
            review = orchestrator_adversarial(
                self.p.gateway, task.prompt, final.answer, self._make_review_fn(mode))
            self.p.emit({"kind": "adversarial_review",
                         "ok": review["ok"],
                         "issues_preview": (review["issues"] or "")[:200]})
            if not review["ok"]:
                try:
                    self.p.blackboard.log_decision(
                        task.task_id, stage="adversarial_review", actor="verifier",
                        summary=("Adversarial-ревью нашло несоответствия: "
                                 + review["issues"][:400]),
                        details={"issues": review["issues"]})
                except Exception:  # noqa: BLE001 — журнал не критичен
                    pass

        self.p.emit({"kind": "node_end", "node": "orchestrator",
                     "decided_by": final.decided_by,
                     "ok_steps": sum(1 for r in results if r.ok), "total": len(results)})
        return {"plan": plan, "step_results": results, "final": final}

    def _run_team(self, task: Task, mode: str,
                  meeting_rounds: int = 1) -> dict[str, Any]:
        """Делегат: см. strategies.run_team."""
        from workflows.strategies import run_team as _rt
        return _rt(self.p, task, mode, meeting_rounds=meeting_rounds)

    def _run_lead_agent(self, task: Task, mode: str) -> dict[str, Any]:
        """Делегат: см. strategies.run_lead_agent."""
        from workflows.strategies import run_lead_agent as _rla
        return _rla(self.p, task, mode)

    def _make_review_fn(self, mode: str):
        """Делегат: см. strategies.make_review_fn."""
        from workflows.strategies import make_review_fn
        return make_review_fn(self.p, mode)

    def _make_checkpoint_logger(self, task: Task, actor: str,
                                store: list | None = None):
        """Делегат: см. strategies.make_checkpoint_logger."""
        from workflows.strategies import make_checkpoint_logger
        return make_checkpoint_logger(self.p, task, actor, store)

    def _run_simple_orchestrated(self, task: Task, mode: str,
                                 context_summary: str) -> dict[str, Any]:
        """Делегат: см. strategies.run_simple_orchestrated."""
        from workflows.strategies import run_simple_orchestrated as _rso
        return _rso(self.p, task, mode, context_summary)

    def _extract_artifact(self, step_result: StepResult, cap: int = 20000) -> str:
        """
        Извлечь полезный артефакт из результатов шага для передачи дальше.

        Приоритет: полный текст, прочитанный файловым инструментом (data.text),
        затем результат QGIS-чтения атрибутов, затем любой машинный data.
        Ограничиваем cap: контекст model'ей вырос до 32K токенов, поэтому
        можно передавать заметно больше (20000 символов ≈ 5-6K токенов),
        а большие файлы по-прежнему читаются порциями (инструмент отдаёт
        next_offset для продолжения).
        """
        if not step_result.tool_results:
            return ""
        parts: list[str] = []
        for r in step_result.tool_results:
            data = r.data or {}
            if not data:
                continue
            if r.tool == "files" and r.action == "read_text" and data.get("text"):
                text = str(data["text"])
                # Указываем, что это порция, если файл большой
                if data.get("truncated"):
                    nxt = data.get("next_offset")
                    parts.append(text[:cap])
                    parts.append(f"[файл больше, прочитана порция до {nxt}; "
                                 f"используй read_text с offset={nxt} для продолжения]")
                else:
                    parts.append(text[:cap])
            elif r.tool == "qgis" and data.get("rows"):
                rows = data["rows"]
                parts.append(f"Прочитано строк: {len(rows)}. ")
                parts.append(str(rows)[:cap])
            elif r.tool == "files" and r.action == "summarize":
                # summarize возвращает распределения по колонкам (dist_*).
                # Это ГОТОВЫЕ данные для следующих шагов — передаём их целиком,
                # иначе модель в шаге записи выдумает числа (кейс 37c1b4d8).
                summary_data = dict(data)
                dist_lines = []
                for key in sorted(summary_data):
                    if key.startswith("dist_"):
                        col = key[5:]
                        dist = summary_data[key] or {}
                        top = "; ".join(f"{k}={v}" for k, v in (dist.get("top") or [])[:30])
                        dist_lines.append(f"{col}: уникальных={dist.get('unique','?')}, "
                                          f"пустых={dist.get('missing','?')} | {top}")
                if dist_lines:
                    parts.append(f"РАСПРЕДЕЛЕНИЯ (files.summarize) — используй их для отчёта:\n"
                                 + "\n".join(dist_lines)[:cap])
            elif data.get("result") or data.get("data"):
                val = data.get("result") or data.get("data")
                parts.append(str(val)[:cap])
        if not parts:
            return ""
        return "\n".join(parts)[:cap]

    def _execute_plan(self, state: GraphState, plan: Plan,
                      done: dict[str, StepResult] | None = None) -> list[StepResult]:
        """
        Выполнить шаги, соблюдая зависимости.

        Шаги без незакрытых зависимостей идут ОДНОЙ волной параллельно —
        Ollama сама сериализует загрузку моделей в VRAM. Результаты
        предшественников подставляются в задание текстом: малая модель
        не имеет доступа к состоянию графа.
        """
        task = state["task"]
        mode = self.p.effective_mode(task)
        max_iter = self.p.limit("max_tool_iterations", 3)
        done = dict(done or {})
        results: list[StepResult] = []
        pending = list(plan.steps)

        while pending:
            wave = [s for s in pending
                    if all(dep in done for dep in s.depends_on)]
            if not wave:
                wave = [pending[0]]
            for step in wave:
                pending.remove(step)

            # Шаг с ПРОВАЛИВШИМСЯ предшественником не выполняем: он получит
            # пустой или мусорный контекст и создаст иллюзию работы (задача
            # 5458dfec7b4e, 24.08: s3 упал, s4 всё равно запустился без данных
            # исследования и сгенерировал Angular 18 вместо 22). Помечаем
            # каскадно: провал тянется по графу зависимостей.
            blocked: list[StepResult] = []
            still_ok: list[PlanStep] = []
            for step in wave:
                failed_deps = [d for d in step.depends_on
                               if d in done and not done[d].ok]
                if failed_deps:
                    blocked.append(StepResult(
                        step_id=step.step_id, title=step.title,
                        assignee=step.assignee, ok=False,
                        error=(f"не выполнен: шаг-предшественник завершился "
                               f"с ошибкой ({', '.join(failed_deps)})")))
                else:
                    still_ok.append(step)
            results.extend(blocked)
            for b in blocked:
                done[b.step_id] = b
            wave = still_ok
            if not wave:
                continue

            def run_step(step=None) -> StepResult:
                assert step is not None
                started = time.perf_counter()
                spec = self.p._spec_by_id(step.assignee)
                if spec is None:
                    return StepResult(step_id=step.step_id, title=step.title,
                                      assignee=step.assignee, ok=False,
                                      error="Исполнитель недоступен")
                context_parts: list[str] = []
                for dep in step.depends_on:
                    previous = done.get(dep)
                    if previous is None:
                        continue
                    # АРТЕФАКТЫ: передаём следующему шагу ПОЛНЫЕ данные,
                    # которые добыл предшественник, а не только краткую
                    # выжимку. Иначе прочитанный файл теряется между шагами.
                    facts = "; ".join(
                        f"{r.tool}.{r.action}: {(r.summary or r.error or '')[:300]}"
                        for r in previous.tool_results[-2:])
                    artifact = self._extract_artifact(previous, cap=20000)
                    block = f"Результат шага {dep} ({previous.title}): {previous.answer}"
                    if artifact:
                        block += f"\nАРТЕФАКТ шага {dep}:\n{artifact}"
                    if facts:
                        block += f"\nФакты инструментов: {facts}"
                    context_parts.append(block)
                # ГЛОБАЛЬНЫЕ ДАННЫЕ: распределения из files.summarize, добытые ЛЮБЫМ
                # предыдущим шагом, должны доходить до шага записи (напр. Excel),
                # даже если он не зависит от них напрямую. Иначе модель выдумает числа.
                global_facts: list[str] = []
                for prev_key, prev in done.items():
                    if prev_key in (step.depends_on or []):
                        continue  # уже добавили выше
                    for r in (prev.tool_results or []):
                        if r.tool == "files" and r.action == "summarize" and r.ok:
                            g = self._extract_artifact(prev, cap=20000)
                            if g and g not in global_facts:
                                global_facts.append(
                                    f"Распределения из шага {prev_key} ({prev.title}):\n{g}")
                for gf in global_facts:
                    context_parts.append(gf)
                instruction = step.instruction
                if context_parts:
                    instruction = ("\n\n".join(context_parts)
                                   + f"\n\nТЕПЕРЬ ВЫПОЛНИ:\n{step.instruction}")
                registry = self.p.registry if (self.p.flag("tools") and step.tools) else None
                allowed = step.tools or None
                if task.allowed_tools is not None and allowed:
                    allowed = [t for t in allowed if t in task.allowed_tools]
                # Автопроверка + ограниченный повтор: раньше шаг считался
                # done, если модель просто не упала (result.ok), даже если
                # она ни разу не вызвала выданные инструменты и написала
                # "нет доступа к содержимому" — сборщик принимал такую
                # догадку за проверенный факт. check_step (core/step_check.py)
                # уже был импортирован, но не вызывался — это и была дырка.
                # Теперь: если после ответа check_step находит подозрительные
                # признаки (инструменты не вызваны, отказ под видом успеха,
                # пустой/слишком короткий ответ) — пробуем шаг ещё раз с явным
                # требованием реально вызвать инструмент, не выдавая догадку.
                max_attempts = 2
                step_result: StepResult | None = None
                for attempt in range(1, max_attempts + 1):
                    run_instruction = instruction
                    if attempt > 1 and step_result is not None:
                        run_instruction = (
                            instruction
                            + "\n\nПРЕДЫДУЩАЯ ПОПЫТКА ОТКЛОНЕНА АВТОПРОВЕРКОЙ: "
                            + "; ".join(step_result.warnings)
                            + f". Обязательно вызови нужный(е) инструмент(ы) из "
                              f"списка {step.tools} и приложи РЕАЛЬНЫЙ результат "
                              "(данные/файл), а не описание того, как бы ты это "
                              "сделал. Не пиши, что у тебя нет доступа — "
                              "инструменты для этого и даны."
                        )
                    self.p.emit({"kind": "step_start", "step_id": step.step_id,
                                 "title": step.title, "assignee": step.assignee,
                                 "model": spec.model, "tools": step.tools,
                                 "attempt": attempt})
                    # Живой статус шага в БД — чтобы панель и история видели
                    # прогресс даже из CLI-процесса, а не только в конце.
                    try:
                        self.p.blackboard.upsert_plan_step(
                            task.task_id, step, status="running", model=spec.model)
                        self.p.blackboard.set_status(task.task_id, "running")
                    except Exception:  # noqa: BLE001
                        pass
                    proposer = Proposer(spec, self.p.gateway, registry, max_iter,
                                        on_event=self.p.emit,
                                        checkpoint_every=3,
                                        on_checkpoint=lambda it, text: (
                                            self.p.emit({"kind": "checkpoint",
                                                         "step_id": step.step_id,
                                                         "iteration": it,
                                                         "preview": (text or "")[:200]})
                                        ),
                                        extra={"research_limit": int(
                                            (self.p.settings.get("limits") or {})
                                            .get("research_limit", 6))})
                    result = proposer.run(run_instruction, "", allowed, mode)
                    elapsed = int((time.perf_counter() - started) * 1000)
                    if not result.ok:
                        self.p.emit({"kind": "step_end", "step_id": step.step_id,
                                     "ok": False, "error": result.error,
                                     "model": spec.model, "latency_ms": elapsed})
                        try:
                            self.p.blackboard.upsert_plan_step(
                                task.task_id, step, status="failed", model=spec.model)
                        except Exception:  # noqa: BLE001
                            pass
                        return StepResult(
                            step_id=step.step_id, title=step.title,
                            assignee=step.assignee, model=spec.model,
                            ok=False, error=result.error,
                            latency_ms=elapsed)
                    candidate = StepResult(
                        step_id=step.step_id, title=step.title,
                        assignee=step.assignee, model=spec.model,
                        ok=True, answer=result.answer,
                        tool_results=result.tool_results,
                        latency_ms=elapsed,
                    )
                    candidate = check_step(step, candidate)
                    step_result = candidate
                    if candidate.suspicious and attempt >= max_attempts:
                        # Автопроверка нашла проблему (инструменты не вызваны
                        # или все вызовы упали), а попытки исчерпаны. Раньше
                        # шаг всё равно уходил как ok=True/status="done" —
                        # сборщик итога принимал догадку модели за факт и
                        # синтезировал неверное объяснение пользователю.
                        # Теперь такой шаг честно считается провальным
                        # (мутация ok/error вынесена в чистую функцию для тестов).
                        candidate = _apply_autocheck_verdict(candidate, max_attempts)
                        step_result = candidate
                        self.p.emit({"kind": "step_end", "step_id": step.step_id,
                                     "ok": False, "error": candidate.error,
                                     "model": spec.model, "latency_ms": elapsed,
                                     "suspicious": True, "attempt": attempt})
                        try:
                            self.p.blackboard.upsert_plan_step(
                                task.task_id, step, status="failed", model=spec.model)
                        except Exception:  # noqa: BLE001
                            pass
                        return candidate
                    if not candidate.suspicious or attempt >= max_attempts:
                        self.p.emit({"kind": "step_end", "step_id": step.step_id,
                                     "ok": True, "model": spec.model,
                                     "latency_ms": elapsed,
                                     "suspicious": candidate.suspicious,
                                     "attempt": attempt})
                        try:
                            self.p.blackboard.upsert_plan_step(
                                task.task_id, step, status="done", model=spec.model)
                        except Exception:  # noqa: BLE001
                            pass
                        return candidate
                    self.p.emit({"kind": "step_retry", "step_id": step.step_id,
                                 "model": spec.model, "warnings": candidate.warnings,
                                 "attempt": attempt})
                return step_result  # защитный код: цикл всегда возвращает выше

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(wave)) as pool:
                futures = {pool.submit(run_step, step): step for step in wave}
                for future in concurrent.futures.as_completed(futures):
                    step = futures[future]
                    try:
                        done[step.step_id] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        done[step.step_id] = StepResult(
                            step_id=step.step_id, title=step.title,
                            assignee=step.assignee, ok=False,
                            error=f"{type(exc).__name__}: {exc}")
                    # ЖИВАЯ ПЕРСИСТЕНЦИЯ: раньше add_step_result (а вместе с ним
                    # запись в tool_calls_log) откладывалась до конца ВСЕГО
                    # плана — вызывалась одним циклом после return из
                    # _execute_plan (см. run()). Из-за этого при долгом плане
                    # (10+ шагов, разные модели по 1-3 мин на шаг) ни ответ,
                    # ни ошибка, ни журнал вызовов инструментов уже готового
                    # и упавшего шага не были видны, пока не завершатся все
                    # оставшиеся шаги — задачу было невозможно диагностировать
                    # вживую (воспроизведено и подтверждено на task
                    # 87eef3701975 от 20.08: шаг s8 упал, но answer/error/
                    # tool_calls_log оставались пустыми ещё ~20+ минут, пока
                    # шли независимые s9/s10/s11). Теперь пишем каждый
                    # результат в блэкборд сразу по готовности шага.
                    try:
                        self.p.blackboard.add_step_result(task.task_id, done[step.step_id])
                    except Exception:  # noqa: BLE001 — сбой записи не должен ронять план
                        pass
            results = [done[s.step_id] for s in plan.steps if s.step_id in done]
        return results

    # ---- запуск графа ----------------------------------------------------

    def _ensure_allowed_roots(self, task: Task) -> None:
        """
        Автоматически ограничить запись файлов папками из текста задачи.

        Зачем (задача 931c894e5624, 23.08): механизм allowed_roots в
        tools/datafiles уже существовал, но срабатывал только если
        task.metadata["allowed_roots"] заполнен вручную — а его никто не
        заполнял, список оставался пустым, и запись молча уходила в
        ЗАПРЕЩЁННУЮ задачей папку прошлой попытки. Здесь мы достаём
        абсолютные пути из промпта и превращаем их в песочницу: первая же
        запись мимо отклоняется инструментом с понятной ошибкой, которую
        исполнитель видит и исправляет в той же итерации.

        Правила отбора (консервативные):
          - путь должен указывать внутрь рабочей области пользователя
            (домашняя папка или папка платформы), чтобы случайный путь
            из примера документации не отрезал весь диск;
          - берём РОДИТЕЛЬСКУЮ папку файла-пути: задача говорит
            «...\\backend\\server.ts» — писать разрешено во весь backend.
        """
        from agents.orchestrator import _extract_absolute_paths

        if not isinstance(task.metadata, dict):
            task.metadata = {}
        if task.metadata.get("allowed_roots"):
            return  # явное ограничение важнее автоопределения

        roots: list[str] = []
        profile = Path(os.environ.get("USERPROFILE") or Path.home()).resolve()
        platform_root = Path(__file__).resolve().parent.parent

        # Пути, упомянутые с ЗАПРЕТОМ («НЕ используй», «артефакты прошлой
        # попытки», ...), не должны открывать запись. Задача 931c894e5624:
        # промпт прямо запрещал C:\Users\<имя>\calisthenics\backend, но
        # простое извлечение путей сделало бы её разрешённой.
        prompt_text = task.prompt or ""
        _NEG_WINDOW = 120  # символов до пути, где ищем маркер запрета
        neg_markers = ("не используй", "не переиспользуй", "не читай",
                       "не пиши", "не трогай", "игнорируй",
                       "артефакты прошлой", "прошлой попытки",
                       "предыдущей попытки", "старые файлы")

        def _is_negated(pos: int) -> bool:
            start = max(0, pos - _NEG_WINDOW)
            window = prompt_text[start:pos].lower()
            return any(m in window for m in neg_markers)

        seen_positions: set[int] = set()

        for raw in _extract_absolute_paths(prompt_text):
            # Регэксп может прихватить обрамляющий markdown-символ:
            # `C:\...\init-db.ts` -> путь с хвостовым бэктиком, который
            # превращает файл в «папку без расширения».
            raw = raw.rstrip("`'\"")
            pos = -1
            while True:
                pos = prompt_text.find(raw, pos + 1)
                if pos == -1:
                    break
                if _is_negated(pos):
                    self.p.emit({"kind": "sandbox_skip", "path": raw,
                                 "reason": "путь упомянут с запретом в задаче"})
                    raw = ""
                    break
            if not raw:
                continue
            try:
                p = Path(raw.replace("/", "\\")).resolve()
            except (OSError, ValueError):
                continue
            inside_profile = False
            for base in (profile, platform_root):
                try:
                    p.relative_to(base)
                    inside_profile = True
                    break
                except ValueError:
                    continue
            if not inside_profile:
                continue
            # Файл с расширением -> песочница = его папка; путь без
            # расширения считаем самой папкой проекта.
            root = p.parent if p.suffix else p
            root_str = str(root)
            if root_str not in roots:
                roots.append(root_str)

        # Отбрасываем ПРЕДКОВ, оставляя самые конкретные корни: если один
        # путь — предок другого (упомянуты C:\Users\x и C:\Users\x\project),
        # песочницей должна быть вложенная папка проекта, а не домашняя.
        # Иначе случайное упоминание файла в профиле открывает запись по
        # всему профилю (задача 931c894e5624: промпт перечислял запрещённые
        # пути прошлой попытки C:\Users\<имя>\init-db.ts).
        filtered = [r for r in roots
                    if not any(o != r and o.startswith(r + "\\")
                               for o in roots)]
        if filtered:
            task.metadata["allowed_roots"] = filtered
            self.p.emit({"kind": "sandbox", "roots": filtered,
                         "reason": "папки записи определены из путей задачи"})

    def run(self, task: Task) -> dict[str, Any]:
        """
        Выполнить полный workflow для задачи.

        Порядок узлов:
        1. Router — классификация задачи
        2. Secretary — сводка контекста
        3. Retrieval — векторный поиск (если включён и нужен)
        4. Proposers — 3 модели параллельно (или 1 для SIMPLE)
        5. Verifier — голосование 2 из 3
        6. Judge — арбитр при расхождениях (если включён)
        7. Secretary (persist) — запись результата в Blackboard

        В режиме ORCHESTRATED узлы 4-6 заменяются на node_orchestrator.
        """
        self.p._current_task_id = task.task_id
        self._ensure_allowed_roots(task)
        self.p._current_task_metadata = task.metadata or {}
        self.p.blackboard.create_task(task)
        self.p.refresh_tool_context()
        # Статус задачи сразу «выполняется», а не висит в «создана» весь прогон
        try:
            self.p.blackboard.set_status(task.task_id, "running")
        except Exception:  # noqa: BLE001
            pass
        self.p.emit({"kind": "task_start", "task_id": task.task_id, "prompt": task.prompt})

        state: GraphState = {"task": task}

        # 1. Router
        state.update(self.node_router(state))
        if state.get("error"):
            self.p.blackboard.set_status(task.task_id, "failed", state["error"])
            self.p.emit({"kind": "task_end", "task_id": task.task_id,
                         "ok": False, "error": state["error"]})
            return state

        routing = state.get("routing")

        # 2. Secretary (initial context summary)
        state.update(self.node_secretary(state))

        # 3. Retrieval (optional)
        if self.p.flag("retrieval") and routing and routing.needs_retrieval:
            state.update(self.node_retrieval(state))

        # 4-6. Основной путь или оркестратор
        if task.mode == ExecutionMode.ORCHESTRATED:
            state.update(self.node_orchestrator(state))
        else:
            # 4. Proposers
            state.update(self.node_proposers(state))
            if state.get("error"):
                self.p.blackboard.set_status(task.task_id, "failed", state["error"])
                self.p.emit({"kind": "task_end", "task_id": task.task_id,
                             "ok": False, "error": state["error"]})
                return state

            # 5. Verifier
            state.update(self.node_verifier(state))

            # 6. Judge (если нужен)
            state.update(self.node_judge(state))

        # 7. Secretary (persist) — финальная запись (Blackboard + память)
        state.update(self.node_secretary_persist(state))

        self.p.emit({"kind": "task_end", "task_id": task.task_id,
                     "ok": bool(state.get("final")),
                     "error": state.get("error")})

        return state
