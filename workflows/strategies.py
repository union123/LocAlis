"""
Run-strategies (25.08): выделены из Workflow (критика ревью — SRP).

Каждая функция принимает platform и задачу, возвращает
{"plan", "step_results", "final"} либо {"error"}.
Workflow вызывает их как тонкие обёртки — поведение не изменилось.
"""

from __future__ import annotations

import time
from typing import Any

from core.schemas import FinalDecision, Plan, StepResult, Task
from agents.orchestrator import Orchestrator
from workflows.main_graph import state_context_summary, orchestrator_adversarial



def make_review_fn(platform, mode: str):
    """
    Единая фабрика функции adversarial-ревью (критика ревью 25.08:
    _review_fn дублировалась трижды буква в букву).
    """
    def _review(prompt: str) -> str:
        vspec = platform.gateway.spec("verifier")
        resp = platform.gateway.chat(vspec, [{"role": "user", "content": prompt}],
                                   tools=None, task_mode=mode)
        return resp.text if resp.ok else ""
    return _review

def make_checkpoint_logger(platform, task: Task, actor: str,
                            store: list | None = None):
    """
    Единая фабрика колбэка чекпоинта (критика ревью 25.08: _on_checkpoint
    дублировалась дважды). Пишет событие + решение в Blackboard.
    """
    def _on_checkpoint(it: int, text: str) -> None:
        preview = (text or "")[:200]
        platform.emit({"kind": "checkpoint", "iteration": it,
                     "preview": preview})
        try:
            platform.blackboard.log_decision(
                task.task_id, stage="checkpoint", actor=actor,
                summary=f"Итерация {it}: {preview}")
        except Exception:  # noqa: BLE001 — чекпоинт не критичен
            pass
        if store is not None:
            store.append({"iteration": it, "preview": preview})
    return _on_checkpoint

def run_team(platform, task: Task, mode: str,
             meeting_rounds: int = 1) -> dict[str, Any]:
    """run_team: никогда не бросает исключений."""
    try:
        return _run_team_impl(platform, task, mode,
                              meeting_rounds=meeting_rounds)
    except BaseException as exc:
        try:
            platform.emit({"kind": "strategy_crash", "where": "run_team",
                           "error": f"{type(exc).__name__}: {exc}"[:300]})
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"{type(exc).__name__}: {exc}", "final": None}


def run_lead_agent(platform, task: Task, mode: str) -> dict[str, Any]:
    """run_lead_agent: никогда не бросает исключений."""
    try:
        return _run_lead_agent_impl(platform, task, mode)
    except BaseException as exc:
        try:
            platform.emit({"kind": "strategy_crash", "where": "run_lead_agent",
                           "error": f"{type(exc).__name__}: {exc}"[:300]})
        except Exception:  # noqa: BLE001
            pass
        return {"error": f"{type(exc).__name__}: {exc}", "final": None}



def run_simple_orchestrated(platform, task: Task, mode: str,
                              context_summary: str) -> dict[str, Any]:
    """
    Выполнить SIMPLE задачу в режиме orchestrated без планировщика.
    Запускаем одного лучшего локального proposer'а (proposer_a) с инструментами.
    """
    from agents.proposer import Proposer
    from core.schemas import Proposal

    agents = platform._cap_list_local_models()
    if not agents:
        error = "Нет доступных локальных исполнителей"
        return {"error": error, "final": None}

    # Берём первого доступного исполнителя (обычно proposer_a = qwen3:8b)
    spec = platform._spec_by_id(agents[0]["id"])
    if spec is None:
        error = "Исполнитель недоступен"
        return {"error": error, "final": None}

    registry = platform.registry if (platform.flag("tools") and platform.flag("proposers")) else None
    allowed = task.allowed_tools
    if registry is not None and allowed is None:
        allowed = [e.name for e in registry.available()
                   if e.name not in platform.AGENT_TOOLS_BLOCKLIST]

    max_iter = platform.limit("max_tool_iterations", 3)
    proposer = Proposer(spec, platform.gateway, registry, max_iter, on_event=platform.emit)

    platform.emit({"kind": "node_start", "node": "simple_proposer", "model": spec.model})
    proposal = proposer.run(task.prompt, context_summary, allowed, mode)
    platform.emit({"kind": "node_end", "node": "simple_proposer",
                 "ok": proposal.ok, "model": spec.model})

    from core.schemas import FinalDecision
    if proposal.ok and proposal.answer:
        final = FinalDecision(
            answer=proposal.answer,
            rationale=f"Выполнено одним исполнителем ({spec.model}) для SIMPLE задачи",
            decided_by="simple_orchestrated",
            model=spec.model,
            used_cloud=False,
            subagent_calls=[],
        )
    else:
        final = FinalDecision(
            answer=proposal.error or "Предложение не получено",
            rationale="SIMPLE задача не выполнена",
            decided_by="simple_orchestrated",
            model=spec.model,
            used_cloud=False,
            subagent_calls=[],
        )

    platform.blackboard.add_proposal(task.task_id, proposal)
    return {"plan": None, "step_results": [], "final": final}



