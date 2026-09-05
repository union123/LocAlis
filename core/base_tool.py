"""
BaseTool — единый интерфейс любого инструмента платформы.

Ядро (router, blackboard, proposers, verifier, judge) не знает ни об одном
конкретном инструменте. Оно видит только этот интерфейс и Pydantic-схемы.

Чтобы добавить инструмент: создать папку tools/<имя>/ с manifest.yaml и
модулем, где объявлен класс-наследник BaseTool. Код ядра не меняется.
См. CONTRIBUTING_TOOLS.md.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from typing import Any

from core.schemas import ToolCallRequest, ToolCallResult, ToolStatus


class ToolAction:
    """Описание одного действия инструмента (для JSON-schema и промпта модели)."""

    def __init__(self, name: str, description: str, parameters: dict[str, Any]):
        self.name = name
        self.description = description
        # parameters — JSON Schema объекта аргументов
        self.parameters = parameters

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


class BaseTool(ABC):
    """
    Базовый класс инструмента.

    Обязательные атрибуты класса: name, version, description.
    Обязательные методы: actions(), execute().
    Необязательный: health() — проверка готовности окружения.
    """

    name: str = "unnamed_tool"
    version: str = "0.1.0"
    description: str = ""

    # Нужен ли инструменту доступ к возможностям платформы (модели, память,
    # база знаний). Большинству инструментов это не нужно.
    requires_context: bool = False

    def __init__(self, config: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None):
        # config приходит из manifest.yaml (секция config) и настроек платформы
        self.config: dict[str, Any] = config or {}
        # context — возможности платформы, внедряемые реестром (см. core/tool_context.py).
        # Инструмент не импортирует классы ядра — берёт объекты из словаря.
        self.context: dict[str, Any] = context or {}

    # ---- доступ к контексту ---------------------------------------------

    def capability(self, name: str) -> Any:
        """Возможность платформы или None, если она не предоставлена."""
        return self.context.get(name)

    def require(self, name: str) -> Any:
        """Возможность или понятная ошибка вместо сбоя глубоко в коде."""
        value = self.context.get(name)
        if value is None:
            raise RuntimeError(
                f"Инструменту '{self.name}' недоступна возможность '{name}': "
                "он запущен без контекста платформы"
            )
        return value

    # ---- обязательная часть контракта -----------------------------------

    @abstractmethod
    def actions(self) -> list[ToolAction]:
        """Список поддерживаемых действий."""

    @abstractmethod
    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        """
        Выполнить действие. Возвращает словарь вида:
            {"ok": bool, "data": {...}, "summary": "...", "error": None|str}
        Исключения наружу бросать не нужно — их перехватит run().
        """

    # ---- необязательная часть -------------------------------------------

    def health(self) -> tuple[ToolStatus, str]:
        """Проверка готовности. По умолчанию считаем инструмент готовым."""
        return ToolStatus.READY, "ok"

    # ---- сервисные методы (не переопределять) ---------------------------

    def input_schema(self) -> dict[str, Any]:
        """JSON Schema инструмента целиком — отдаётся моделям для tool calling."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "actions": [a.as_dict() for a in self.actions()],
        }

    def run(self, request: ToolCallRequest) -> ToolCallResult:
        """
        Безопасный вызов execute(): меряет время, ловит исключения,
        всегда возвращает валидный ToolCallResult.
        """
        started = time.perf_counter()
        known = {a.name for a in self.actions()}
        if request.action not in known:
            return ToolCallResult(
                call_id=request.call_id,
                tool=self.name,
                action=request.action,
                ok=False,
                error=f"Неизвестное действие '{request.action}'. Доступны: {sorted(known)}",
                tool_version=self.version,
            )
        try:
            raw = self.execute(request.action, request.args) or {}
            ok = bool(raw.get("ok", False))
            return ToolCallResult(
                call_id=request.call_id,
                tool=self.name,
                action=request.action,
                ok=ok,
                data=raw.get("data") or {},
                summary=str(raw.get("summary") or ""),
                error=raw.get("error"),
                duration_ms=int((time.perf_counter() - started) * 1000),
                tool_version=self.version,
            )
        except Exception as exc:  # noqa: BLE001 — инструмент не должен ронять граф
            return ToolCallResult(
                call_id=request.call_id,
                tool=self.name,
                action=request.action,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=int((time.perf_counter() - started) * 1000),
                tool_version=self.version,
            )
