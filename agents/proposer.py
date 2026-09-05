"""
Proposer (слой 5) — независимый исполнитель задачи.

Три экземпляра разных семейств работают параллельно. Каждый умеет вызывать
инструменты из Tool Registry в цикле: вызов -> результат -> уточнённый ответ.

Ключевой момент (прошлая проблема «модели не обращаются к инструментам»):
результат инструмента возвращается модели ОТДЕЛЬНЫМ сообщением с ролью tool,
а для моделей без нативного tool calling — текстом с явной пометкой. Так
итоговый ответ строится на фактах инструмента, а не на догадках.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from core.llm_gateway import LLMGateway, ModelSpec
from core.schemas import Proposal, ToolCallRequest, ToolCallResult
from core.tool_registry import ToolRegistry

PROPOSER_VERSION = "1.0"

SYSTEM_PROMPT = (
    "Ты аналитик-исполнитель. Отвечай по-русски, кратко и по существу.\n"
    "Правила:\n"
    "1. Данные о файлах, слоях, таблицах и координатах берутся ТОЛЬКО из "
    "результатов инструментов. Выдумывать значения запрещено.\n"
    "2. Если инструмент вернул ошибку — сообщи об ошибке, не подменяй её догадкой.\n"
    "3. Путь к файлу НИКОГДА не угадывай. Стандартных папок вида "
    "C:/Users/<имя>/Desktop на этом компьютере может не быть — рабочий стол "
    "и документы бывают перенаправлены в OneDrive. Если точный путь не дан "
    "пользователем, сначала вызови files__find_file по имени файла или "
    "files__known_folders, и только потом читай найденный полный путь.\n"
    "4. Если чтение вернуло «путь не найден», в тексте ошибки уже есть "
    "найденные полные пути — повтори вызов с одним из них, а не сдавайся.\n"
    "5. В конце дай итоговый ответ обычным текстом (без JSON), опираясь на "
    "полученные факты.\n"
    "6. ВЕДИ ЧЕКЛИСТ ЗАДАЧИ: перечитай задание, выпиши в ответе (или держи в "
    "голове) список требований и отмечай выполненные. Перед финальным ответом "
    "сверь: каждый ли требуемый файл/артефакт реально записан через инструмент, "
    "а не только описан текстом. Задание на запись без вызова пишущего "
    "инструмента = задача НЕ выполнена.\n"
    "7. ВАЖНО про CSV/GeoJSON/большие таблицы: НЕ читай весь файл через read_text "
    "(контекст не вместит). Вместо этого вызови files__summarize с пути и списком "
    "колонок (columns) — он вернёт компактную сводку: число записей, уникальные "
    "значения и частоты по каждой колонке.\n"
    "8. ШПАРГАЛКА ПО ИНСТРУМЕНТАМ (частые ошибки):\n"
    "   - datafiles__write_text принимает ТОЛЬКО .txt и .md. Для HTML/CSS/JS — "
    "используй datafiles__write_text с расширением .html НЕЛЬЗЯ: создай файл "
    "через write_text только если расширение .txt/.md; для веб-файлов используй "
    "инструмент build или сохрани как .txt и переименуй на следующем шаге.\n"
    "   - datafiles__write_csv — для .csv, datafiles__write_json — для .json, "
    "datafiles__write_excel — для .xlsx (данные передавай как список словарей "
    "[{...}, ...], не как строку). КРИТИЧНО для write_csv: ВСЕГДА передавай "
    "field_order — список колонок В ТОМ ПОРЯДКЕ, что указан в задании, например "
    "field_order=[\"проба\",\"Au_г_т\",\"Ag_г_т\",\"интервал_м\"]. Без field_order "
    "колонки лягут в случайном порядке и ревью забракует файл.\n"
    "   - charts__plot: требует аргумент values (список чисел) или series "
    "(словарь {имя: [числа]}). Строки вместо чисел = ошибка.\n"
    "   - ЛЮБОЙ файл при записи: если уже существует — добавь overwrite=true, "
    "иначе получишь ошибку «Файл уже существует».\n"
    "   - websearch__get_page_text: при ConnectTimeout НЕ повторяй тот же URL — "
    "возьми другой результат из поиска или сообщи о недоступности.\n"
    "   - browser: схема file:// ЗАПРЕЩЕНА, только http:// или https://. "
    "Для локальных файлов сначала подними сервер (build) или работай через "
    "files/datafiles.\n"
    "   - После ошибки инструмента ПРОЧИТАЙ текст ошибки — там обычно написано, "
    "что именно исправить (не тот аргумент, не тот путь, нужен overwrite)."
    )


class Proposer:
    """Один Proposer = одна модель из config/models.yaml."""

    version = PROPOSER_VERSION

    def __init__(
        self,
        spec: ModelSpec,
        gateway: LLMGateway,
        registry: ToolRegistry | None = None,
        max_tool_iterations: int = 3,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        checkpoint_every: int = 0,
        on_checkpoint: Callable[[int, str], None] | None = None,
        extra: dict[str, Any] | None = None,
    ):
        self.spec = spec
        self.gateway = gateway
        self.registry = registry
        self.max_tool_iterations = max_tool_iterations
        self.on_event = on_event
        # A2 (24.08): self-checkpoint — каждые N итераций вызываю on_checkpoint
        # (номер итерации, краткое резюме). 0 = выключено. Резюме узел
        # оркестратора пишет в Blackboard: прогресс виден ДО конца прогона.
        self.checkpoint_every = max(0, int(checkpoint_every))
        self.on_checkpoint = on_checkpoint
        # A3: extra несёт research_limit (лимит поисковых вызовов до записи).
        self.extra = extra or {}

    def _emit(self, kind: str, **payload: Any) -> None:
        """События для экрана «Ход выполнения» (реалтайм)."""
        if self.on_event:
            try:
                self.on_event({"kind": kind, "proposer_id": self.spec.id,
                               "model": self.spec.model, **payload})
            except Exception:  # noqa: BLE001 — UI не должен ломать выполнение
                pass

    def run(
        self,
        prompt: str,
        context_summary: str = "",
        allowed_tools: list[str] | None = None,
        task_mode: str | None = None,
    ) -> Proposal:
        started = time.perf_counter()
        tools = self.registry.openai_tools(allowed_tools) if self.registry else []
        system = SYSTEM_PROMPT
        if context_summary:
            system = f"{system}\n\n{context_summary}"

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        all_requests: list[ToolCallRequest] = []
        all_results: list[ToolCallResult] = []
        last_text = ""
        # Защита от зацикливания: если модель N раз подряд вызывает ОДИН И ТОТ ЖЕ
        # инструмент с теми же аргументами (напр. read_text без offset), прерываем,
        # чтобы зависший шаг не жuл минуты в цикле.Считаем повторы вызова.
        seen_calls: dict[tuple, int] = {}
        # A3 (24.08): лимит исследовательских websearch/get_page_text вызовов ДО
        # первой записи — иначе модель читает 6-8 страниц подряд и съедает полчаса
        # (задача d1d0e9c3). После первой записи лимит не применяется.
        _RESEARCH_ACTIONS = ("search_web", "get_page_text")
        research_calls = 0
        research_limit = int(self.extra.get("research_limit", 6))
        wrote_something = False

        for iteration in range(self.max_tool_iterations + 1):
            self._emit("model_call", iteration=iteration)
            if (self.checkpoint_every and iteration
                    and iteration % self.checkpoint_every == 0
                    and self.on_checkpoint is not None):
                try:
                    self.on_checkpoint(iteration, last_text or "(пока без текста)")
                except Exception:  # noqa: BLE001 — чекпоинт не должен ронять шаг
                    pass
            response = self.gateway.chat(
                self.spec, messages, tools=tools or None, task_mode=task_mode)
            if not response.ok:
                self._emit("error", error=response.error)
                return Proposal(
                    proposer_id=self.spec.id, model=self.spec.model,
                    provider=self.spec.provider, ok=False, error=response.error,
                    tool_calls=all_requests, tool_results=all_results,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )

            last_text = response.text
            calls = response.tool_calls if (tools and iteration < self.max_tool_iterations) else []

            # A3: гасим исследовательские вызовы после исчерпания лимита,
            # пока не было ни одной записи. Модель получит подсказку и
            # перейдёт к действию.
            if calls and not wrote_something:
                still_researching = [c for c in calls
                                     if any(c.action.startswith(a) for a in _RESEARCH_ACTIONS)]
                if still_researching and research_calls >= research_limit:
                    self._emit("research_limit", used=research_calls, limit=research_limit)
                    messages.append({
                        "role": "user",
                        "content": (f"ЛИМИТ ИССЛЕДОВАНИЯ ИСЧЕРПАН ({research_calls} "
                                    f"поисковых вызовов). Переходи к выполнению: "
                                    f"запиши результат инструментом записи."),
                    })
                    continue

            if not calls:
                break

            # Ответ модели с вызовом сохраняем в истории — иначе она забудет,
            # что уже запрашивала, и уйдёт в цикл повторных вызовов.
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

            # Детекция повторов: тот же инструмент+действие+аргументы.
            # Аргументы сериализуем в JSON-строку: в args может лежать вложенный
            # dict (напр. фильтр QGIS {"filter": {...}}), а dict не хешируется —
            # без сериализации кортеж падал бы с TypeError: unhashable type.
            repeated = False
            for call in calls:
                args_key = json.dumps(call.args, sort_keys=True, ensure_ascii=False)
                sig = (call.tool, call.action, args_key)
                seen_calls[sig] = seen_calls.get(sig, 0) + 1
                if seen_calls[sig] >= 3:
                    repeated = True
                    break
            if repeated:
                self._emit("loop_stopped", reason="повторный вызов одного инструмента")
                # Сообщаем модели, что зациклилась, и просим завершить ответ
                messages.append({
                    "role": "user",
                    "content": "ВНИМАНИЕ: ты повторяешь один и тот же вызов инструмента "
                               "с теми же аргументами три раза подряд. Это цикл. "
                               "Прекрати вызывать инструменты и дай итоговый ответ "
                               "текстом на основе уже полученных данных.",
                })
                continue

            for call in calls:
                self._emit("tool_call", tool=call.tool, action=call.action, args=call.args)
                assert self.registry is not None
                result = self.registry.call(call)
                all_requests.append(call)
                all_results.append(result)
                # A3: счётчики для лимита исследования / признака записи
                if any(call.action.startswith(a) for a in _RESEARCH_ACTIONS):
                    research_calls += 1
                if result.ok and call.tool == "datafiles":
                    wrote_something = True
                self._emit("tool_result", tool=call.tool, action=call.action,
                           ok=result.ok, summary=result.summary or result.error,
                           # видно в журнале: повторное чтение не стоило времени
                           from_cache=getattr(result, "from_cache", False))
                messages.append({
                    "role": "tool",
                    "name": f"{call.tool}__{call.action}",
                    "content": _format_tool_result(result),
                })

        self._emit("done", latency_ms=int((time.perf_counter() - started) * 1000))
        return Proposal(
            proposer_id=self.spec.id, model=self.spec.model, provider=self.spec.provider,
            answer=last_text.strip(), tool_calls=all_requests, tool_results=all_results,
            ok=True, latency_ms=int((time.perf_counter() - started) * 1000),
        )


def _format_tool_result(result: ToolCallResult) -> str:
    """
    Результат инструмента для модели.

    Дублируем машинные данные и человеческую сводку: часть моделей читает
    только текст, часть лучше работает с JSON.
    """
    if not result.ok:
        return (f"ОШИБКА инструмента {result.tool}.{result.action}: {result.error}\n"
                f"Не придумывай данные — сообщи об ошибке в ответе.")
    data = json.dumps(result.data, ensure_ascii=False)
    # Больше данных доходит до модели: контекст вырос до 32K токенов,
    # поэтому результат величиной до ~20K символов оправдан.
    # Это даёт модели реальное содержимое прочитанного файла, а не огрызок.
    if len(data) > 20000:
        data = data[:20000] + "...(обрезано, вызови с offset для продолжения)"
    return (f"РЕЗУЛЬТАТ инструмента {result.tool}.{result.action} "
            f"(версия {result.tool_version}):\n{result.summary}\nДанные JSON: {data}")
