"""
Judge / Supervisor (слой 7).

Приоритет: облачная сильная модель (через ModeController + Circuit Breaker)
-> fallback локальная модель. Облако НЕ обязательно: при недоступности задача
доводится до конца локально (graceful degradation).

Agents-as-tools в обратную сторону: облачный Judge может вызвать конкретную
локальную модель повторно с уточняющим промптом — для этого ему выдаются
служебные функции local_agent__ask и (если разрешено) инструменты платформы.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from core.llm_gateway import LLMGateway
from core.schemas import FinalDecision, Proposal, Verdict
from core.tool_registry import ToolRegistry

JUDGE_VERSION = "1.0"

_SYSTEM_PROMPT = (
    "Ты старший арбитр (Judge). Тебе даны задача, ответы моделей и заключение "
    "проверяющего. Выбери или сформулируй ИТОГОВЫЙ ответ по-русски.\n"
    "Правила:\n"
    "1. Факты о файлах и данных бери только из результатов инструментов; "
    "если данных не хватает — уточни их вызовом инструмента или переспроси "
    "локальную модель через функцию local_agent__ask.\n"
    "2. Не смешивай противоречивые факты — выбери обоснованный вариант.\n"
    "3. Ответ дай в формате:\n"
    "ОТВЕТ: <итоговый ответ>\n"
    "ОБОСНОВАНИЕ: <почему выбран этот вариант>"
)

# Служебная функция: Judge вызывает локальную модель как sub-agent tool
_LOCAL_AGENT_TOOL = {
    "type": "function",
    "function": {
        "name": "local_agent__ask",
        "description": (
            "Задать уточняющий вопрос конкретной ЛОКАЛЬНОЙ модели-исполнителю. "
            "Используй, когда нужно перепроверить деталь или получить данные "
            "через инструменты платформы."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "proposer_id": {"type": "string",
                                "description": "Идентификатор локальной модели, например proposer_a"},
                "question": {"type": "string", "description": "Уточняющий вопрос по-русски"},
            },
            "required": ["question"],
        },
    },
}


class Judge:
    """Арбитр с приоритетом облака и локальным fallback."""

    version = JUDGE_VERSION

    def __init__(
        self,
        gateway: LLMGateway,
        registry: ToolRegistry | None = None,
        subagent_runner: Callable[[str, str], str] | None = None,
        allow_cloud: bool = True,
        allow_subagents: bool = True,
        max_iterations: int = 3,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ):
        self.gateway = gateway
        self.registry = registry
        # subagent_runner(proposer_id, question) -> текст ответа локальной модели
        self.subagent_runner = subagent_runner
        self.allow_cloud = allow_cloud
        self.allow_subagents = allow_subagents
        self.max_iterations = max_iterations
        self.on_event = on_event

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            try:
                self.on_event({"kind": kind, "actor": "judge", **payload})
            except Exception:  # noqa: BLE001
                pass

    # ---- промпт ----------------------------------------------------------

    @staticmethod
    def _build_user_message(task_prompt: str, proposals: list[Proposal],
                            verdict: Verdict | None, context_summary: str) -> str:
        parts = [f"ЗАДАЧА: {task_prompt}"]
        if context_summary:
            parts.append(context_summary)
        for proposal in proposals:
            if proposal.ok and proposal.answer.strip():
                block = f"[{proposal.proposer_id}] ({proposal.model})\n{proposal.answer[:2500]}"
                if proposal.tool_results:
                    facts = "; ".join(
                        f"{r.tool}.{r.action}: {r.summary or r.error}"
                        for r in proposal.tool_results[-3:])
                    block += f"\nФакты инструментов: {facts}"
            else:
                block = f"[{proposal.proposer_id}] ({proposal.model}) — сбой: {proposal.error}"
            parts.append(block)
        if verdict is not None:
            parts.append(
                f"ЗАКЛЮЧЕНИЕ ПРОВЕРЯЮЩЕГО: consensus={verdict.consensus}; "
                f"согласны={verdict.agreeing}; расходятся={verdict.dissenting}; "
                f"причина: {verdict.reason}")
        return "\n\n".join(parts)

    # ---- главный вход ----------------------------------------------------

    def decide(
        self,
        task_prompt: str,
        proposals: list[Proposal],
        verdict: Verdict | None,
        context_summary: str = "",
        task_mode: str | None = None,
        allowed_tools: list[str] | None = None,
    ) -> FinalDecision:
        cloud_spec, local_spec = self.gateway.judge_specs()
        # Цепочка облачных арбитров; если шлюз старой версии — берём одиночный
        chain = []
        if hasattr(self.gateway, "cloud_judge_chain"):
            try:
                chain = list(self.gateway.cloud_judge_chain() or [])
            except Exception:  # noqa: BLE001
                chain = []
        if not chain and cloud_spec is not None:
            chain = [cloud_spec]

        user_message = self._build_user_message(
            task_prompt, proposals, verdict, context_summary)

        # 1. Облако (если разрешено политикой и доступно физически)
        if self.allow_cloud and chain and task_mode != "local_only":
            status = self.gateway.mc.status(task_mode=task_mode)
            self._emit("cloud_check", allowed=status.allowed, reason=status.reason)
            if status.allowed:
                # Перебираем арбитров по очереди: бесплатные тиры часто отдают 429,
                # и без резерва задача сразу уходила бы на слабую локальную модель
                for attempt, spec in enumerate(chain, start=1):
                    decision = self._run(spec, user_message, task_mode,
                                         allowed_tools, used_cloud=True)
                    if decision is not None:
                        return decision
                    self._emit("cloud_failed", model=spec.model, attempt=attempt,
                               reason=f"Облачный арбитр {spec.model} не дал ответа")

        # 2. Локальный fallback
        decision = self._run(local_spec, user_message, task_mode,
                             allowed_tools, used_cloud=False)
        if decision is not None:
            return decision

        # 3. Крайний случай: берём лучшее из имеющегося
        fallback_answer = (verdict.chosen_answer if verdict and verdict.chosen_answer
                           else next((p.answer for p in proposals if p.ok and p.answer), ""))
        return FinalDecision(
            answer=fallback_answer or "Не удалось получить ответ ни от одной модели.",
            rationale="Judge недоступен: возвращён ответ с наибольшей поддержкой.",
            decided_by="fallback", model="none", used_cloud=False,
        )

    # ---- цикл одного судьи ------------------------------------------------

    def _tools_for_judge(self, allowed_tools: list[str] | None) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        if self.registry is not None:
            tools += self.registry.openai_tools(allowed_tools)
        if self.allow_subagents and self.subagent_runner is not None:
            tools.append(_LOCAL_AGENT_TOOL)
        return tools

    def _run(self, spec, user_message: str, task_mode: str | None,
             allowed_tools: list[str] | None, used_cloud: bool) -> FinalDecision | None:
        """Один судья с циклом инструментов и суб-агентов. None = не смог."""
        tools = self._tools_for_judge(allowed_tools)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ]
        subagent_calls: list[str] = []

        for iteration in range(self.max_iterations + 1):
            self._emit("model_call", model=spec.model, provider=spec.provider,
                       iteration=iteration, used_cloud=used_cloud)
            response = self.gateway.chat(spec, messages, tools=tools or None,
                                         task_mode=task_mode)
            if not response.ok:
                self._emit("error", model=spec.model, error=response.error)
                return None

            calls = response.tool_calls if (tools and iteration < self.max_iterations) else []
            if not calls:
                answer, rationale = _split_answer(response.text)
                if not answer:
                    return None
                return FinalDecision(
                    answer=answer, rationale=rationale,
                    decided_by="judge_cloud" if used_cloud else "judge_local",
                    model=spec.model, used_cloud=used_cloud,
                    subagent_calls=subagent_calls,
                )

            messages.append({
                "role": "assistant",
                "content": response.text or "",
                "tool_calls": [
                    # ВАЖНО: arguments передаём СЛОВАРЁМ. Клиент Ollama валидирует
                    # это поле как dict и отклоняет JSON-строку (проверено на практике).
                    {"type": "function",
                     "function": {"name": f"{c.tool}__{c.action}", "arguments": c.args}}
                    for c in calls
                ],
            })

            for call in calls:
                if call.tool == "local_agent":
                    # agents-as-tools в обратную сторону: облачный судья
                    # переспрашивает локальную модель
                    question = str(call.args.get("question") or "")
                    proposer_id = str(call.args.get("proposer_id") or "")
                    self._emit("subagent_call", proposer_id=proposer_id, question=question[:200])
                    if self.subagent_runner is None:
                        content = "Суб-агенты отключены в настройках."
                    else:
                        try:
                            content = self.subagent_runner(proposer_id, question)
                        except Exception as exc:  # noqa: BLE001
                            content = f"Ошибка суб-агента: {type(exc).__name__}: {exc}"
                    subagent_calls.append(f"{proposer_id or 'auto'}: {question[:120]}")
                    self._emit("subagent_result", proposer_id=proposer_id,
                               preview=content[:200])
                    messages.append({"role": "tool", "name": "local_agent__ask",
                                     "content": content[:4000]})
                    continue

                if self.registry is None:
                    messages.append({"role": "tool", "name": f"{call.tool}__{call.action}",
                                     "content": "Инструменты отключены в настройках."})
                    continue

                self._emit("tool_call", tool=call.tool, action=call.action, args=call.args)
                result = self.registry.call(call)
                self._emit("tool_result", tool=call.tool, action=call.action,
                           ok=result.ok, summary=result.summary or result.error)
                from agents.proposer import _format_tool_result
                messages.append({"role": "tool", "name": f"{call.tool}__{call.action}",
                                 "content": _format_tool_result(result)})
        return None


def _split_answer(text: str) -> tuple[str, str]:
    """Разобрать формат «ОТВЕТ: ... ОБОСНОВАНИЕ: ...»."""
    if not text:
        return "", ""
    upper = text.upper()
    answer, rationale = text.strip(), ""
    if "ОТВЕТ:" in upper:
        start = upper.index("ОТВЕТ:") + len("ОТВЕТ:")
        rest = text[start:]
        rest_upper = rest.upper()
        if "ОБОСНОВАНИЕ:" in rest_upper:
            cut = rest_upper.index("ОБОСНОВАНИЕ:")
            answer = rest[:cut].strip()
            rationale = rest[cut + len("ОБОСНОВАНИЕ:"):].strip()
        else:
            answer = rest.strip()
    elif "ОБОСНОВАНИЕ:" in upper:
        cut = upper.index("ОБОСНОВАНИЕ:")
        answer = text[:cut].strip()
        rationale = text[cut + len("ОБОСНОВАНИЕ:"):].strip()
    return answer.strip(), rationale.strip()
