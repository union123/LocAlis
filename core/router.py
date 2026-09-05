"""
Router (слой 1) — классификация задачи.

Порядок: таблица правил из config/routing_rules.yaml -> лёгкая локальная
модель -> default. Правила НЕ зашиты в код: добавление правила = запись в YAML.

Router никогда не падает: любая ошибка модели даёт default-решение
(graceful degradation).
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.llm_gateway import LLMGateway
from core.schemas import RoutingDecision, Task, TaskComplexity

ROUTER_VERSION = "1.0"

_SYSTEM_PROMPT = (
    "Ты маршрутизатор задач. Классифицируй задачу пользователя и верни ТОЛЬКО JSON:\n"
    '{"complexity":"simple|ambiguous|needs_verification",'
    '"needs_tools":true|false,"needs_retrieval":true|false,'
    '"needs_full_debate":true|false,"reason":"кратко по-русски"}\n'
    "simple — тривиальный вопрос, хватит одной модели.\n"
    "ambiguous — формулировка неоднозначна, нужны уточнения и сверка.\n"
    "needs_verification — есть риск ошибки, нужна проверка несколькими моделями.\n"
    "needs_tools=true, если требуется работа с файлами, таблицами, геоданными."
)


class Router:
    """Классификатор задач по конфигурируемой таблице правил."""

    def __init__(self, rules_config: dict[str, Any], gateway: LLMGateway | None = None):
        self.config = rules_config
        self.gateway = gateway
        self.rules: list[dict[str, Any]] = list(rules_config.get("rules") or [])
        self.default: dict[str, Any] = dict(rules_config.get("default") or {})
        classifier = rules_config.get("llm_classifier") or {}
        self.llm_enabled = bool(classifier.get("enabled", True))
        self.llm_fallback_default = bool(classifier.get("fallback_to_default", True))

    # ---- правила ---------------------------------------------------------

    @staticmethod
    def _matches(prompt: str, match: dict[str, Any]) -> bool:
        """Проверить один блок match. Пустой блок не срабатывает никогда."""
        if not match:
            return False
        lowered = prompt.lower()
        checks: list[bool] = []

        keywords = match.get("any_keyword")
        if keywords:
            checks.append(any(str(k).lower() in lowered for k in keywords))

        all_keywords = match.get("all_keywords")
        if all_keywords:
            checks.append(all(str(k).lower() in lowered for k in all_keywords))

        pattern = match.get("regex")
        if pattern:
            try:
                checks.append(bool(re.search(pattern, prompt, re.IGNORECASE)))
            except re.error:
                checks.append(False)

        max_length = match.get("max_length")
        if max_length is not None:
            checks.append(len(prompt) <= int(max_length))

        min_length = match.get("min_length")
        if min_length is not None:
            checks.append(len(prompt) >= int(min_length))

        # Все указанные условия должны выполниться (И)
        return bool(checks) and all(checks)

    @staticmethod
    def _to_decision(data: dict[str, Any], decided_by: str, rule_name: str = "") -> RoutingDecision:
        complexity = str(data.get("complexity", "needs_verification"))
        try:
            complexity_enum = TaskComplexity(complexity)
        except ValueError:
            complexity_enum = TaskComplexity.NEEDS_VERIFICATION
        reason = str(data.get("reason") or "")
        if rule_name:
            reason = f"[правило: {rule_name}] {reason}"
        return RoutingDecision(
            complexity=complexity_enum,
            needs_tools=bool(data.get("needs_tools", False)),
            needs_retrieval=bool(data.get("needs_retrieval", False)),
            needs_full_debate=bool(data.get("needs_full_debate", True)),
            suggested_tools=list(data.get("suggested_tools") or []),
            reason=reason,
            decided_by=decided_by,
        )

    def match_rules(self, prompt: str) -> RoutingDecision | None:
        """Первое сработавшее правило сверху вниз."""
        for rule in self.rules:
            if self._matches(prompt, rule.get("match") or {}):
                return self._to_decision(
                    rule.get("decision") or {}, "rules", str(rule.get("name", "")))
        return None

    # ---- LLM-классификатор ----------------------------------------------

    def classify_with_llm(self, prompt: str) -> RoutingDecision | None:
        if not (self.llm_enabled and self.gateway):
            return None
        try:
            spec = self.gateway.spec("router")
        except Exception:  # noqa: BLE001
            return None
        response = self.gateway.chat(spec, [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ])
        if not response.ok or not response.text:
            return None
        data = _first_json_object(response.text)
        if data is None:
            return None
        return self._to_decision(data, "llm")

    # ---- публичный вход --------------------------------------------------

    def route(self, task: Task) -> RoutingDecision:
        decision = self.match_rules(task.prompt)
        if decision is not None:
            return decision
        decision = self.classify_with_llm(task.prompt)
        if decision is not None:
            return decision
        return self._to_decision(self.default, "fallback")


def _first_json_object(text: str) -> dict[str, Any] | None:
    """Достать первый JSON-объект из текста ответа модели."""
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth:
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    parsed = json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = -1
                    continue
                if isinstance(parsed, dict):
                    return parsed
    return None
