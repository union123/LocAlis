"""
Team Agent (24.08) — реализация изначальной задумки платформы:
модели СОВЕЩАЮТСЯ, распределяют обязанности и ВЗАИМНО проверяют друг друга.

Три фазы:
  1. Совещание: N раундов обмена планами между всеми моделями, затем
     консенсус-план (арбитр сводит при тупом расхождении).
  2. Выполнение: по консенсус-плану (обычный путь оркестратора).
  3. Крест-накрест: каждый проверяет чужую часть по фактам с диска,
     проблемы возвращаются авторам на исправление (1 автоцикл).

«Стол переговоров» — ScratchMemory платформы: все реплики совещания и
ревью пишутся туда и доступны моделям через local_agents (recall).
"""

from __future__ import annotations

import time
from typing import Any, Callable

from core.llm_gateway import LLMGateway, ModelSpec


TEAM_VERSION = "1.0"

_MEETING_SYSTEM = (
    "Ты участник команды из трёх ИИ-агентов, готовящих план задачи. "
    "Твоя задача — предложить ЛУЧШИЙ план выполнения и учесть замечания "
    "коллег из предыдущих раундов. Отвечай по-русски, кратко и по делу."
)

_MEETING_USER_FIRST = (
    "ЗАДАЧА КОМАНДЫ:\n{task}\n\n"
    "ДОСТУПНЫЕ ИНСТРУМЕНТЫ:\n{tools}\n\n"
    "Предложи план выполнения: какие файлы создать, в каком порядке, "
    "какие инструменты использовать на каждом шаге. Учти все требования "
    "задачи. Ответ — структурированный список шагов с обоснованием."
)

_MEETING_USER_ROUND = (
    "ЗАДАЧА КОМАНДЫ:\n{task}\n\n"
    "ПЛАНЫ КОЛЛЕГ ИЗ ПРЕДЫДУЩЕГО РАУНДА:\n{peers}\n\n"
    "ТВОЙ ПЛАН ИЗ ПРЕДЫДУЩЕГО РАУНДА:\n{mine}\n\n"
    "Проанализируй планы коллег: что у них лучше, что хуже, какие ошибки "
    "ты видишь? Улучши СВОЙ план с учётом лучших идей коллег и явных "
    "ошибок в их вариантах. Ответ — улучшенный план + краткие замечания "
    "к коллегам."
)

_CONSENSUS_SYSTEM = (
    "Ты арбитр команды из трёх ИИ-агентов. Каждый предложил свой план "
    "выполнения задачи. Твоя задача — свести их в ЕДИНЫЙ финальный план: "
    "возьми лучшее из каждого, устрани противоречия, не потеряй ни одного "
    "требования задачи. Отвечай кратко."
)

_CROSS_REVIEW_SYSTEM = (
    "Ты проверяющий в команде ИИ-агентов. Тебе даны: задание, список "
    "файлов, созданных коллегой, и фактическое содержимое этих файлов "
    "с диска. Найди несоответствия: пропущенные файлы, несоответствие "
    "заданию, битые импорты, заглушки, неверные версии.\n\n"
    "ВАЖНО: требования ЗАДАНИЯ — единственный критерий. НЕ выдумывай "
    "дополнительные правила, которых нет в задании (пример ошибки: "
    "задание требовало zone.js ^0.15.0, проверяющий забраковал его, "
    "сославшись на своё представление об Angular 22). Проверяй только "
    "то, что явно требуется заданием.\n\n"
    "Ответ — ТОЛЬКО JSON, без текста вокруг:\n"
    '{"clean": true|false, "issues": ["файл: проблема", ...]}\n'
    'clean=true — всё соответствует заданию. issues — конкретные проблемы '
    '(каждая: файл + что не так).'
)



