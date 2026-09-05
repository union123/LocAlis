"""
Инструмент «Локальные агенты».

Превращает локальные модели в инструменты: любая модель (в том числе
облачный арбитр) может поручить работу конкретной локальной модели,
спросить мнение нескольких моделей и сравнить ответы, либо разбить задачу
на подзадачи и распределить их.

Почему это инструмент, а не узел графа: узлы графа фиксированы и одинаковы
для всех задач, а здесь решение «кого спросить» принимает сама модель по
ходу работы. Это и есть agents-as-tools.

Защита от рекурсии: под-агент получает урезанный набор инструментов, из
которого исключён сам local_agents. Иначе агенты начали бы вызывать друг
друга бесконечно, забив VRAM.
"""

from __future__ import annotations

import concurrent.futures
from typing import Any

from core.base_tool import BaseTool, ToolAction
from core.schemas import ToolStatus
from core.tool_context import (
    ASK_LOCAL_MODEL,
    EMIT_EVENT,
    GET_TASK_HISTORY,
    LIST_LOCAL_MODELS,
    RECALL,
    RECALL_FACT,
    REMEMBER,
    REMEMBER_FACT,
    RUN_LOCAL_AGENT,
    SEARCH_FACTS,
    SEARCH_PAST,
)


class LocalAgentsTool(BaseTool):
    """Локальные модели как вызываемые агенты."""

    name = "local_agents"
    version = "1.0.0"
    description = (
        "Поручить работу локальной модели-агенту: выполнить подзадачу, дать "
        "второе мнение, обсудить спорный вопрос, разбить задачу на части."
    )
    # Инструменту нужны модели платформы — без контекста он бесполезен
    requires_context = True

    # ---- готовность ------------------------------------------------------

    def health(self) -> tuple[ToolStatus, str]:
        if self.capability(ASK_LOCAL_MODEL) is None:
            return ToolStatus.UNAVAILABLE, (
                "Нет доступа к локальным моделям: инструмент запущен без контекста платформы"
            )
        lister = self.capability(LIST_LOCAL_MODELS)
        if lister is None:
            return ToolStatus.READY, "Локальные модели доступны"
        try:
            models = lister()
        except Exception as exc:  # noqa: BLE001
            return ToolStatus.ERROR, f"Не удалось получить список моделей: {exc}"
        if not models:
            return ToolStatus.UNAVAILABLE, "В конфиге нет активных локальных моделей"
        return ToolStatus.READY, f"Доступно локальных агентов: {len(models)}"

    # ---- описание действий ----------------------------------------------

    def actions(self) -> list[ToolAction]:
        agent_id = {
            "type": "string",
            "description": (
                "Идентификатор локального агента (например proposer_a). "
                "Если не указан или неизвестен — выбирается первый доступный."
            ),
        }
        return [
            ToolAction(
                "list_agents",
                "Показать доступных локальных агентов: их идентификаторы, модели и сильные стороны.",
                {"type": "object", "properties": {}},
            ),
            ToolAction(
                "ask_agent",
                "Задать вопрос одной локальной модели и получить её ответ. "
                "Быстро: модель только рассуждает, инструменты не вызывает.",
                {
                    "type": "object",
                    "properties": {
                        "agent_id": agent_id,
                        "question": {"type": "string",
                                     "description": "Вопрос или задание по-русски"},
                        "role": {"type": "string",
                                 "description": "Роль агента, например «геолог-эксперт», "
                                                "«критик», «редактор»"},
                    },
                    "required": ["question"],
                },
            ),
            ToolAction(
                "run_agent",
                "Поручить локальной модели самостоятельную задачу С ДОСТУПОМ к инструментам "
                "платформы (файлы, геоданные). Медленнее ask_agent, но агент может добыть факты.",
                {
                    "type": "object",
                    "properties": {
                        "agent_id": agent_id,
                        "task": {"type": "string", "description": "Формулировка задачи"},
                        "tools": {"type": "array", "items": {"type": "string"},
                                  "description": "Какие инструменты разрешить, например [\"qgis\"]"},
                    },
                    "required": ["task"],
                },
            ),
            ToolAction(
                "second_opinion",
                "Спросить НЕСКОЛЬКО локальных моделей об одном и том же и сравнить ответы. "
                "Используй при сомнениях: покажет, согласны модели или расходятся.",
                {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "Вопрос для всех моделей"},
                        "agents": {"type": "array", "items": {"type": "string"},
                                   "description": "Кого спросить; пусто — всех доступных"},
                    },
                    "required": ["question"],
                },
            ),
            ToolAction(
                "debate",
                "Организовать обсуждение: первая модель предлагает решение, вторая критикует, "
                "первая учитывает критику. Подходит для спорных выводов.",
                {
                    "type": "object",
                    "properties": {
                        "question": {"type": "string", "description": "Предмет обсуждения"},
                        "proposer": {"type": "string", "description": "Кто предлагает решение"},
                        "critic": {"type": "string", "description": "Кто критикует"},
                    },
                    "required": ["question"],
                },
            ),
            ToolAction(
                "delegate_subtasks",
                "Разбить работу: раздать разные подзадачи разным локальным моделям "
                "и собрать их ответы вместе. Подзадачи выполняются параллельно.",
                {
                    "type": "object",
                    "properties": {
                        "subtasks": {
                            "type": "array",
                            "description": "Список подзадач",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "agent_id": agent_id,
                                    "task": {"type": "string", "description": "Текст подзадачи"},
                                    "use_tools": {"type": "boolean",
                                                  "description": "Разрешить инструменты платформы"},
                                },
                                "required": ["task"],
                            },
                        },
                    },
                    "required": ["subtasks"],
                },
            ),
            ToolAction(
                "remember",
                "Сохранить промежуточный вывод в рабочую память задачи, "
                "чтобы другие агенты могли им воспользоваться.",
                {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Короткое имя записи"},
                        "value": {"type": "string", "description": "Что запомнить"},
                    },
                    "required": ["key", "value"],
                },
            ),
            ToolAction(
                "recall",
                "Прочитать рабочую память задачи: конкретную запись или все сразу.",
                {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string",
                                "description": "Имя записи; пусто — вернуть все"},
                    },
                },
            ),
            ToolAction(
                "remember_fact",
                "Сохранить факт в ДОЛГОВЕЧНУЮ память (переживает перезапуск). "
                "Используй для устойчивых знаний: рабочие папки, конвенции, "
                "предпочтения пользователя, проверенные выводы.",
                {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "description": "Короткое имя факта"},
                        "value": {"type": "string", "description": "Что запомнить"},
                    },
                    "required": ["key", "value"],
                },
            ),
            ToolAction(
                "recall_fact",
                "Прочитать долговечную память: конкретный факт или все сразу.",
                {
                    "type": "object",
                    "properties": {
                        "key": {"type": "string",
                                "description": "Имя факта; пусто — вернуть все"},
                    },
                },
            ),
            ToolAction(
                "search_facts",
                "Найти в долговечной памяти факты по текстовому запросу.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Что ищем"},
                        "top_k": {"type": "integer", "description": "Сколько вернуть"},
                    },
                    "required": ["query"],
                },
            ),
            ToolAction(
                "search_past",
                "Найти похожие ПРОШЛЫЕ решения задач по текстовому запросу. "
                "Полезно, если похожая задача уже решалась — можно переиспользовать.",
                {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Вопрос или тема"},
                        "top_k": {"type": "integer", "description": "Сколько вернуть"},
                    },
                    "required": ["query"],
                },
            ),
            ToolAction(
                "get_task_history",
                "Прочитать РЕАЛЬНЫЙ журнал прошлых задач платформы: список задач, "
                "их статус (done/failed), и для сбойных — вызовы инструментов с "
                "ошибками. Используй для анализа истории выполнения, поиска "
                "повторяющихся проблем, самосовершенствования. Не ищи файл .log — "
                "журнал живёт в базе платформы.",
                {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer",
                                  "description": "Сколько задач вернуть (по умолчанию 15)"},
                        "status": {"type": "string",
                                   "description": "Фильтр: done / failed / running / created"},
                    },
                },
            ),
        ]

    # ---- служебное -------------------------------------------------------

    def _agents(self) -> list[dict[str, Any]]:
        lister = self.capability(LIST_LOCAL_MODELS)
        if lister is None:
            return []
        try:
            return list(lister() or [])
        except Exception:  # noqa: BLE001
            return []

    def _resolve(self, agent_id: str | None) -> str:
        """
        Выбрать существующего агента.

        Модели часто присылают неточный идентификатор («proposer A», «qwen»),
        поэтому сопоставляем и по имени модели, и по подстроке, и только потом
        берём первого доступного.
        """
        agents = self._agents()
        if not agents:
            return ""
        ids = [str(a.get("id", "")) for a in agents]
        wanted = (agent_id or "").strip()
        if not wanted:
            return ids[0]
        if wanted in ids:
            return wanted
        lowered = wanted.lower().replace(" ", "_")
        for agent in agents:
            if str(agent.get("id", "")).lower() == lowered:
                return str(agent["id"])
        for agent in agents:
            model = str(agent.get("model", "")).lower()
            if lowered in model or model.startswith(lowered):
                return str(agent["id"])
        for agent in agents:
            if lowered in str(agent.get("id", "")).lower():
                return str(agent["id"])
        return ids[0]

    def _emit(self, kind: str, **payload: Any) -> None:
        emitter = self.capability(EMIT_EVENT)
        if emitter is None:
            return
        try:
            emitter({"kind": kind, "actor": "local_agents", **payload})
        except Exception:  # noqa: BLE001 — интерфейс не должен ломать работу
            pass

    def _ask(self, agent_id: str, question: str, system: str = "") -> dict[str, Any]:
        ask = self.require(ASK_LOCAL_MODEL)
        self._emit("subagent_call", proposer_id=agent_id, question=question[:200])
        result = ask(agent_id, question, system) or {}
        self._emit("subagent_result", proposer_id=agent_id,
                   preview=str(result.get("text") or result.get("error") or "")[:200])
        return result

    @staticmethod
    def _role_system(role: str | None) -> str:
        base = ("Ты локальный агент-исполнитель. Отвечай по-русски, кратко и по делу. "
                "Не выдумывай факты о файлах и данных: если их нет, скажи об этом прямо.")
        if role:
            return f"Твоя роль: {role}. {base}"
        return base

    # ---- выполнение ------------------------------------------------------

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Неизвестное действие: {action}"}
        return handler(args)

    def _do_list_agents(self, args: dict[str, Any]) -> dict[str, Any]:
        agents = self._agents()
        if not agents:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Локальные агенты недоступны (нет активных моделей в конфиге)"}
        listing = "; ".join(f"{a['id']} ({a.get('model', '?')})" for a in agents)
        return {"ok": True, "data": {"agents": agents, "count": len(agents)},
                "summary": f"Доступно агентов: {len(agents)} — {listing}"}

    def _do_ask_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return {"ok": False, "data": {}, "summary": "", "error": "Не указан вопрос"}
        agent_id = self._resolve(args.get("agent_id"))
        if not agent_id:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нет доступных локальных агентов"}
        result = self._ask(agent_id, question, self._role_system(args.get("role")))
        if not result.get("ok"):
            return {"ok": False, "data": {"agent_id": agent_id}, "summary": "",
                    "error": f"Агент {agent_id} не ответил: {result.get('error')}"}
        text = str(result.get("text") or "")
        return {
            "ok": True,
            "data": {"agent_id": agent_id, "model": result.get("model"),
                     "answer": text, "latency_ms": result.get("latency_ms", 0)},
            "summary": f"Агент {agent_id} ответил: {text[:400]}",
        }

    def _do_run_agent(self, args: dict[str, Any]) -> dict[str, Any]:
        task = str(args.get("task") or "").strip()
        if not task:
            return {"ok": False, "data": {}, "summary": "", "error": "Не указана задача"}
        runner = self.capability(RUN_LOCAL_AGENT)
        if runner is None or not self.config.get("allow_nested_tools", True):
            # Инструменты под-агентам запрещены — работаем в режиме рассуждения
            fallback = self._do_ask_agent({"agent_id": args.get("agent_id"), "question": task})
            if fallback.get("ok"):
                fallback["summary"] = ("Инструменты под-агентам отключены, получен ответ "
                                       "без обращения к данным. ") + fallback["summary"]
            return fallback

        agent_id = self._resolve(args.get("agent_id"))
        tools = args.get("tools")
        if isinstance(tools, str):
            tools = [t.strip() for t in tools.split(",") if t.strip()]
        self._emit("subagent_call", proposer_id=agent_id, question=task[:200])
        try:
            result = runner(agent_id, task, tools) or {}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "data": {"agent_id": agent_id}, "summary": "",
                    "error": f"Сбой под-агента {agent_id}: {type(exc).__name__}: {exc}"}
        self._emit("subagent_result", proposer_id=agent_id,
                   preview=str(result.get("text") or result.get("error") or "")[:200])
        if not result.get("ok"):
            return {"ok": False, "data": {"agent_id": agent_id}, "summary": "",
                    "error": f"Агент {agent_id} не справился: {result.get('error')}"}
        text = str(result.get("text") or "")
        facts = result.get("tool_facts") or []
        summary = f"Агент {agent_id} выполнил задачу: {text[:350]}"
        if facts:
            summary += f" | Использованы инструменты: {'; '.join(facts[:3])}"
        return {"ok": True, "summary": summary,
                "data": {"agent_id": agent_id, "model": result.get("model"),
                         "answer": text, "tool_facts": facts}}

    def _do_second_opinion(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return {"ok": False, "data": {}, "summary": "", "error": "Не указан вопрос"}

        available = [str(a["id"]) for a in self._agents()]
        if not available:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нет доступных локальных агентов"}
        requested = args.get("agents") or []
        if isinstance(requested, str):
            requested = [requested]
        chosen = [self._resolve(a) for a in requested] if requested else available
        # Убираем дубли, сохраняя порядок, и ограничиваем число участников
        limit = int(self.config.get("max_debate_participants", 3))
        chosen = list(dict.fromkeys(chosen))[:max(1, limit)]

        system = self._role_system("независимый эксперт")
        answers: dict[str, dict[str, Any]] = {}
        # Параллельно: Ollama сама сериализует загрузку моделей в VRAM
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(chosen)) as pool:
            futures = {pool.submit(self._ask, aid, question, system): aid for aid in chosen}
            for future in concurrent.futures.as_completed(futures):
                aid = futures[future]
                try:
                    answers[aid] = future.result()
                except Exception as exc:  # noqa: BLE001
                    answers[aid] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        good = {aid: str(r.get("text") or "") for aid, r in answers.items()
                if r.get("ok") and str(r.get("text") or "").strip()}
        failed = [aid for aid in chosen if aid not in good]
        if not good:
            return {"ok": False, "data": {"failed": failed}, "summary": "",
                    "error": "Ни один локальный агент не дал ответа"}

        agreement = _rough_agreement(list(good.values()))
        verdict = ("мнения совпадают" if agreement >= 0.8
                   else "мнения частично расходятся" if agreement >= 0.5
                   else "мнения существенно расходятся")
        lines = [f"[{aid}] {text[:300]}" for aid, text in good.items()]
        return {
            "ok": True,
            "data": {"answers": good, "failed": failed,
                     "agreement": round(agreement, 2), "verdict": verdict},
            "summary": (f"Опрошено агентов: {len(good)} из {len(chosen)}; {verdict} "
                        f"(схожесть {agreement:.2f}). " + " || ".join(lines)),
        }

    def _do_debate(self, args: dict[str, Any]) -> dict[str, Any]:
        question = str(args.get("question") or "").strip()
        if not question:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Не указан предмет обсуждения"}
        available = [str(a["id"]) for a in self._agents()]
        if not available:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нет доступных локальных агентов"}

        proposer = self._resolve(args.get("proposer"))
        critic = self._resolve(args.get("critic"))
        # Критик обязан отличаться от автора, иначе критика бессмысленна
        if critic == proposer:
            other = [a for a in available if a != proposer]
            if not other:
                return {"ok": False, "data": {}, "summary": "",
                        "error": "Для обсуждения нужны минимум две разные модели"}
            critic = other[0]

        first = self._ask(proposer, question, self._role_system("автор решения"))
        if not first.get("ok"):
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Автор {proposer} не ответил: {first.get('error')}"}
        proposal = str(first.get("text") or "")

        critique_prompt = (
            f"ВОПРОС: {question}\n\nПРЕДЛОЖЕННОЕ РЕШЕНИЕ:\n{proposal[:2500]}\n\n"
            "Найди слабые места, ошибки и непроверенные допущения. Если решение верное — "
            "скажи об этом прямо и коротко. Не переписывай решение целиком."
        )
        second = self._ask(critic, critique_prompt, self._role_system("строгий критик"))
        critique = str(second.get("text") or "") if second.get("ok") else ""

        revision = proposal
        if critique:
            revise_prompt = (
                f"ВОПРОС: {question}\n\nТВОЁ РЕШЕНИЕ:\n{proposal[:2000]}\n\n"
                f"КРИТИКА:\n{critique[:1500]}\n\n"
                "Дай итоговый ответ с учётом обоснованной критики. Если критика неверна — "
                "объясни, почему остаёшься при своём."
            )
            third = self._ask(proposer, revise_prompt, self._role_system("автор решения"))
            if third.get("ok") and str(third.get("text") or "").strip():
                revision = str(third["text"])

        return {
            "ok": True,
            "data": {"proposer": proposer, "critic": critic, "proposal": proposal,
                     "critique": critique, "final": revision},
            "summary": (f"Обсуждение {proposer} против {critic}. Итог после критики: "
                        f"{revision[:400]}"),
        }

    def _do_delegate_subtasks(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = args.get("subtasks") or []
        if isinstance(raw, dict):
            raw = [raw]
        if isinstance(raw, str):
            raw = [{"task": raw}]
        subtasks = [s for s in raw if isinstance(s, dict) and str(s.get("task") or "").strip()]
        if not subtasks:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Не переданы подзадачи (ожидается список объектов с полем task)"}

        limit = int(self.config.get("max_subtasks", 4))
        skipped = max(0, len(subtasks) - limit)
        subtasks = subtasks[:limit]

        available = [str(a["id"]) for a in self._agents()]
        if not available:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нет доступных локальных агентов"}

        def run_one(index: int, item: dict[str, Any]) -> dict[str, Any]:
            task = str(item["task"]).strip()
            # Если агент не указан — раскладываем подзадачи по кругу,
            # чтобы не грузить одну модель всем объёмом работы
            agent_id = (self._resolve(item.get("agent_id")) if item.get("agent_id")
                        else available[index % len(available)])
            if item.get("use_tools"):
                result = self._do_run_agent({"agent_id": agent_id, "task": task})
            else:
                result = self._do_ask_agent({"agent_id": agent_id, "question": task})
            return {"index": index, "task": task, "agent_id": agent_id,
                    "ok": bool(result.get("ok")),
                    "answer": (result.get("data") or {}).get("answer", ""),
                    "error": result.get("error")}

        results: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(subtasks)) as pool:
            futures = [pool.submit(run_one, i, s) for i, s in enumerate(subtasks)]
            for future in concurrent.futures.as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as exc:  # noqa: BLE001
                    results.append({"ok": False, "error": f"{type(exc).__name__}: {exc}",
                                    "answer": "", "task": "", "agent_id": "", "index": -1})
        results.sort(key=lambda r: r["index"])

        done = [r for r in results if r["ok"]]
        parts = [f"{r['index'] + 1}) [{r['agent_id']}] {str(r['answer'])[:250]}" for r in done]
        failures = [f"{r['index'] + 1}) {r.get('error')}" for r in results if not r["ok"]]
        summary = f"Выполнено подзадач: {len(done)} из {len(results)}. " + " | ".join(parts)
        if failures:
            summary += f" | Неудачи: {'; '.join(failures)}"
        if skipped:
            summary += f" | Пропущено сверх лимита: {skipped}"
        return {"ok": bool(done), "data": {"results": results, "completed": len(done)},
                "summary": summary,
                "error": None if done else "Ни одна подзадача не выполнена"}

    def _do_remember(self, args: dict[str, Any]) -> dict[str, Any]:
        key = str(args.get("key") or "").strip()
        value = str(args.get("value") or "").strip()
        if not key or not value:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нужны непустые key и value"}
        self.require(REMEMBER)(key, value)
        return {"ok": True, "data": {"key": key},
                "summary": f"Запомнено под именем '{key}' ({len(value)} символов)"}

    def _do_recall(self, args: dict[str, Any]) -> dict[str, Any]:
        recall = self.require(RECALL)
        key = str(args.get("key") or "").strip() or None
        value = recall(key)
        if key is None:
            data = dict(value or {})
            if not data:
                return {"ok": True, "data": {"memory": {}},
                        "summary": "Рабочая память пуста"}
            listing = "; ".join(f"{k}: {str(v)[:120]}" for k, v in data.items())
            return {"ok": True, "data": {"memory": data},
                    "summary": f"В памяти записей: {len(data)}. {listing}"}
        if not value:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"В памяти нет записи '{key}'"}
        return {"ok": True, "data": {"key": key, "value": value},
                "summary": f"{key}: {str(value)[:400]}"}

    # ---- долговечная память (между задачами) ----------------------------

    def _do_remember_fact(self, args: dict[str, Any]) -> dict[str, Any]:
        fn = self.require(REMEMBER_FACT)
        key = str(args.get("key") or "").strip()
        value = str(args.get("value") or "").strip()
        if not key or not value:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нужны непустые key и value"}
        fn(key, value)
        return {"ok": True, "data": {"key": key},
                "summary": f"Факт запомнен навсегда: '{key}' ({len(value)} символов)"}

    def _do_recall_fact(self, args: dict[str, Any]) -> dict[str, Any]:
        fn = self.require(RECALL_FACT)
        key = str(args.get("key") or "").strip() or None
        data = fn(key)
        if key is None:
            if not data:
                return {"ok": True, "data": {"facts": {}},
                        "summary": "Долговечная память пуста"}
            listing = "; ".join(f"{k}: {str(v)[:120]}" for k, v in data.items())
            return {"ok": True, "data": {"facts": data},
                    "summary": f"Фактов: {len(data)}. {listing}"}
        if key not in data:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Факт '{key}' не найден"}
        return {"ok": True, "data": {"key": key, "value": data[key]},
                "summary": f"{key}: {str(data[key])[:400]}"}

    def _do_search_facts(self, args: dict[str, Any]) -> dict[str, Any]:
        fn = self.require(SEARCH_FACTS)
        query = str(args.get("query") or "").strip()
        top_k = int(args.get("top_k") or 5)
        if not query:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нужен непустой query"}
        results = fn(query, top_k=top_k)
        if not results:
            return {"ok": True, "data": {"results": []},
                    "summary": f"По запросу '{query}' факты не найдены"}
        listing = "; ".join(f"{r['key']}: {str(r['value'])[:100]}" for r in results)
        return {"ok": True, "data": {"results": results},
                "summary": f"Найдено фактов: {len(results)}. {listing}"}

    def _do_search_past(self, args: dict[str, Any]) -> dict[str, Any]:
        fn = self.require(SEARCH_PAST)
        query = str(args.get("query") or "").strip()
        top_k = int(args.get("top_k") or 3)
        if not query:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нужен непустой query"}
        results = fn(query, top_k=top_k)
        if not results:
            return {"ok": True, "data": {"results": []},
                    "summary": f"По запросу '{query}' прошлые решения не найдены"}
        listing = "; ".join(
            f"[{r.get('mode','')}] {r['prompt'][:60]}: {r['answer'][:80]}"
            for r in results)
        return {"ok": True, "data": {"results": results},
                "summary": f"Похожих решений: {len(results)}. {listing}"}

    def _do_get_task_history(self, args: dict[str, Any]) -> dict[str, Any]:
        """Журнал прошлых задач из Blackboard (реальный, не файл .log)."""
        fn = self.require(GET_TASK_HISTORY)
        limit = max(1, int(args.get("limit") or 15))
        status = str(args.get("status") or "").strip() or None
        data = fn(limit=limit, status=status)
        tasks = data.get("tasks") or []
        if not tasks:
            return {"ok": True, "data": data,
                    "summary": "Журнал задач пуст (или ошибка чтения)"}
        lines = [f"Задач в журнале: {len(tasks)}"]
        for t in tasks:
            cloud = " [облако]" if t.get("used_cloud") else ""
            lines.append(f"- {str(t.get('created_at',''))[:16]} {t.get('status','?')} "
                         f"{t.get('mode','?')}{cloud} {t.get('task_id','')[:8]} "
                         f"-- {str(t.get('prompt',''))[:70]}")
        for tc in (data.get("tool_calls") or []):
            lines.append(f"  Сбойные вызовы задачи {tc.get('task_id','')[:8]} "
                         f"({tc.get('errors')} ошибок из {tc.get('total')}):")
            for s in (tc.get("sample") or []):
                okc = "OK" if s.get("ok") else "ОШИБКА"
                lines.append(f"    {s.get('tool')}.{s.get('action')} [{okc}] "
                             f"{str(s.get('text'))[:100]}")
        return {"ok": True, "data": data,
                "summary": "\n".join(lines)[:1900]}


def _rough_agreement(answers: list[str]) -> float:
    """
    Грубая оценка согласия между ответами (0..1).

    Сравниваем пересечение значимых слов: для задачи «согласны ли модели»
    этого достаточно, а полноценную сверку делает Verifier.
    """
    import re
    if len(answers) < 2:
        return 1.0
    sets = []
    for answer in answers:
        words = re.findall(r"[\w\d]+", (answer or "").lower().replace("ё", "е"))
        sets.append({w for w in words if len(w) > 3})
    scores = []
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            if not union:
                continue
            scores.append(len(sets[i] & sets[j]) / len(union))
    return sum(scores) / len(scores) if scores else 0.0
