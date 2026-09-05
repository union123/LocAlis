"""
Orchestrator (новый режим) — планировщик-распорядитель.

Отличие от Judge: Judge сравнивает три готовых ответа на ОДИН вопрос,
Orchestrator дробит задачу на РАЗНЫЕ подзадачи и поручает каждую малой
локальной модели, а затем собирает итог.

Зачем: голосование 2 из 3 хорошо ловит галлюцинации, но тратит три модели
на один и тот же вопрос. Для составных задач («прочитай файл, посчитай,
сохрани в Excel») выгоднее разделить работу: каждый шаг проще, малая модель
справляется, а сильная модель нужна только на планирование и сборку.

Размещение планировщика (placement):
  cloud — сильнее планирует, но текст задачи уходит в сеть;
  local — всё остаётся на компьютере;
  auto  — облако при доступности, иначе локально (без падения).

Исполнители ВСЕГДА локальные: это гарантия, что содержимое файлов
пользователя не попадёт в облако даже в облачном режиме планирования.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from core.llm_gateway import LLMGateway
from core.schemas import (
    FinalDecision,
    OrchestratorPlacement,
    Plan,
    PlanStep,
    StepResult,
)

ORCHESTRATOR_VERSION = "1.0"

MAX_STEPS = 6  # больше шагов на 8 ГБ VRAM превращаются в многоминутное ожидание

_PLAN_SYSTEM = (
    "Ты планировщик-распорядитель. Раздели задачу пользователя на "
    "последовательность простых шагов и поручи каждый одному исполнителю.\n"
    "Исполнители — небольшие локальные модели: шаг должен быть ОДНИМ понятным "
    "действием, а не сложной цепочкой.\n"
    "Правила:\n"
    "1. Шагов не больше {max_steps}. Если задача простая — один шаг.\n"
    "2. Каждому шагу укажи инструменты из списка доступных, если они нужны.\n"
    "3. depends_on — идентификаторы шагов, без результата которых этот шаг "
    "выполнить нельзя. Независимые шаги выполнятся одновременно.\n"
    "4. Не придумывай пути к файлам и значения данных: пусть исполнитель "
    "получит их инструментом.\n"
    "5. ОБЯЗАТЕЛЬНО укажи путь к файлу в instruction каждого шага, если он "
    "нужен. Исполнители не знают задачу целиком — только свой шаг.\n"
    "6. ПУТЬ К ФАЙЛУ — ПЕРВОСТЕПЕННО. Если в задаче пользователя уже указан "
    "абсолютный путь (например C:\\Users\\...\\file.geojson), скопируй его в "
    "instruction шага ДОСЛОВНО, без изменений. НИКОГДА не переписывай путь, "
    "не угадывай его и не подставляй относительную папку вида "
    "agent-platform\\data\\test. Путь из задачи — единственный правильный.\n"
    "\n"
    "СВЕДЕНИЯ ОБ ИНСТРУМЕНТАХ (кто что умеет — распоряжайся строго по этому):\n"
    "СПИСОК ИНСТРУМЕНТОВ приведён ниже с описанием. Запомни роли по именам:\n"
    "  - files: ЧТЕНИЕ и поиск файлов (read_text, list_dir, find_file, "
        "search_in_files). Первый инструмент, если нужно прочитать данные.\n"
        "  - files.summarize: СЧИТАТЬ РАСПРЕДЕЛЕНИЕ по колонкам CSV/GeoJSON "
        "без чтения всего файла. ОБЯЗАТЕЛЬНО для больших таблиц и геофайлов: "
        "когда нужна статистика (сколько точек, какие категории, частоты) — "
        "дай files.summarize с списком полей (route_num, rock_type, Index), "
        "а не read_text. Это возвращает компактную сводку вместо 200КБ текста.\n"
    "  - datafiles: ТОЛЬКО ЗАПИСЬ готовых файлов (write_csv/excel/word/json). "
    "НЕ читает ничего. Не назначай datafiles на чтение/анализ данных.\n"
    "  - qgis: чтение/обработка ГЕО-данных (list_layers, layer_info, "
    "read_attributes, run_processing, export_geojson).\n"
    "  - charts: построение графиков из ДАННЫХ, которые уже есть (передай их "
    "в шаг с charts как вход).\n"
    "  - local_agents: анализ/память (ask_agent, run_agent, debate, "
    "remember_fact, search_past, **get_task_history** — РЕАЛЬНЫЙ журнал задач "
    "Blackboard: задачи, статусы, сбойные вызовы инструментов). "
    "Для анализа истории выполнения, поиска повторяющихся проблем, "
    "самосовершенствования — ОБЯЗАТЕЛЬНО вызывай get_task_history, "
    "НЕ ищи файл .log.\n"
    "КЛЮЧЕВОЕ: если данные надо ПРОЧИТАТЬ — дай files или qgis. datafiles "
    "только сохраняет результат ПОСЛЕ чтения.\n"
    "ПРО БОЛЬШИЕ ФАЙЛЫ (>50КБ, напр. GeoJSON/CSV с тысячами строк): НЕ давай "
    "шаг «прочитай файл и посчитай руками» — модель не уместит его в контекст. "
    "Дай qgis.read_attributes (читает и агрегирует сам QGIS, возвращает компактно) "
    "или files.read_text ПОРЦИЯМИ (offset/max_chars) и каждый шаг обрабатывает "
    "только свою порцию. Объём данных в контексте шага ограничен, не пытайся "
    "впихнуть весь файл в один шаг.\n"
    "\n"
    "ПОЛНОТА ПЛАНА (задача aaa7ac957231, 24.08): поиск/исследование в интернете — "
    "это ПОДГОТОВКА, а не результат. Если задача требует СОЗДАТЬ файлы/проект — план "
    "ОБЯЗАН содержать шаги создания каждого артефакта (через datafiles) ПОСЛЕ "
    "исследовательских шагов. План только из поисковых шагов = невыполненная задача. "
    "Финальный шаг плана всегда проверяет фактическое наличие созданных файлов через "
    "files.list_dir/read_text, а не пересказывает намерения.\n"
    "\n"
    "КАК ВЫБИРАТЬ ИСПОЛНИТЕЛЯ (по когнитивному стилю, описание каждой модели "
    "дано в списке исполнителей ниже):\n"
    "  - Строгий процедурный анализ, проверка корректности, аудит → модель "
    "с формальным чек-листом (proposer_b).\n"
    "  - Неоднозначные полевые данные, смысл и подтекст, описание/интерпретация "
    "→ интерпретативная модель (proposer_a).\n"
    "  - Быстрые однозначные шаги, простой вызов инструмента, чтение файла "
    "→ скоростная минималистичная модель (proposer_c).\n"
    "Распределяй равномерно: не вешай всю работу на одну модель.\n"
    "\n"
    "Ответ верни ТОЛЬКО в формате JSON, без пояснений вокруг:\n"
    '{{"reasoning": "кратко замысел", "steps": [{{"step_id": "s1", '
    '"title": "коротко", "instruction": "что сделать исполнителю + путь файла", '
    '"assignee": "proposer_a", "tools": ["files"], "depends_on": []}}]}}\n'
)

_ASSEMBLE_SYSTEM = (
    "Ты распорядитель. Ниже задача пользователя и результаты шагов, "
    "выполненных исполнителями. Собери из них ИТОГОВЫЙ ответ по-русски.\n"
    "Правила:\n"
    "1. Опирайся только на результаты шагов. Не добавляй фактов от себя.\n"
    "2. Если шаг не удался, честно скажи, что именно не получилось.\n"
    "3. Если у шага помечено «есть вопросы» — не выдавай его данные за "
    "проверенный факт, а предупреди пользователя об этом.\n"
    "4. Формат:\n"
    "ОТВЕТ: <итоговый ответ>\n"
    "ОБОСНОВАНИЕ: <из каких шагов он собран>"
)


def _extract_json(text: str) -> dict[str, Any] | None:
    """
    Достать JSON-объект из ответа модели.

    Малые и средние модели упаковывают JSON в ```-блоки, дописывают
    пояснения до и после. Поэтому берём подстроку по фигурным скобкам,
    а не полагаемся на json.loads всего текста.
    """
    if not text:
        return None
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    start = cleaned.find("{")
    while start != -1:
        depth = 0
        for index in range(start, len(cleaned)):
            char = cleaned[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    try:
                        parsed = json.loads(cleaned[start:index + 1])
                        if isinstance(parsed, dict):
                            return parsed
                    except json.JSONDecodeError:
                        break
        start = cleaned.find("{", start + 1)
    return None


def _split_answer(text: str) -> tuple[str, str]:
    """Разделить «ОТВЕТ:/ОБОСНОВАНИЕ:». Если разметки нет — весь текст ответ."""
    if not text:
        return "", ""
    match = re.search(r"ОТВЕТ\s*:(.*?)(?:ОБОСНОВАНИЕ\s*:(.*))?$", text,
                      re.IGNORECASE | re.DOTALL)
    if match:
        answer = (match.group(1) or "").strip()
        rationale = (match.group(2) or "").strip()
        if answer:
            return answer, rationale
    return text.strip(), ""


# Регэксп ловит Windows-пути: C:\..., C:/..., D:\... и т.п.
_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|]+", re.IGNORECASE)


def _extract_absolute_paths(text: str) -> list[str]:
    r"""
    Достать все абсолютные пути (C:\...) из текста задачи.

    Нужно, чтобы передать исполнителю ТОЧНЫЙ путь из задачи, а не позволить
    планировщику «угадать» его (из-за этого файл искали не там — см. кейс
    с Test_for_AI.geojson, где путь был изменён на data\test).
    """
    found: list[str] = []
    for m in _PATH_RE.finditer(text or ""):
        p = m.group(0).rstrip('.,;:') or m.group(0)
        # Оставляем только пути, которые похожи на файлы/папки существующие
        # или с расширением — отбрасываем случайные вхождения
        if "\\" in p or "/" in p:
            if p not in found:
                found.append(p)
    return found[:5]  # достаточно первых путей


class Orchestrator:
    """
    Планирует шаги, поручает их локальным исполнителям, собирает итог.

    Зависимости внедряются извне (шлюз, список исполнителей, функция запуска
    исполнителя) — сам класс не знает ни о графе, ни о реестре инструментов.
    """

    version = ORCHESTRATOR_VERSION

    def __init__(
        self,
        gateway: LLMGateway,
        placement: OrchestratorPlacement = OrchestratorPlacement.AUTO,
        cloud_available: bool = False,
        max_steps: int = MAX_STEPS,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        planner_model: str | None = None,
    ):
        self.gateway = gateway
        self.placement = placement
        self.cloud_available = cloud_available
        self.max_steps = max(1, int(max_steps))
        self.on_event = on_event
        # Явный выбор локального планировщика. Пусто = модель из конфига.
        self.planner_model = planner_model or None

    def _emit(self, kind: str, **payload: Any) -> None:
        if self.on_event:
            try:
                self.on_event({"kind": kind, "actor": "orchestrator", **payload})
            except Exception:  # noqa: BLE001 — UI не должен ломать выполнение
                pass

    # ---- выбор модели планировщика ---------------------------------------

    def _planner_specs(self) -> list[tuple[Any, bool]]:
        """
        Цепочка моделей планировщика: [(spec, это_облако), ...].

        Порядок отражает placement, но локальная модель всегда в конце —
        чтобы недоступность облака не отменяла задачу.
        """
        cloud_chain = self.gateway.cloud_judge_chain()
        # Отдельная роль planner'а: см. orchestrator.local в models.yaml.
        # Если пользователь выбрал модель явно — берём её.
        if self.planner_model and hasattr(self.gateway, "orchestrator_spec_for"):
            local_spec = self.gateway.orchestrator_spec_for(self.planner_model)
        elif hasattr(self.gateway, "orchestrator_spec"):
            local_spec = self.gateway.orchestrator_spec()
        else:
            local_spec = self.gateway.judge_specs()[1]
        want_cloud = (
            self.placement == OrchestratorPlacement.CLOUD
            or (self.placement == OrchestratorPlacement.AUTO and self.cloud_available)
        )
        chain: list[tuple[Any, bool]] = []
        if want_cloud and self.cloud_available:
            chain.extend((spec, True) for spec in cloud_chain)
        chain.append((local_spec, False))
        return chain

    # ---- планирование -----------------------------------------------------

    def plan(
        self,
        task_prompt: str,
        available_agents: list[dict[str, Any]],
        available_tools: list[dict[str, Any]],
        context_summary: str = "",
        task_mode: str | None = None,
    ) -> Plan:
        started = time.perf_counter()
        agents_text = "\n".join(
            f"- {a['id']}: модель {a.get('model', '?')}, {a.get('purpose', '')}"
            for a in available_agents) or "- (исполнителей нет)"
        tools_text = "\n".join(
            f"- {t['name']}: {t.get('description', '')[:110]}"
            for t in available_tools) or "- (инструментов нет)"
        user_message = (
            f"ЗАДАЧА ПОЛЬЗОВАТЕЛЯ:\n{task_prompt}\n\n"
            f"ДОСТУПНЫЕ ИСПОЛНИТЕЛИ:\n{agents_text}\n\n"
            f"ДОСТУПНЫЕ ИНСТРУМЕНТЫ:\n{tools_text}"
        )
        # Явные пути из задачи — планировщик должен вставить их в шаги ДОСЛОВНО
        paths = _extract_absolute_paths(task_prompt)
        if paths:
            user_message += (
                "\n\nАБСОЛЮТНЫЕ ПУТИ ИЗ ЗАДАЧИ (вставь в instruction шагов "
                "ДОСЛОВНО, не переписывай):\n" + "\n".join(f"  {p}" for p in paths))
        if context_summary:
            user_message += f"\n\nКОНТЕКСТ:\n{context_summary}"

        system = _PLAN_SYSTEM.format(max_steps=self.max_steps)
        last_error = ""
        for spec, is_cloud in self._planner_specs():
            self._emit("plan_start", model=spec.model,
                       placement="cloud" if is_cloud else "local")
            # Планировщик работает БЕЗ инструментов: его задача — разделить
            # работу, а не выполнять её самому.
            response = self.gateway.chat(
                spec, [{"role": "system", "content": system},
                       {"role": "user", "content": user_message}],
                tools=None,
                task_mode=None if is_cloud else "local_only",
            )
            if not response.ok:
                last_error = response.error or "неизвестная ошибка"
                self._emit("plan_error", model=spec.model, error=last_error)
                continue
            data = _extract_json(response.text)
            if not data or not isinstance(data.get("steps"), list) or not data["steps"]:
                last_error = "планировщик не вернул корректный JSON с шагами"
                self._emit("plan_error", model=spec.model, error=last_error)
                continue
            content_hints = {t["name"]: t.get("content_hints") or []
                             for t in available_tools if t.get("content_hints")}
            steps = self._ensure_creation_steps(
                self._normalize_steps(data["steps"], available_agents,
                                      available_tools,
                                      task_paths=_extract_absolute_paths(task_prompt),
                                      content_hints=content_hints),
                available_agents, available_tools, task_prompt)
            if not steps:
                last_error = "в плане не осталось выполнимых шагов"
                self._emit("plan_error", model=spec.model, error=last_error)
                continue
            plan = Plan(
                steps=steps, reasoning=str(data.get("reasoning") or "").strip(),
                model=spec.model,
                placement=(OrchestratorPlacement.CLOUD if is_cloud
                           else OrchestratorPlacement.LOCAL),
                ok=True, latency_ms=int((time.perf_counter() - started) * 1000),
            )
            self._emit("plan_ready", model=spec.model, steps=len(steps))
            return plan

        # Ни один планировщик не справился: задача выполняется одним шагом
        # целиком — это лучше, чем вернуть пользователю ошибку.
        fallback_agent = available_agents[0]["id"] if available_agents else ""
        return Plan(
            steps=[PlanStep(step_id="s1", title="Выполнить задачу целиком",
                            instruction=task_prompt, assignee=fallback_agent,
                            tools=[t["name"] for t in available_tools])],
            reasoning="План не составлен, задача передана одному исполнителю целиком",
            model="", ok=False, error=last_error,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )

    def _normalize_steps(self, raw_steps: list[Any],
                         available_agents: list[dict[str, Any]],
                         available_tools: list[dict[str, Any]],
                         task_paths: list[str] | None = None,
                         content_hints: dict[str, list[str]] | None = None) -> list[PlanStep]:
        """
        Привести шаги от модели к валидному плану.

        Модели ошибаются предсказуемо: придумывают несуществующих исполнителей
        и инструменты, ссылаются на несуществующие шаги, теряют step_id,
        а главное — «угадывают» путь к файлу вместо того, чтобы взять из задачи.
        task_paths — абсолютные пути из задачи; принудительно подставляем их
        в инструкцию шага, чтобы исполнитель не искал файл не там.
        """
        agent_ids = [a["id"] for a in available_agents]
        tool_names = {t["name"] for t in available_tools}
        steps: list[PlanStep] = []
        for index, raw in enumerate(raw_steps[:self.max_steps]):
            if not isinstance(raw, dict):
                continue
            instruction = str(raw.get("instruction") or raw.get("task") or "").strip()
            if not instruction:
                continue
            step_id = str(raw.get("step_id") or f"s{index + 1}").strip() or f"s{index + 1}"
            assignee = str(raw.get("assignee") or "").strip()
            if assignee not in agent_ids:
                # Круговое распределение: не даём всю работу одной модели
                assignee = agent_ids[index % len(agent_ids)] if agent_ids else ""
            raw_tools = raw.get("tools") or []
            if isinstance(raw_tools, str):
                raw_tools = [raw_tools]
            tools = [str(t) for t in raw_tools if str(t) in tool_names]

            # Гарантируем правильный путь: если в инструкции нет ни одного
            # абсолютного пути из задачи, подставляем его явно.
            if task_paths:
                missing = [p for p in task_paths if p.lower() not in instruction.lower()]
                if missing:
                    instruction = (instruction
                                   + "\n\nПУТЬ К ФАЙЛУ (используй дословно): "
                                   + ", ".join(missing))

            steps.append(PlanStep(
                step_id=step_id,
                title=str(raw.get("title") or instruction[:60]).strip(),
                instruction=instruction, assignee=assignee, tools=tools,
                depends_on=[str(d) for d in (raw.get("depends_on") or [])],
            ))

        # Ссылки на несуществующие шаги удаляем, иначе планировщик
        # заблокировал бы выполнение сам себя
        known = {s.step_id for s in steps}
        for step in steps:
            step.depends_on = [d for d in step.depends_on if d in known and d != step.step_id]
        steps = self._break_cycles(steps)
        return self._repair_tools(steps, available_tools, content_hints)

    # ---- детерминированный ремонт назначения инструментов ------------------

    # Глаголы ЧТЕНИЯ (стемы: покрывают все словоформы). Шагу с таким глаголом
    # нужен files/qgis, а НЕ datafiles (она только пишет).
    # ВАЖНО: "структур" убран — он ловил «создать СТРУКТУРУ папок» (задача
    # f4af7315, шаг s4) и превращал шаг записи в read-only.
    _READ_VERB_STEMS = (
        "прочита", "читай", "чтение", "загруж", "загруз", "открой файл",
        "посчита", "подсчит", "проанализ",
        "извлек", "атрибут",
    )
    # Глаголы ЗАПИСИ. Задача 4c38f0eb (22.08): узкий список покрыл только
    # офисные форматы, шаги «Создание tsconfig.json» / «Инициализация
    # package.json» не совпали ни с одним стемом — 4 из 7 шагов остались
    # с read-only files, папка осталась пустой при статусе done.
    _WRITE_VERB_STEMS = (
        "созда", "запиши", "запись", "перезапис", "сохрани", "сохранение",
        "инициализ", "сгенерируй", "сформируй", "подготовь файл",
        "напиши", "написание",
    )

    def _repair_tools(self, steps: list[PlanStep],
                      available_tools: list[dict[str, Any]],
                      content_hints: dict[str, list[str]] | None = None) -> list[PlanStep]:
        """
        Детерминированная коррекция tools у шагов ПОСЛЕ LLM-планировщика.

        Причина: планировщик регулярно игнорирует инструкцию промпта
        «datafiles только пишет, не читает» — подтверждено задачей b02a7a21b99a
        (17.08), где ВСЕ 6 шагов получили tools=["datafiles"]/["charts"], ни
        одному не достался files. Исполнители честно отвечали «нет функции
        чтения». Обратный случай — задача b75cdf922ce3 (23.08): все 6 шагов
        получили только read-only files, ни одного datafiles, и НИ ОДИН файл
        не был создан.

        Здесь чиним оба класса ошибок по тексту инструкции, без LLM:
          - глагол записи в инструкции -> добавляем datafiles;
          - глагол чтения -> убираем datafiles, добавляем files;
          - гео-подсказки -> добавляем qgis.
        Инструмент добавляется, только если он вообще доступен задаче.
        """
        available = {t["name"] for t in available_tools}
        # Доменные подсказки из манифестов (content_hints): ядро не знает
        # имён конкретных инструментов — сверяет текст шага со словами,
        # которые каждый плагин объявил сам в своём манифесте (критика
        # ревью 25.08: QGIS-логика была захардкожена в ядре).
        hints = content_hints or {}
        for step in steps:
            text = f"{step.title} {step.instruction}".lower()
            tools = list(step.tools)
            has_write_verb = any(s in text for s in self._WRITE_VERB_STEMS)
            has_read_verb = any(s in text for s in self._READ_VERB_STEMS)
            # Инструменты, чьи доменные подсказки встретились в шаге
            hinted = [name for name, words in hints.items()
                      if any(w in text for w in words)]

            if has_write_verb:
                # Шаг создаёт/меняет файлы — нужен пишущий инструмент.
                if not ("datafiles" in tools or hinted):
                    if "datafiles" in available:
                        tools.append("datafiles")
                # Доменный инструмент (напр. qgis) добавляем только если
                # он умеет писать и объявлен доступным.
                for h in hinted:
                    if h in available and h not in tools:
                        tools.append(h)
            if has_read_verb:
                if "datafiles" in tools:
                    tools.remove("datafiles")
                if "files" in available and "files" not in tools:
                    tools.append("files")
                for h in hinted:
                    if h in available and h not in tools:
                        tools.append(h)
            step.tools = tools
        return steps

    def _ensure_creation_steps(
        self,
        steps: list[PlanStep],
        available_agents: list[dict[str, Any]],
        available_tools: list[dict[str, Any]],
        task_prompt: str,
    ) -> list[PlanStep]:
        """
        Гарантировать наличие шага СОЗДАНИЯ файлов, если задача этого требует.

        Задача 2f073f8a (24.08): планировщик составил план ТОЛЬКО из поисковых
        шагов websearch и объявил задачу выполненной — фронтенд не создан.
        Промптовое правило «план обязан содержать шаги создания» проигнорировано,
        как и все промптовые запреты до него. Здесь детерминированная проверка:
        если задача требует создания (глаголы создания + пути к файлам в тексте),
        а в плане нет ни одного шага с пишущим инструментом — добавляется
        финальный шаг создания с полным заданием пользователя.
        """
        write_tools = {"datafiles", "qgis"}
        has_creation_step = any(
            write_tools.intersection(step.tools) for step in steps)
        if has_creation_step:
            return steps

        text = task_prompt.lower()
        creation_verbs = ("создай", "создать", "сгенерируй", "сформируй",
                          "напиши", "сделай проект", "сделай сайт",
                          "сделай backend", "сделай фронтенд", "заполни",
                          "доработай", "перепиши", "реализуй")
        needs_creation = any(v in text for v in creation_verbs)
        has_target_path = bool(_extract_absolute_paths(task_prompt))
        if not (needs_creation and has_target_path):
            return steps

        if not available_agents:
            return steps
        if not any(t["name"] in write_tools for t in available_tools):
            return steps

        writer = available_agents[0]["id"]
        write_tool = ("datafiles" if any(t["name"] == "datafiles"
                                         for t in available_tools) else "qgis")
        paths = _extract_absolute_paths(task_prompt)
        path_line = ("\n\nПУТИ ИЗ ЗАДАЧИ (используй дословно): "
                     + ", ".join(paths)) if paths else ""
        tool_names = {t["name"] for t in available_tools}
        step_tools = [write_tool] + (["files"] if "files" in tool_names else [])
        steps.append(PlanStep(
            step_id=f"s{len(steps) + 1}",
            title="Создать все файлы проекта по задаче",
            instruction=(task_prompt + path_line
                         + "\n\nЭто ЕДИНСТВЕННЫЙ шаг создания: предыдущие шаги "
                         "были только исследованием. Создай ВСЕ файлы, "
                         "перечисленные в задаче, через инструмент "
                         f"{write_tool}. Не пересказывай содержимое текстом — "
                         "запиши каждый файл инструментом."),
            assignee=writer,
            tools=step_tools,
        ))
        self._emit("plan_patched",
                   reason="план без шагов создания — добавлен шаг создания",
                   steps=len(steps))
        return steps

    @staticmethod
    def _break_cycles(steps: list[PlanStep]) -> list[PlanStep]:
        """
        Разорвать циклы в зависимостях.

        Модель может выдать s1->s2 и s2->s1. Без проверки планировщик завис
        бы навсегда, поэтому оставляем только зависимости на шаги, объявленные
        РАНЬШЕ по списку — порядок из плана считаем намерением автора.
        """
        position = {step.step_id: i for i, step in enumerate(steps)}
        for step in steps:
            step.depends_on = [d for d in step.depends_on
                               if position.get(d, 10**6) < position[step.step_id]]
        return steps

    def adversarial_review(
        self,
        task_prompt: str,
        final_answer: str,
        review_fn: Callable[[str], str] | None = None,
    ) -> dict[str, Any]:
        """
        B5 (24.08): adversarial-ревью. Дешёвый вызов ДРУГОЙ модели с вопросом
        «найди несоответствие между заданием и итоговым ответом». Ловит класс
        «успех без результата» дешевле полного ансамбля.

        review_fn — функция, которую injectует main_graph: она вызывает модель
        верификатора и возвращает текст. None = ревью недоступно.
        Возвращает {"ok": bool, "issues": str} — issues пуст, если проблем нет.
        """
        if review_fn is None:
            return {"ok": True, "issues": ""}
        prompt = (
            "Ты скептический проверяющий. Сравни ЗАДАЧУ и ЗАЯВЛЕННЫЙ ИТОГ. "
            "Найди конкретные несоответствия: заявлено создание файлов, которых "
            "нет; использованы не те версии/пакеты, что требовались; пропущены "
            "пункты задания. Если всё сходится — ответь ровно OK. Иначе перечисли "
            "проблемы кратко списком.\n\n"
            "Границы проверки:\n"
            "- Дополнительные служебные файлы (README, вспомогательные скрипты, "
            "тесты) НЕ являются ошибкой — не перечисляй их.\n"
            "- Суди только соответствие ЗАДАЧЕ. Упоминания служебных путей "
            "исполнения (временные каталоги вида localis_tb_...) игнорируй: "
            "не требуй их указания в ответе и не считай отклонением.\n"
            "- Не выдумывай требования, которых нет в тексте задания.\n\n"
            f"ЗАДАЧА:\n{task_prompt[:3000]}\n\n"
            f"ЗАЯВЛЕННЫЙ ИТОГ:\n{final_answer[:3000]}"
        )
        try:
            response = review_fn(prompt)
        except Exception as exc:  # noqa: BLE001 — ревью не должно ломать задачу
            return {"ok": True, "issues": f"(ревью недоступно: {exc})"}
        text = (response or "").strip()
        if not text:
            return {"ok": True, "issues": ""}
        if text.upper().startswith("OK"):
            return {"ok": True, "issues": ""}
        return {"ok": False, "issues": text[:2000]}

    # ---- сборка итога -----------------------------------------------------

    def assemble(
        self,
        task_prompt: str,
        plan: Plan,
        results: list[StepResult],
        task_mode: str | None = None,
    ) -> FinalDecision:
        """
        Собрать итоговый ответ из результатов шагов.

        Если сборщик недоступен (нет облака и локальная модель дала сбой),
        возвращаем склейку результатов шагов: пользователь всё равно получит
        проделанную работу, а не сообщение об ошибке.
        """
        blocks: list[str] = []
        for result in results:
            head = f"[{result.step_id}] {result.title} (исполнитель {result.assignee})"
            if result.ok:
                facts = "; ".join(
                    f"{r.tool}.{r.action}: {(r.summary or r.error or '')[:200]}"
                    for r in result.tool_results[-3:])
                body = result.answer or "(пустой ответ)"
                if facts:
                    body += f"\nФакты инструментов: {facts}"
                # Предупреждения показываем сборщику: иначе он примет
                # догадку исполнителя за проверенный факт
                if result.warnings:
                    body += ("\nВНИМАНИЕ, к этому шагу есть вопросы: "
                             + "; ".join(result.warnings))
            else:
                body = f"НЕ ВЫПОЛНЕН: {result.error}"
            blocks.append(f"{head}\n{body}")
        steps_text = "\n\n".join(blocks) or "(шаги не выполнялись)"
        user_message = (f"ЗАДАЧА:\n{task_prompt}\n\nРЕЗУЛЬТАТЫ ШАГОВ:\n{steps_text}")

        for spec, is_cloud in self._planner_specs():
            self._emit("assemble_start", model=spec.model,
                       placement="cloud" if is_cloud else "local")
            response = self.gateway.chat(
                spec, [{"role": "system", "content": _ASSEMBLE_SYSTEM},
                       {"role": "user", "content": user_message}],
                tools=None,
                task_mode=None if is_cloud else "local_only",
            )
            if not response.ok or not (response.text or "").strip():
                self._emit("assemble_error", model=spec.model,
                           error=response.error or "пустой ответ")
                continue
            answer, rationale = _split_answer(response.text)
            return FinalDecision(
                answer=answer,
                rationale=rationale or plan.reasoning,
                decided_by=("orchestrator_cloud" if is_cloud else "orchestrator_local"),
                model=spec.model, used_cloud=is_cloud,
                subagent_calls=[f"{r.assignee}:{r.step_id}" for r in results],
            )

        # Крайний случай: сборщик недоступен — отдаём работу исполнителей как есть
        done = [r for r in results if r.ok and r.answer]
        fallback = "\n\n".join(f"{r.title}: {r.answer}" for r in done) or (
            "Ни один шаг не удалось выполнить.")
        return FinalDecision(
            answer=fallback,
            rationale="Итог собран без модели-сборщика: она была недоступна.",
            decided_by="fallback", model="", used_cloud=False,
            subagent_calls=[f"{r.assignee}:{r.step_id}" for r in results],
        )
