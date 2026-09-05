"""
Проверка результата шага плана без обращения к модели.

Зачем: шаг считается успешным, если модель не упала. Но малая модель может
вернуть «готово, файл прочитан», ни разу не вызвав инструмент, — то есть
ответить по догадке. Такой шаг формально ok=True, а фактически бесполезен,
и сборщик итога примет выдумку за факт.

Здесь только детерминированные проверки: никаких вызовов LLM, поэтому
проверка ничего не стоит по времени и не может сама ошибиться.
"""

from __future__ import annotations

from pathlib import Path

from core.schemas import PlanStep, StepResult

# Фразы, которыми модели маскируют отказ. Проверено на реальных ответах:
# вместо честной ошибки они пишут «не могу получить доступ» и продолжают.
_REFUSAL_MARKERS = (
    "не могу получить доступ",
    "у меня нет доступа",
    "не имею возможности",
    "как языковая модель",
    "предоставьте содержимое",
    "пришлите файл",
)

# Слишком короткий ответ на шаг с инструментами почти всегда означает,
# что модель не поняла задание.
_MIN_ANSWER_CHARS = 12


def check_step(step: PlanStep, result: StepResult) -> StepResult:
    """
    Дополнить результат шага предупреждениями. Возвращает тот же объект.

    Не меняет ok: шаг мог выполниться, просто к нему есть вопросы. Решение,
    что делать с подозрительным шагом, принимает сборщик и пользователь —
    молча выбрасывать работу нельзя.
    """
    warnings: list[str] = []
    if not result.ok:
        return result

    answer = (result.answer or "").strip()
    lowered = answer.lower()

    # 1) Инструменты выданы, но не вызваны -> ответ построен на догадках
    if step.tools and not result.tool_results:
        warnings.append(
            f"инструменты {', '.join(step.tools)} были доступны, но ни один "
            "не вызван — ответ может быть основан на догадке")

    # 2) Все вызовы инструментов провалились, а ответ уверенный
    elif result.tool_results and not any(call.ok for call in result.tool_results):
        failed = "; ".join((call.error or "")[:80] for call in result.tool_results[-2:])
        warnings.append(f"все вызовы инструментов завершились ошибкой ({failed})")

    # 3) Пустой или подозрительно короткий ответ
    if not answer:
        warnings.append("пустой ответ исполнителя")
    elif len(answer) < _MIN_ANSWER_CHARS and step.tools:
        warnings.append(f"ответ короче {_MIN_ANSWER_CHARS} символов при работе с инструментами")

    # 4) Замаскированный отказ
    for marker in _REFUSAL_MARKERS:
        if marker in lowered:
            warnings.append(f"похоже на отказ выполнять задание: «{marker}»")
            break

    # 5) Запись не по тому пути (задача 931c894e5624, 23.08): исполнитель
    # записал файлы в папку прошлой попытки, все вызовы ok=1, и сборщик
    # принял работу. Здесь сверяем пути из успешных записей инструментов
    # с абсолютным путём из инструкции шага. Только детерминированная
    # логика, без LLM.
    _WRITE_TOOLS = {"datafiles", "files"}
    _WRITE_ACTIONS = ("write_text", "write_json", "write_csv", "write_geojson",
                      "write_excel", "write_docx", "append_text")
    written_roots: list[str] = []
    for call in result.tool_results:
        if not (call.ok and call.tool in _WRITE_TOOLS
                and any(call.action.startswith(a) for a in _WRITE_ACTIONS)):
            continue
        data_path = str((call.data or {}).get("path") or "")
        summary = call.summary or ""
        candidate = data_path or (summary.split("Создан файл ")[-1].split(" (")[0]
                                  if "Создан файл " in summary else "")
        if not candidate:
            continue
        try:
            from core.fs_paths import normalize as _norm
            written = Path(_norm(candidate))
            if written.suffix:
                written_roots.append(str(written.parent.resolve()))
        except (OSError, ValueError):
            continue

    if written_roots:
        from agents.orchestrator import _extract_absolute_paths
        wanted_dirs: list[str] = []
        for raw in _extract_absolute_paths(step.instruction):
            p = Path(raw.replace("/", "\\"))
            wanted_dirs.append(str((p.parent if p.suffix else p).resolve()))
        if wanted_dirs:
            offroot = [w for w in set(written_roots)
                       if not any(w == d or w.startswith(d + "\\")
                                  for d in wanted_dirs)]
            if offroot:
                warnings.append(
                    f"файлы записаны ВНЕ папки из задания ({'; '.join(offroot)} "
                    f"вместо {'; '.join(wanted_dirs)}) — результат не в том месте")

    # 6) Задание на запись, но ни одной успешной записи (задача de9dc117,
    # 24.08): шаг «Исправление init-db.ts» ответил текстом файла, не вызвав
    # пишущий инструмент; сборщик принял отчёт за факт. Детерминированная
    # проверка без LLM.
    _WRITE_TOOL_NAMES = {"datafiles"}
    if any(stem in lowered for stem in (
            "созда", "запиши", "перезапис", "переписал", "исправь",
            "сохрани", "инициализ", "обнови", "доработ")):
        has_write_tools = bool(step.tools & _WRITE_TOOL_NAMES) if isinstance(
            step.tools, set) else any(t in (_WRITE_TOOL_NAMES) for t in (step.tools or []))
        ok_writes = [c for c in result.tool_results
                     if c.ok and any(c.action.startswith(a) for a in _WRITE_ACTIONS)]
        if has_write_tools and not ok_writes and "файлы записаны ВНЕ" not in " ".join(warnings):
            warnings.append(
                "задание требует записи файлов, пишущий инструмент был выдан, "
                "но ни одной успешной записи не выполнено — ответ может быть "
                "описанием вместо результата")

    result.warnings = warnings
    result.suspicious = bool(warnings)
    return result
