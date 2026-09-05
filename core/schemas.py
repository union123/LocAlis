"""
Единые Pydantic-контракты платформы.

Все агенты и инструменты общаются ТОЛЬКО через эти схемы.
Прямой импорт внутренних классов друг друга запрещён — это обеспечивает
слабую связанность и возможность заменить любой компонент без правки ядра.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Версия контрактов. Повышать при несовместимых изменениях схем.
SCHEMAS_VERSION = "1.0"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


# --------------------------------------------------------------------------
# Инструменты (Tool Registry)
# --------------------------------------------------------------------------


class ToolStatus(str, Enum):
    """Состояние инструмента после проверки готовности."""

    READY = "ready"           # готов к работе
    UNAVAILABLE = "unavailable"  # нет зависимостей/окружения (напр. не найден QGIS)
    DISABLED = "disabled"     # выключен пользователем в манифесте/настройках
    ERROR = "error"           # ошибка загрузки плагина


class ToolManifest(BaseModel):
    """Метаданные плагина, читаются из tools/<plugin>/manifest.yaml."""

    # coerce_numbers_to_str: YAML превращает значения вида off/on/yes/no в булевы,
    # а версию 1.0 — в число. Без приведения типов такой манифест падает
    # с непонятной ошибкой валидации.
    model_config = ConfigDict(extra="allow", coerce_numbers_to_str=True)

    name: str
    version: str = "0.1.0"

    @field_validator("name", "version", "description", "entrypoint", mode="before")
    @classmethod
    def _coerce_to_str(cls, value: Any) -> Any:
        """Привести булевы/числовые значения из YAML к строке."""
        if isinstance(value, bool):
            return "off" if value is False else "on"
        if isinstance(value, (int, float)):
            return str(value)
        return value
    description: str = ""
    enabled: bool = True
    entrypoint: str = "tool.py"       # файл с классом-наследником BaseTool
    class_name: str | None = None      # имя класса; если None — ищем автоматически
    requires: list[str] = Field(default_factory=list)  # внешние требования (инфо)


class ToolCallRequest(BaseModel):
    """Запрос на вызов инструмента (то, что формирует модель)."""

    tool: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    call_id: str = Field(default_factory=_new_id)


class ToolCallResult(BaseModel):
    """Результат вызова инструмента. Ошибка — это тоже результат, не исключение."""

    call_id: str
    tool: str
    action: str
    ok: bool
    # data — машинночитаемый результат; summary — короткий текст для LLM
    data: dict[str, Any] = Field(default_factory=dict)
    summary: str = ""
    error: str | None = None
    duration_ms: int = 0
    tool_version: str = ""
    started_at: datetime = Field(default_factory=_utcnow)
    # Результат взят из кеша чтений внутри одной задачи (см. ToolRegistry).
    # Полезно в журнале: видно, что повторный вызов не стоил времени.
    from_cache: bool = False


# --------------------------------------------------------------------------
# Задачи и маршрутизация
# --------------------------------------------------------------------------


class TaskComplexity(str, Enum):
    SIMPLE = "simple"
    AMBIGUOUS = "ambiguous"
    NEEDS_VERIFICATION = "needs_verification"


class ExecutionMode(str, Enum):
    AUTO = "auto"              # локально + облако при доступности
    LOCAL_ONLY = "local_only"  # строго офлайн (конфиденциальные данные)
    # Оркестратор: сильная модель делит задачу на шаги и поручает
    # их малым локальным моделям. В отличие от auto/local_only здесь
    # нет голосования 2 из 3: шаги разные, сравнивать их между собой нельзя.
    ORCHESTRATED = "orchestrated"


class OrchestratorPlacement(str, Enum):
    """Где работает сам оркестратор (исполнители всегда локальные)."""

    CLOUD = "cloud"    # сильнее планирует, но текст задачи уходит в сеть
    LOCAL = "local"    # всё остаётся на компьютере
    AUTO = "auto"      # облако при доступности, иначе локальный


class Task(BaseModel):
    """Входная задача пользователя."""

    task_id: str = Field(default_factory=_new_id)
    prompt: str
    mode: ExecutionMode = ExecutionMode.AUTO
    allowed_tools: list[str] | None = None  # None = все включённые
    created_at: datetime = Field(default_factory=_utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RoutingDecision(BaseModel):
    """Решение Router: как обрабатывать задачу."""

    complexity: TaskComplexity = TaskComplexity.NEEDS_VERIFICATION
    needs_tools: bool = False
    needs_retrieval: bool = False
    needs_full_debate: bool = True
    suggested_tools: list[str] = Field(default_factory=list)
    reason: str = ""
    decided_by: str = "rules"  # rules | llm | fallback


# --------------------------------------------------------------------------
# Ответы агентов
# --------------------------------------------------------------------------


class Proposal(BaseModel):
    """Ответ одного Proposer."""

    proposer_id: str            # имя роли из конфига, напр. "proposer_a"
    model: str                  # реальный id модели
    provider: Literal["ollama", "openrouter", "llamacpp", "stub"] = "ollama"
    answer: str = ""
    tool_calls: list[ToolCallRequest] = Field(default_factory=list)
    tool_results: list[ToolCallResult] = Field(default_factory=list)
    ok: bool = True
    error: str | None = None
    latency_ms: int = 0


class Verdict(BaseModel):
    """Результат Verifier: голосование 2 из 3."""

    consensus: bool = False
    agreeing: list[str] = Field(default_factory=list)   # proposer_id, совпавшие
    dissenting: list[str] = Field(default_factory=list)
    chosen_answer: str | None = None
    escalate_to_judge: bool = True
    reason: str = ""
    model: str = ""


class FinalDecision(BaseModel):
    """Финальный ответ Judge/Supervisor."""

    answer: str
    rationale: str = ""
    decided_by: Literal["verifier_consensus", "judge_cloud", "judge_local",
                        "orchestrator_cloud", "orchestrator_local", "simple_orchestrated",
                        "fallback"] = "fallback"
    model: str = ""
    used_cloud: bool = False
    subagent_calls: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Оркестрация: план шагов и их выполнение
# --------------------------------------------------------------------------


class PlanStep(BaseModel):
    """
    Один шаг плана, поручаемый малой локальной модели.

    depends_on даёт оркестратору выразить порядок: шаг «посчитать» бессмыслен
    без шага «прочитать файл». Шаги без зависимостей выполняются параллельно.
    """

    step_id: str = ""                 # короткий идентификатор вида "s1"
    title: str = ""                   # короткое название для интерфейса
    instruction: str = ""             # точное задание исполнителю
    assignee: str = ""                # proposer_id или "" = выберет платформа
    tools: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)


class Plan(BaseModel):
    """План, составленный оркестратором."""

    steps: list[PlanStep] = Field(default_factory=list)
    reasoning: str = ""
    model: str = ""
    placement: OrchestratorPlacement = OrchestratorPlacement.LOCAL
    ok: bool = True
    error: str | None = None
    latency_ms: int = 0


class StepResult(BaseModel):
    """Итог одного шага плана."""

    step_id: str = ""
    title: str = ""
    assignee: str = ""
    model: str = ""
    answer: str = ""
    ok: bool = True
    error: str | None = None
    tool_results: list[ToolCallResult] = Field(default_factory=list)
    latency_ms: int = 0
    # Шаг формально успешен, но вызывает сомнения: например, ему выдали
    # инструменты, но модель ни разу их не вызвала — значит, ответ
    # построен на догадках. Проверка без вызова модели, чистая логика.
    suspicious: bool = False
    warnings: list[str] = Field(default_factory=list)


class TaskRecord(BaseModel):
    """Полная запись задачи в Blackboard."""

    task: Task
    routing: RoutingDecision | None = None
    context_summary: str = ""
    proposals: list[Proposal] = Field(default_factory=list)
    verdict: Verdict | None = None
    final: FinalDecision | None = None
    plan: Plan | None = None
    step_results: list[StepResult] = Field(default_factory=list)
    status: Literal["created", "running", "done", "failed"] = "created"
    error: str | None = None