def _run_team_impl(platform, task: Task, mode: str,
              meeting_rounds: int = 1) -> dict[str, Any]:
    """
    Режим «Команда» (24.08): реализация изначальной задумки платформы.
    Фаза 1: совещание — N раундов обмена планами, консенсус от арбитра.
    Фаза 2: выполнение по консенсус-плану одним сильным исполнителем
    (Lead Agent с планом в контексте).
    Фаза 3: крест-накрест — каждый проверяет файлы другого по фактам
    с диска, проблемы возвращаются на исправление (1 автоцикл).
    """
    from agents.proposer import Proposer
    from agents.team_agent import TeamMeeting

    agents = platform._cap_list_local_models()
    if len(agents) < 2:
        platform.emit({"kind": "node_end", "node": "team",
                     "reason": "меньше 2 исполнителей — команда невозможна"})
        return run_lead_agent(platform, task, mode)
    specs = [platform._spec_by_id(a["id"]) for a in agents]
    specs = [s for s in specs if s is not None][:3]

    registry = platform.registry if platform.flag("tools") else None
    allowed = task.allowed_tools
    if registry is not None and allowed is None:
        allowed = [e.name for e in registry.available()
                   if e.name not in platform.AGENT_TOOLS_BLOCKLIST]

    tools_info = "\n".join(
        f"- {e.name}: {(e.manifest.description or '')[:80]}"
        for e in platform.registry.available()
        if e.name not in platform.AGENT_TOOLS_BLOCKLIST)

    # ---- ФАЗА 1: совещание ------------------------------------------
    platform.emit({"kind": "node_start", "node": "team_meeting",
                 "rounds": meeting_rounds, "models": [s.model for s in specs]})
    meeting = TeamMeeting(
        platform.gateway, specs, on_event=platform.emit,
        meeting_rounds=meeting_rounds, task_mode=mode)
    result_meeting = meeting.meeting(
        task.prompt, tools_info,
        context_summary=state_context_summary(platform, task))
    platform.emit({"kind": "meeting_done",
                 "rounds": meeting_rounds,
                 "consensus_preview": result_meeting["consensus_text"][:300],
                 "consensus_full": result_meeting["consensus_text"]})
    try:
        platform.blackboard.log_decision(
            task.task_id, stage="team_meeting", actor="team",
            summary=("Совещание: " + meeting_rounds * "R" +
                     f", консенсус: {result_meeting['consensus_text'][:200]}"),
            details={"rounds_log": result_meeting["rounds_log"],
                     "consensus": result_meeting["consensus_text"]})
    except Exception:  # noqa: BLE001
        pass

    # ---- ФАЗА 2: выполнение по консенсусу ---------------------------
    # Консенсус-план вплетаем в задание Lead-исполнителя.
    # Lead-исполнитель команды: из metadata (включая lead_agent_extra типа
    # Coder-Next), иначе первый пропозер.
    wanted = str((task.metadata or {}).get("lead_agent_model") or "").strip()
    lead_spec = None
    if wanted:
        extra_specs = platform.gateway.lead_agent_extra_specs()
        for ps in list(specs) + extra_specs:
            if ps.model == wanted:
                lead_spec = ps
                break
    if lead_spec is None:
        lead_spec = specs[0]
    lead_ctx = int((task.metadata or {}).get("lead_agent_num_ctx") or 32768)
    lead_spec.num_ctx = lead_ctx

    enriched_prompt = (
        f"{task.prompt}\n\n"
        "=== КОНСЕНСУС-ПЛАН КОМАНДЫ (обязателен к исполнению) ===\n"
        f"{result_meeting['consensus_text'][:6000]}\n"
        "=== КОНЕЦ ПЛАНА ===\n"
        "Выполни задачу строго по этому плану. Каждый файл записывай "
        "инструментом datafiles write_text с полным путём.")

    max_iter = int((platform.settings.get("limits") or {}).get(
        "lead_agent_iterations", 25))
    checkpoints: list[dict[str, Any]] = []

    def _on_checkpoint(it: int, text: str) -> None:
        checkpoints.append({"iteration": it, "preview": (text or "")[:200]})
        platform.emit({"kind": "checkpoint", "iteration": it,
                     "preview": (text or "")[:200]})
        try:
            platform.blackboard.log_decision(
                task.task_id, stage="checkpoint", actor=lead_spec.id,
                summary=f"Итерация {it}: {(text or '')[:200]}")
        except Exception:  # noqa: BLE001
            pass

    proposer = Proposer(
        lead_spec, platform.gateway, registry, max_iter, on_event=platform.emit,
        checkpoint_every=3, on_checkpoint=_on_checkpoint,
        extra={"research_limit": int((platform.settings.get("limits") or {})
                                     .get("research_limit", 6))})
    platform.emit({"kind": "node_start", "node": "lead_agent",
                 "model": lead_spec.model, "max_iterations": max_iter,
                 "team": True})
    # Память и сводка контекста доходят до исполнителя (раньше "" —
    # past_results/facts не попадали в system prompt командного прогона)
    proposal = proposer.run(enriched_prompt,
                            state_context_summary(platform, task),
                            allowed, mode)
    # Fallback (25.08): llamacpp-исполнитель недоступен -> локальный пропозер
    if not proposal.ok and lead_spec.provider == "llamacpp":
        platform.emit({"kind": "lead_fallback", "from": lead_spec.model,
                       "reason": (proposal.error or "")[:200]})
        fb_spec = next((p for p in specs), None)
        if fb_spec is not None:
            fb_proposer = Proposer(fb_spec, platform.gateway, registry,
                                   max_iter, on_event=platform.emit,
                                   checkpoint_every=3,
                                   on_checkpoint=make_checkpoint_logger(
                                       platform, task, fb_spec.id, checkpoints),
                                   extra={"research_limit": research_limit})
            proposal = fb_proposer.run(enriched_prompt,
                                       state_context_summary(platform, task),
                                       allowed, mode)
    platform.emit({"kind": "node_end", "node": "lead_agent",
                 "ok": proposal.ok, "model": lead_spec.model})

    # ---- ФАЗА 3: крест-накрест ---------------------------------------
    from core.schemas import FinalDecision, StepResult
    step_result = StepResult(
        step_id="team", title="Команда: совещание + выполнение",
        assignee=lead_spec.id, model=lead_spec.model,
        answer=proposal.answer, ok=proposal.ok, error=proposal.error,
        tool_results=proposal.tool_results)

    # Журнал шага + вызовов инструментов (раньше в _run_team этого не было,
    # и tool_calls_log оставался пустым — задача ef78663c, 25.08)
    try:
        platform.blackboard.add_step_result(task.task_id, step_result)
    except Exception:  # noqa: BLE001
        pass

    review_summary = ""
    if proposal.ok and platform.flag("orchestrator_review", True):
        # Собираем ФАКТИЧЕСКИЕ файлы из tool_results (что реально записано)
        created = []
        seen: set[str] = set()
        for tr in proposal.tool_results:
            if tr.ok and tr.tool == "datafiles":
                p = (tr.data or {}).get("path") or ""
                if p and p not in seen:
                    seen.add(p)
                    created.append({"path": p, "author": lead_spec.id})

        def _read_fn(path: str) -> str:
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.read()[:2500]
            except OSError:
                return "(файл не найден на диске)"

        review = meeting.cross_review(task.prompt, created, _read_fn)
        review_summary = "; ".join(
            f"[{i['reviewer']}] {i['comment'][:150]}"
            for i in review["issues"][:3])
        platform.emit({"kind": "cross_review_done",
                     "clean": review["clean"],
                     "issues": len(review["issues"])})
        try:
            platform.blackboard.log_decision(
                task.task_id, stage="cross_review", actor="team",
                summary=("Крест-накрест: " + ("чисто" if review["clean"]
                         else f"{len(review['issues'])} проблем: "
                              + review_summary[:300])),
                details={"issues": review["issues"]})
        except Exception:  # noqa: BLE001
            pass

        if not review["clean"]:
            # Автоцикл исправления: автор получает проблемы и чинит (1 цикл)
            platform.emit({"kind": "team_fix_cycle"})
            fix_prompt = (
                f"{enriched_prompt}\n\n"
                "=== ПРОБЛЕМЫ, НАЙДЕННЫЕ ПРИ ВЗАИМНОЙ ПРОВЕРКЕ ===\n"
                + "\n".join(f"- [{i['reviewer']}] {i['file']}: "
                             f"{i['comment'][:200]}"
                             for i in review["issues"][:8])
                + "\n=== ИСПРАВЬ ИХ: перезапиши проблемные файлы "
                  "инструментом записи с учётом замечаний. ===")
            fixer = Proposer(
                lead_spec, platform.gateway, registry, max_iter,
                on_event=platform.emit,
                extra={"research_limit": 2})
            fix_result = fixer.run(fix_prompt, "", allowed, mode)
            step_result = StepResult(
                step_id="team_fix", title="Команда: исправления по крест-накресту",
                assignee=lead_spec.id, model=lead_spec.model,
                answer=fix_result.answer or proposal.answer,
                ok=fix_result.ok, error=fix_result.error,
                tool_results=fix_result.tool_results)
            try:
                platform.blackboard.add_step_result(task.task_id, step_result)
            except Exception:  # noqa: BLE001
                pass

    final = FinalDecision(
        answer=step_result.answer or (step_result.error or ""),
        rationale=(f"Команда ({', '.join(s.model for s in specs)}): "
                   f"совещание {meeting_rounds} р., выполнение, "
                   f"крест-накрест. {review_summary}"),
        decided_by="orchestrator_local", model=lead_spec.model,
        used_cloud=False)

    # Adversarial-ревью финала (как в Lead Agent)
    if platform.flag("orchestrator_review", True) and proposal.ok:
        adv = orchestrator_adversarial(platform.gateway, task.prompt,
                                       final.answer,
                                       make_review_fn(platform, mode))
        platform.emit({"kind": "adversarial_review", "ok": adv["ok"],
                     "issues_preview": (adv["issues"] or "")[:200]})
        final.rationale = ((final.rationale or "") +
                           f"\n\nAdversarial-ревью: {adv['issues'] or 'OK'}")[:4000]
        if not adv["ok"]:
            try:
                platform.blackboard.set_status(
                    task.task_id, "failed",
                    "adversarial-ревью: " + adv["issues"][:300])
            except Exception:  # noqa: BLE001
                pass

    return {"plan": None, "step_results": [step_result], "final": final}



