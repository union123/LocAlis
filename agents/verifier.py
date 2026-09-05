"""
Verifier (слой 6) — сверка ответов Proposers, голосование 2 из 3.

Двухступенчатая проверка (сначала дешёвая, потом дорогая):
  1. Дешёвая: нормализация текста + сравнение по схожести и по совпадению
     числовых/кодовых фактов (EPSG, количества). Явные совпадения — авто-принять,
     LLM не вызывается вовсе.
  2. Дорогая: локальная MoE-модель сверяет смысл, если текстово ответы разошлись.

Расхождение -> escalate_to_judge = True.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any

from core.llm_gateway import LLMGateway
from core.schemas import Proposal, Verdict

VERIFIER_VERSION = "1.0"

_SYSTEM_PROMPT = (
    "Ты проверяющий (Verifier). Тебе даны ответы нескольких моделей на одну задачу.\n"
    "Определи, какие ответы совпадают ПО СУТИ (различия формулировок не важны, "
    "различия в фактах, числах и кодах — важны).\n"
    "Верни ТОЛЬКО JSON:\n"
    '{"agreeing":["id1","id2"],"dissenting":["id3"],'
    '"consensus":true|false,"chosen":"id1","reason":"кратко по-русски"}\n'
    "consensus=true только если согласны минимум 2 ответа из 3."
)

# Факты, расхождение в которых недопустимо даже при похожем тексте
_FACT_PATTERNS = [
    re.compile(r"EPSG:\s*(\d{3,6})", re.IGNORECASE),
    re.compile(r"(\d+[.,]\d+|\d+)"),
]


def _normalize(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^\wа-я\d.,:-]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _facts(text: str) -> set[str]:
    """Числовые и кодовые факты из ответа — сравниваются строго."""
    found: set[str] = set()
    for pattern in _FACT_PATTERNS:
        for match in pattern.findall(text or ""):
            found.add(str(match).replace(",", "."))
    return found


class Verifier:
    """Голосование 2 из 3 по ответам Proposers."""

    version = VERIFIER_VERSION

    def __init__(self, gateway: LLMGateway, similarity_threshold: float = 0.82,
                 use_llm: bool = True):
        self.gateway = gateway
        self.threshold = similarity_threshold
        self.use_llm = use_llm

    # ---- дешёвая ступень -------------------------------------------------

    def cheap_vote(self, proposals: list[Proposal]) -> Verdict | None:
        """Кластеризация похожих ответов без вызова модели."""
        good = [p for p in proposals if p.ok and p.answer.strip()]
        if len(good) < 2:
            return None

        clusters: list[list[Proposal]] = []
        for proposal in good:
            placed = False
            for cluster in clusters:
                head = cluster[0]
                same_text = _similarity(head.answer, proposal.answer) >= self.threshold
                # Конфликт фактов запрещает объединение даже при похожем тексте
                facts_a, facts_b = _facts(head.answer), _facts(proposal.answer)
                conflict = bool(facts_a and facts_b and facts_a.isdisjoint(facts_b))
                if same_text and not conflict:
                    cluster.append(proposal)
                    placed = True
                    break
            if not placed:
                clusters.append([proposal])

        clusters.sort(key=len, reverse=True)
        best = clusters[0]
        if len(best) >= 2:
            agreeing = [p.proposer_id for p in best]
            dissenting = [p.proposer_id for p in good if p.proposer_id not in agreeing]
            return Verdict(
                consensus=True, agreeing=agreeing, dissenting=dissenting,
                chosen_answer=best[0].answer, escalate_to_judge=False,
                reason=f"Совпали {len(best)} ответа из {len(good)} "
                       f"(текстовая схожесть ≥ {self.threshold})",
                model="rules",
            )
        return None

    # ---- дорогая ступень -------------------------------------------------

    def verify(self, task_prompt: str, proposals: list[Proposal]) -> Verdict:
        good = [p for p in proposals if p.ok and p.answer.strip()]
        failed = [p.proposer_id for p in proposals if not p.ok or not p.answer.strip()]

        if not good:
            return Verdict(consensus=False, dissenting=failed, escalate_to_judge=True,
                           reason="Ни один Proposer не дал ответа", model="rules")
        if len(good) == 1:
            return Verdict(
                consensus=False, agreeing=[good[0].proposer_id], dissenting=failed,
                chosen_answer=good[0].answer, escalate_to_judge=True,
                reason="Ответил только один Proposer — нужна проверка Judge", model="rules")

        cheap = self.cheap_vote(proposals)
        if cheap is not None:
            cheap.dissenting = list(dict.fromkeys(cheap.dissenting + failed))
            return cheap

        if not self.use_llm:
            return Verdict(consensus=False, dissenting=[p.proposer_id for p in good] + failed,
                           chosen_answer=good[0].answer, escalate_to_judge=True,
                           reason="Ответы расходятся, LLM-проверка отключена", model="rules")

        return self._verify_with_llm(task_prompt, good, failed)

    def _verify_with_llm(self, task_prompt: str, good: list[Proposal],
                         failed: list[str]) -> Verdict:
        try:
            spec = self.gateway.spec("verifier")
        except Exception as exc:  # noqa: BLE001
            return Verdict(consensus=False, escalate_to_judge=True,
                           reason=f"Verifier недоступен: {exc}", model="none")

        listing = "\n\n".join(
            f"[{p.proposer_id}] (модель {p.model})\n{p.answer[:2000]}" for p in good)
        response = self.gateway.chat(spec, [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"ЗАДАЧА: {task_prompt}\n\nОТВЕТЫ:\n{listing}"},
        ])
        if not response.ok:
            return Verdict(consensus=False, dissenting=[p.proposer_id for p in good] + failed,
                           chosen_answer=good[0].answer, escalate_to_judge=True,
                           reason=f"Verifier не ответил: {response.error}", model=spec.model)

        from core.router import _first_json_object
        data = _first_json_object(response.text) or {}
        agreeing = [str(x) for x in (data.get("agreeing") or [])]
        by_id = {p.proposer_id: p for p in good}
        agreeing = [a for a in agreeing if a in by_id]
        consensus = bool(data.get("consensus")) and len(agreeing) >= 2

        chosen_id = str(data.get("chosen") or (agreeing[0] if agreeing else good[0].proposer_id))
        chosen = by_id.get(chosen_id, good[0])
        dissenting = [p.proposer_id for p in good if p.proposer_id not in agreeing]
        return Verdict(
            consensus=consensus, agreeing=agreeing,
            dissenting=list(dict.fromkeys(dissenting + failed)),
            chosen_answer=chosen.answer,
            escalate_to_judge=not consensus,
            reason=str(data.get("reason") or "Проверка Verifier"),
            model=spec.model,
        )