def _parse_review(text: str) -> tuple[bool, list[str]]:
    """
    Разобрать ответ проверяющего. Ожидается JSON {"clean": bool, "issues": [...]}.
    Fallback для не-JSON ответа: пустой/«OK»-подобный = чисто, иначе — весь
    текст одной проблемой. Хрупкий «startswith OK» заменён на это (критика
    ревью 25.08, риск #5).
    """
    import json as _json
    if not text:
        return True, []
    try:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = _json.loads(text[start:end + 1])
            clean = bool(data.get("clean"))
            issues = [str(i) for i in (data.get("issues") or []) if str(i).strip()]
            if clean:
                return True, []
            return False, issues or [text[:500]]
    except (ValueError, TypeError):
        pass
    lowered = text.lower().strip()
    if lowered.startswith(("ok", "ок", "всё в порядке", "все в порядке")):
        return True, []
    return False, [text[:500]]


class TeamMeeting:
    """
    Один прогон «Команды»: совещание + (выполнение снаружи) + крест-накрест.

    Класс не знает о графе и Blackboard — только модели и колбэки.
    """

    version = TEAM_VERSION

    # Жёсткий потолок раундов: защита от передачи огромных значений вызывающим
    MAX_ROUNDS = 3
    # Таймаут одного вызова модели, сек (зависший вызов Ollama не должен
    # останавливать совещание навсегда — задача d1d0e9c3, 24.08)
    CHAT_TIMEOUT_SEC = 240

    def __init__(
        self,
        gateway: LLMGateway,
        specs: list[ModelSpec],
        on_event: Callable[[dict[str, Any]], None] | None = None,
        meeting_rounds: int = 1,
        task_mode: str | None = None,
    ):
        self.gateway = gateway
        self.specs = specs
        self.on_event = on_event
        self.meeting_rounds = max(1, min(int(meeting_rounds), self.MAX_ROUNDS))
        self.task_mode = task_mode

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            try:
                self.on_event({"kind": kind, "component": "team", **payload})
            except Exception:  # noqa: BLE001
                pass

    # ---- фаза 1: совещание -------------------------------------------------

    def meeting(
        self,
        task_prompt: str,
        tools_text: str,
        context_summary: str = "",
    ) -> dict[str, Any]:
        """
        Провести раунды совещания. Возвращает:
        {
          "proposals": {spec_id: текст плана},
          "consensus_text": текст консенсуса от арбитра,
          "rounds_log": [ {round, speaker, text} ... ]
        }
        """
        proposals: dict[str, str] = {}
        rounds_log: list[dict[str, Any]] = []

        for rnd in range(1, self.meeting_rounds + 1):
            self._emit("meeting_round", round=rnd, of=self.meeting_rounds)
            new_proposals: dict[str, str] = {}

            for spec in self.specs:
                if rnd == 1:
                    user = (_MEETING_USER_FIRST.format(
                        task=task_prompt[:4000], tools=tools_text[:1500])
                        + (f"\n\nКОНТЕКСТ:\n{context_summary[:800]}"
                           if context_summary else ""))
                else:
                    peers = "\n\n".join(
                        f"— {sid}:\n{text[:2500]}"
                        for sid, text in proposals.items() if sid != spec.id)
                    mine = proposals.get(spec.id, "(нет варианта)")
                    user = _MEETING_USER_ROUND.format(
                        task=task_prompt[:4000], peers=peers[:7500],
                        mine=mine[:2000])

                response = self.gateway.chat(
                    spec,
                    [{"role": "system", "content": _MEETING_SYSTEM},
                     {"role": "user", "content": user}],
                    tools=None, task_mode=self.task_mode or "local_only",
                    timeout_sec=self.CHAT_TIMEOUT_SEC)
                text = response.text if response.ok else (
                    f"(модель {spec.model} не ответила: {response.error})")
                new_proposals[spec.id] = text
                rounds_log.append({"round": rnd, "speaker": spec.id,
                                   "text": text})  # полный текст — в журнал
                self._emit("meeting_speech", round=rnd, speaker=spec.id,
                           preview=text[:200], full_text=text)

            proposals = new_proposals

        consensus_text = self._consensus(task_prompt, proposals)
        return {"proposals": proposals, "consensus_text": consensus_text,
                "rounds_log": rounds_log}

    def _consensus(self, task_prompt: str,
                   proposals: dict[str, str]) -> str:
        """Арбитр (первая спецификация) сводит планы в консенсус."""
        peers = "\n\n".join(
            f"=== ПЛАН ОТ {sid} ===\n{text[:3000]}"
            for sid, text in proposals.items())
        user = (
            f"ЗАДАЧА:\n{task_prompt[:3000]}\n\n"
            f"ПЛАНЫ КОМАНДЫ:\n{peers}\n\n"
            "Сведи эти планы в ЕДИНЫЙ финальный план: общий подход, "
            "список файлов, порядок работы, распределение шагов. "
            "Отметь спорные места и как они решены."
        )
        arbiter = self.specs[0]
        response = self.gateway.chat(
            arbiter,
            [{"role": "system", "content": _CONSENSUS_SYSTEM},
             {"role": "user", "content": user}],
            tools=None, task_mode=self.task_mode or "local_only",
            timeout_sec=self.CHAT_TIMEOUT_SEC)
        if response.ok and (response.text or "").strip():
            return response.text.strip()
        # Арбитр не ответил — консенсусом считаем план первой модели
        self._emit("consensus_fallback", reason=response.error or "пустой ответ")
        return next(iter(proposals.values()), "")

    # ---- фаза 3: крест-накрест ---------------------------------------------

    def cross_review(
        self,
        task_prompt: str,
        created_files: list[dict[str, Any]],
        read_fn: Callable[[str], str],
    ) -> dict[str, Any]:
        """
        Взаимная проверка: каждый проверяет файлы, созданные ДРУГИМ.

        created_files: [{"path": ..., "author": <spec_id>}, ...]
        read_fn(path) -> содержимое файла (факты с диска, не текст отчёта).

        Возвращает {"issues": [{file, reviewer, comment}], "clean": bool}
        """
        by_author: dict[str, list[dict[str, Any]]] = {}
        for f in created_files:
            by_author.setdefault(f.get("author") or "?", []).append(f)

        issues: list[dict[str, Any]] = []
        authors = list(by_author.keys())
        if len(authors) < 2:
            # Один автор — проверяет второй спецификатор
            reviewer_pool = [s.id for s in self.specs if s.id not in by_author]
            if not reviewer_pool:
                return {"issues": [], "clean": True}
            authors_reviewers = [(a, reviewer_pool[0]) for a in authors]
        else:
            # Крест-накрест: каждый проверяет следующего по кругу
            authors_reviewers = [
                (authors[i], authors[(i + 1) % len(authors)])
                for i in range(len(authors))
            ]

        for author, reviewer_id in authors_reviewers:
            files = by_author[author]
            reviewer_spec = next((s for s in self.specs
                                  if s.id == reviewer_id), self.specs[0])
            files_text = ""
            for f in files[:6]:  # не больше 6 файлов на проверяющего
                content = read_fn(f["path"])
                files_text += (f"\n=== {f['path']} ===\n"
                               f"{(content or '')[:2500]}\n")

            user = (f"ЗАДАНИЕ КОМАНДЫ:\n{task_prompt[:2500]}\n\n"
                    f"ФАЙЛЫ, СОЗДАННЫЕ КОЛЛЕГОЙ ({author}):\n{files_text}\n\n"
                    "Найди несоответствия заданию и дефекты. Конкретно: "
                    "файл + проблема. Если всё хорошо — ответь ровно OK.")

            response = self.gateway.chat(
                reviewer_spec,
                [{"role": "system", "content": _CROSS_REVIEW_SYSTEM},
                 {"role": "user", "content": user}],
                tools=None, task_mode=self.task_mode or "local_only",
                timeout_sec=self.CHAT_TIMEOUT_SEC)
            text = (response.text or "").strip() if response.ok else ""
            clean, issue_list = _parse_review(text)
            if not clean:
                # Одно ревью на пару проверяющий-автор (не дублируем на каждый
                # файл: задача ef78663c дала 18 одинаковых замечаний вместо 2-3)
                issues.append({"file": ", ".join(f["path"] for f in files[:3]),
                               "reviewer": reviewer_id,
                               "comment": "\n".join(issue_list)[:800]})
                self._emit("cross_review_issue", author=author,
                           reviewer=reviewer_id, preview="; ".join(issue_list)[:200],
                           full_text=text)

        return {"issues": issues, "clean": not issues}