def _run_lead_agent_impl(platform, task: Task, mode: str) -> dict[str, Any]:
    """
    LEAD AGENT (24.08): один сильный исполнитель делает задачу целиком
    с полным контекстом и всеми инструментами. Без LLM-планировщика.

    Отличия от _run_simple_orchestrated:
      - повышенный лимит итераций (lead_limit из settings, по умолчанию 25);
      - self-checkpoint каждые 3 итерации -> событие checkpoint (прогресс
        виден в панели до завершения);
      - research_limit — лимит поисковых вызовов до первой записи;
      - после выполнения — adversarial-ревью верификатором.
    """
    from agents.proposer import Proposer

    agents = platform._cap_list_local_models()
    if not agents:
        error = "Нет доступных локальных исполнителей"
        platform.emit({"kind": "node_end", "node": "orchestrator", "error": error})
        return {"error": error}

    spec = None
    # Выбор Lead-исполнителя из UI: task.metadata["lead_agent_model"] —
    # имя модели (не proposer_id). Ищем среди спецификаций пропозеров.
    wanted_model = str((task.metadata or {}).get("lead_agent_model") or "").strip()
    if wanted_model:
        # поиск: пропозеры + lead_agent_extra (тяжёлые модели типа Coder-Next)
        pool = platform.gateway.proposer_specs() \
               + platform.gateway.lead_agent_extra_specs()
        for ps in pool:
            if ps.model == wanted_model:
                spec = ps
                break
        if spec is None:
            # Модель есть в Ollama, но не в пропозерах — собираем спецификацию вручную
            from core.llm_gateway import ModelSpec
            spec = ModelSpec(id="lead_agent", provider="ollama", model=wanted_model,
                             temperature=0.2, num_ctx=32768, supports_tools=True)
    if spec is None:
        spec = platform._spec_by_id(agents[0]["id"])
    if spec is None:
        return {"error": "Исполнитель недоступен"}

    # Переопределение контекста из UI (lead_agent_num_ctx): 128K для
    # длинных задач, если модель/память позволяют.
    wanted_ctx = int((task.metadata or {}).get("lead_agent_num_ctx") or 0)
    if wanted_ctx >= 2048:
        spec.num_ctx = wanted_ctx

    registry = platform.registry if platform.flag("tools") else None
    allowed = task.allowed_tools
    if registry is not None and allowed is None:
        allowed = [e.name for e in registry.available()
                   if e.name not in platform.AGENT_TOOLS_BLOCKLIST]

    max_iter = int((platform.settings.get("limits") or {}).get("lead_agent_iterations", 25))
    research_limit = int((platform.settings.get("limits") or {}).get("research_limit", 6))
    context_summary = ""
    try:
        context_summary = platform.blackboard.load_record(task.task_id).context_summary or ""
    except Exception:  # noqa: BLE001 — сводка не критична
        pass

    checkpoints: list[dict[str, Any]] = []
    proposer = Proposer(
        spec, platform.gateway, registry, max_iter, on_event=platform.emit,
        checkpoint_every=3,
        on_checkpoint=make_checkpoint_logger(platform, task, spec.id, checkpoints),
        extra={"research_limit": research_limit},
    )

    platform.emit({"kind": "node_start", "node": "lead_agent",
                 "model": spec.model, "max_iterations": max_iter})
    proposal = proposer.run(task.prompt, context_summary, allowed, mode)

    # Fallback-цепочка (25.08): внешние серверы могут быть не запущены.
    # Если выбранная Lead-модель из lead_agent_extra недоступна — повторяем
    # на первом локальном пропозере с пометкой в итоговом отчёте.
    fallback_note = ""
    if not proposal.ok and spec.provider == "llamacpp":
        platform.emit({"kind": "lead_fallback",
                       "from": spec.model,
                       "reason": (proposal.error or "")[:200]})
        fb_spec = next((p for p in platform.gateway.proposer_specs()), None)
        if fb_spec is not None:
            fb_proposer = Proposer(fb_spec, platform.gateway, registry,
                                   max_iter, on_event=platform.emit,
                                   checkpoint_every=3,
                                   on_checkpoint=make_checkpoint_logger(
                                       platform, task, fb_spec.id, checkpoints),
                                   extra={"research_limit": research_limit})
            proposal = fb_proposer.run(task.prompt, context_summary,
                                       allowed, mode)
            fallback_note = (f"Модель {spec.model} была недоступна "
                             f"({(proposal.error or '')[:80]}); выполнено на "
                             f"{fb_spec.model}.")

    platform.emit({"kind": "node_end", "node": "lead_agent",
                 "ok": proposal.ok, "model": spec.model})

    from core.schemas import FinalDecision, StepResult
    step_result = StepResult(
        step_id="lead", title="Выполнить задачу целиком (Lead Agent)",
        assignee=spec.id, model=spec.model,
        answer=proposal.answer, ok=proposal.ok, error=proposal.error,
        tool_results=proposal.tool_results)
    try:
        platform.blackboard.add_step_result(task.task_id, step_result)
    except Exception:  # noqa: BLE001
        pass

    final = FinalDecision(
        answer=proposal.answer or (proposal.error or ""),
        rationale=(f"Lead Agent ({spec.model}) выполнил задачу целиком "
                   f"за {len(proposal.tool_results)} вызовов инструментов."),
        decided_by="orchestrator_local", model=spec.model, used_cloud=False)

    # Adversarial-ревью (B5) — как в многошаговом пути.
    if platform.flag("orchestrator_review", True) and proposal.ok and final.answer:
        orchestrator = Orchestrator(
            platform.gateway, placement=platform.orchestrator_placement(task),
            cloud_available=False, on_event=platform.emit)
        review = orchestrator.adversarial_review(
            task.prompt, final.answer,
            review_fn=make_review_fn(platform, mode))
        final.rationale = ((final.rationale or "") +
                           f"\n\nAdversarial-ревью: {review['issues'] or 'OK'}")[:4000]
        platform.emit({"kind": "adversarial_review", "ok": review["ok"],
                     "issues_preview": (review["issues"] or "")[:200]})
        try:
            platform.blackboard.log_decision(
                task.task_id, stage="adversarial_review", actor="verifier",
                summary=("Adversarial-ревью: " + (review["issues"][:400] or "OK")),
                details={"issues": review["issues"]})
        except Exception:  # noqa: BLE001
            pass
        if not review["ok"]:
            final.rationale = ("Adversarial-ревью отклонило результат задачи. "
                               "Цикл исполнения завершён (см. шаг lead), "
                               "но итог не принят. Причины: "
                               + (review["issues"][:600] or "см. журнал ревью")
                               + " | [Процесс: "
                               + (final.rationale or "") + "]")[:4000]
            # Провал ревью = провал задачи: статус failed, а не done.
            # Иначе пользователь видит «выполнено» при пустой папке
            # (задача e525134f, 24.08: 25 итераций проверок, 0 записей).
            try:
                platform.blackboard.set_status(
                    task.task_id, "failed",
                    "adversarial-ревью: " + review["issues"][:300])
            except Exception:  # noqa: BLE001
                pass

    return {"plan": None, "step_results": [step_result], "final": final}
