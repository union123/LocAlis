"""
Контекст платформы для инструментов-агентов.

Зачем: обычный инструмент работает с внешним миром (файлы, QGIS) и ничего
не знает о платформе. Но инструмент-АГЕНТ должен уметь вызывать локальные
модели, искать в базе знаний и писать в память задачи.

Чтобы не нарушать слабую связанность, ядро не передаёт инструментам свои
классы напрямую: оно собирает словарь простых функций (возможностей).
Инструмент вызывает их через self.require("ask_local_model") и остаётся
независимым от внутреннего устройства ядра.

Ключи возможностей (константы ниже) — часть публичного контракта:
их менять нельзя без повышения версии.
"""

from __future__ import annotations

from typing import Any, Callable

CONTEXT_VERSION = "1.0"

# ---- ключи возможностей --------------------------------------------------

# ask_local_model(model_id: str, prompt: str, system: str = "",
#                 temperature: float | None = None) -> dict
#   Один вызов локальной модели БЕЗ инструментов (быстро, без рекурсии).
ASK_LOCAL_MODEL = "ask_local_model"

# list_local_models() -> list[dict]
#   Какие локальные модели доступны: id, модель, роль, назначение.
LIST_LOCAL_MODELS = "list_local_models"

# run_local_agent(model_id: str, task: str, allowed_tools: list[str] | None) -> dict
#   Полноценный под-агент: модель с доступом к инструментам платформы.
RUN_LOCAL_AGENT = "run_local_agent"

# search_knowledge(query: str, top_k: int) -> list[dict]
#   Поиск по локальной базе знаний (векторный индекс).
SEARCH_KNOWLEDGE = "search_knowledge"

# remember(key: str, value: str) / recall(key: str | None) -> ...
#   Рабочая память текущей задачи (переживает шаги графа).
REMEMBER = "remember"
RECALL = "recall"

# Долговечная память МЕЖДУ задачами (SQLite).
# remember_fact(key, value) — сохранить факт навсегда (переживает перезапуск).
# recall_fact(key | None) — прочитать факт(ы).
# search_facts(query, top_k) — найти факты по тексту.
# search_past(query, top_k) — найти похожие прошлые решения задач.
REMEMBER_FACT = "remember_fact"
RECALL_FACT = "recall_fact"
SEARCH_FACTS = "search_facts"
SEARCH_PAST = "search_past"

# Журнал задач Blackboard (SQLite): последние задачи, их статус, вызовы
# инструментов, причины сбоев. Позволяет агенту анализировать реальную
# историю выполнения, а не искать несуществующий файл .log.
GET_TASK_HISTORY = "get_task_history"

# task_id() -> str  — идентификатор текущей задачи (для журналов)
TASK_ID = "task_id"

# emit_event(event: dict) -> None — прогресс в интерфейс «Ход выполнения»
EMIT_EVENT = "emit_event"

# get_allowed_roots() -> list[str] | None
#   Папки, внутрь которых задаче разрешено писать файлы. Источник —
#   task.metadata["allowed_roots"] (заполняется автоматически из путей
#   задачи, см. Workflow._ensure_allowed_roots). None или пустой список =
#   задача не ограничена (используется статический allowed_roots из
#   manifest.yaml инструмента).
GET_ALLOWED_ROOTS = "get_allowed_roots"

ALL_CAPABILITIES = [
    ASK_LOCAL_MODEL,
    LIST_LOCAL_MODELS,
    RUN_LOCAL_AGENT,
    SEARCH_KNOWLEDGE,
    REMEMBER,
    RECALL,
    REMEMBER_FACT,
    RECALL_FACT,
    SEARCH_FACTS,
    SEARCH_PAST,
    GET_TASK_HISTORY,
    GET_ALLOWED_ROOTS,
    TASK_ID,
    EMIT_EVENT,
]


class ScratchMemory:
    """
    Рабочая память задачи в оперативной памяти процесса.

    Нужна, чтобы под-агенты могли передавать промежуточные выводы друг другу
    внутри одной задачи, не засоряя Blackboard (там хранятся решения, а не
    черновики). Живёт до конца задачи.
    """

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    def remember(self, key: str, value: str) -> None:
        self._data[str(key)] = str(value)

    def recall(self, key: str | None = None) -> Any:
        if key is None:
            return dict(self._data)
        return self._data.get(str(key))

    def clear(self) -> None:
        self._data.clear()

    def __len__(self) -> int:
        return len(self._data)


def build_context(
    ask_local_model: Callable[..., dict[str, Any]] | None = None,
    list_local_models: Callable[[], list[dict[str, Any]]] | None = None,
    run_local_agent: Callable[..., dict[str, Any]] | None = None,
    search_knowledge: Callable[..., list[dict[str, Any]]] | None = None,
    memory: ScratchMemory | None = None,
    task_id: Callable[[], str] | None = None,
    emit_event: Callable[[dict[str, Any]], None] | None = None,
    persistent_memory: Any = None,
    get_task_history: Callable[..., dict[str, Any]] | None = None,
    get_allowed_roots: Callable[[], list[str] | None] | None = None,
) -> dict[str, Any]:
    """
    Собрать словарь возможностей. None-значения не попадают в контекст —
    инструмент увидит отсутствие возможности и сообщит об этом честно.
    """
    memory = memory or ScratchMemory()
    context: dict[str, Any] = {
        REMEMBER: memory.remember,
        RECALL: memory.recall,
    }
    # Долговечная память (между задачами) — только если есть хранилище
    if persistent_memory is not None:
        context[REMEMBER_FACT] = persistent_memory.remember_fact
        context[RECALL_FACT] = persistent_memory.recall_fact
        context[SEARCH_FACTS] = persistent_memory.search_facts
        context[SEARCH_PAST] = persistent_memory.search_past
    # Журнал задач Blackboard — только если доступен
    if get_task_history is not None:
        context[GET_TASK_HISTORY] = get_task_history
    # Песочница записи текущей задачи — только если ограничение задано
    if get_allowed_roots is not None:
        context[GET_ALLOWED_ROOTS] = get_allowed_roots
    optional = {
        ASK_LOCAL_MODEL: ask_local_model,
        LIST_LOCAL_MODELS: list_local_models,
        RUN_LOCAL_AGENT: run_local_agent,
        SEARCH_KNOWLEDGE: search_knowledge,
        TASK_ID: task_id,
        EMIT_EVENT: emit_event,
    }
    for key, value in optional.items():
        if value is not None:
            context[key] = value
    return context
