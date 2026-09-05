"""
CLI — альтернативный вход в платформу (без графического интерфейса).

Примеры:
    python main.py --task "Опиши слой C:\data\wells.shp"
    python main.py --mode local_only --tools qgis --task "Экспортируй слой в GeoJSON"
    python main.py --status          # состояние системы
    python main.py --list-tools      # плагины и их версии
    python main.py --history 10      # последние задачи
    python main.py --ui              # запустить панель управления
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Версия платформы. Держится в одном месте и должна совпадать с верхней
# записью в CHANGELOG.md — она печатается в --status и уходит в трейсы
# Langfuse, поэтому по ней потом восстанавливают, какой код дал результат.
PLATFORM_VERSION = "1.6.0"


def _print_event(event: dict) -> None:
    """Компактный поток событий выполнения в консоль."""
    kind = event.get("kind")
    if kind == "node_start":
        print(f"  -> {event['node']}...", flush=True)
    elif kind == "node_end":
        details = {k: v for k, v in event.items() if k not in ("kind", "node")}
        print(f"  <- {event['node']}: {str(details)[:150]}", flush=True)
    elif kind == "node_skip":
        print(f"  ~~ {event['node']} пропущен: {event.get('reason')}", flush=True)
    elif kind == "tool_call":
        who = event.get("proposer_id") or event.get("actor") or "?"
        print(f"     [{who}] вызов {event['tool']}.{event['action']}", flush=True)
    elif kind == "tool_result":
        mark = "OK" if event.get("ok") else "ОШИБКА"
        print(f"     -> {mark}: {str(event.get('summary'))[:120]}", flush=True)
    elif kind == "subagent_call":
        # Кто инициатор: арбитр или инструмент «Локальные агенты»
        actor = {"judge": "арбитр", "local_agents": "агент-координатор"}.get(
            str(event.get("actor", "")), str(event.get("actor") or "система"))
        target = event.get("proposer_id") or "локальную модель"
        print(f"     [{actor}] спрашивает {target}", flush=True)
    elif kind == "error":
        print(f"     ! ошибка: {str(event.get('error'))[:160]}", flush=True)


def cmd_status() -> int:
    from workflows.main_graph import Platform
    platform = Platform()
    status = platform.mode_controller.status()
    stats = platform.blackboard.stats()
    print(f"Платформа версии {PLATFORM_VERSION}")
    print(f"Режим:            {status.mode}")
    print(f"Облако:           {'доступно' if status.allowed else 'недоступно'} — {status.reason}")
    print(f"Circuit Breaker:  {status.breaker_state} (сбоев: {status.fail_counter})")
    print(f"Схема Blackboard: v{stats['schema_version']}")
    print(f"Задач в журнале:  {stats['tasks_total']} {stats['by_status']}")

    # --- Статистика успеха и латентности (из Blackboard) ---
    import sqlite3 as _sq
    from datetime import datetime as _dt
    _db = _sq.connect(str(ROOT / "data" / "blackboard.sqlite3"))
    _db.row_factory = _sq.Row
    _rows = _db.execute("SELECT task_id,status,created_at FROM tasks").fetchall()
    _done = sum(1 for r in _rows if r["status"] == "done")
    _failed = sum(1 for r in _rows if r["status"] == "failed")
    if _rows:
        print(f"Успешность:       {_done}/{len(_rows)} = {100*_done/len(_rows):.0f}% "
              f"(провалов: {_failed})")
    _wall = []
    for r in _rows:
        if r["status"] != "done":
            continue
        _last = _db.execute("SELECT MAX(created_at) t FROM decisions_log WHERE task_id=?",
                            (r["task_id"],)).fetchone()
        if _last and _last["t"]:
            _wall.append((_dt.fromisoformat(_last["t"]) -
                          _dt.fromisoformat(r["created_at"])).total_seconds())
    if _wall:
        _wall.sort()
        _n = len(_wall)
        print(f"Wall-clock (done): медиана {_wall[_n//2]:.0f}с · "
              f"p25 {_wall[_n//4]:.0f}с · p75 {_wall[3*_n//4]:.0f}с")
    _llm_stats = platform.blackboard.llm_stats()
    if _llm_stats:
        print("Модели за 14 дней (реальные вызовы):")
        for p in _llm_stats:
            print(f"  {p['model']:44} n={p['n']:3} "
                  f"avg={p['ms']/1000:6.1f}с tps={p['tps'] or 0:5.1f}")
    else:
        print("Модели: LLM-вызовов за 14 дней нет")
    _db.close()
    print(f"Инструменты:      {len(platform.registry.available())} готовы "
          f"из {len(platform.registry.entries)}")
    print("Модели в Ollama:")
    for name, present in platform.gateway.local_models_available().items():
        print(f"  {'+' if present else '-'} {name}")
    return 0


def cmd_list_tools() -> int:
    from workflows.main_graph import Platform
    platform = Platform()
    if not platform.registry.entries:
        print("Плагины не найдены в папке tools/")
        return 1
    for entry in platform.registry.entries.values():
        print(f"[{entry.status.value:11}] {entry.name} v{entry.manifest.version} — {entry.message}")
        if entry.instance is not None:
            for action in entry.instance.actions():
                print(f"      • {action.name}: {action.description}")
    return 0


def cmd_planner_models() -> int:
    """Модели, доступные планировщику, с пояснениями из конфига."""
    from workflows.main_graph import Platform
    platform = Platform()
    try:
        candidates = platform.gateway.orchestrator_candidates()
        default_model = platform.gateway.orchestrator_spec().model
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка чтения models.yaml: {exc}")
        return 1
    chosen = str((platform.settings.get("orchestrator") or {}).get("model", ""))
    print("Модели для локального планировщика (--planner-model МОДЕЛЬ):\n")
    for spec in candidates:
        marks = []
        if spec.model == default_model:
            marks.append("по умолчанию")
        if chosen and spec.model == chosen:
            marks.append("выбрана в настройках")
        suffix = f"  [{', '.join(marks)}]" if marks else ""
        print(f"  {spec.model}{suffix}")
        label = str(spec.extra.get("label") or "")
        if label:
            print(f"      {label}")
    print("\nГлавное требование — не размер модели, а стабильный JSON: "
          "если план\nне разобрался, задача уйдёт одному исполнителю целиком.")
    return 0


def cmd_history(limit: int) -> int:
    from workflows.main_graph import Platform
    platform = Platform()
    rows = platform.blackboard.list_tasks(limit=limit)
    if not rows:
        print("История пуста.")
        return 0
    for row in rows:
        cloud = " [облако]" if row.get("used_cloud") else ""
        print(f"{row['created_at'][:19]}  {row['status']:8} {row['mode']:10}{cloud}  "
              f"{row['task_id']}  {row['prompt'][:70]}")
    return 0


def cmd_show(task_id: str) -> int:
    from workflows.main_graph import Platform
    platform = Platform()
    record = platform.blackboard.load_record(task_id)
    if record is None:
        print(f"Задача {task_id} не найдена.")
        return 1
    print(f"Задача: {record.task.prompt}")
    print(f"Статус: {record.status} | режим: {record.task.mode.value}")
    if record.routing:
        print(f"Маршрутизация: {record.routing.complexity.value} — {record.routing.reason}")
    for proposal in record.proposals:
        mark = "OK" if proposal.ok else "СБОЙ"
        print(f"\n[{proposal.proposer_id}] {proposal.model} ({mark}, {proposal.latency_ms} мс)")
        print((proposal.answer or proposal.error or "")[:600])
    if record.plan:
        where = {"cloud": "в облаке", "local": "локально"}.get(
            record.plan.placement.value, record.plan.placement.value)
        print(f"\nПлан от оркестратора ({record.plan.model or 'без модели'}, {where}):")
        if record.plan.reasoning:
            print(f"  Замысел: {record.plan.reasoning}")
        if record.plan.error:
            print(f"  Проблема планирования: {record.plan.error}")
        for step in record.plan.steps:
            deps = f" после {', '.join(step.depends_on)}" if step.depends_on else ""
            tools_text = f" [{', '.join(step.tools)}]" if step.tools else ""
            print(f"  {step.step_id}. {step.title} -> {step.assignee}{tools_text}{deps}")
    for result in record.step_results:
        mark = "OK" if result.ok else "СБОЙ"
        print(f"\n[шаг {result.step_id}] {result.title} — {result.assignee} "
              f"({result.model}, {mark}, {result.latency_ms} мс)")
        print((result.answer or result.error or "")[:600])
    if record.verdict:
        print(f"\nПроверка: консенсус={record.verdict.consensus} "
              f"согласны={record.verdict.agreeing} — {record.verdict.reason}")
    if record.final:
        print(f"\nИТОГ ({record.final.decided_by}, модель {record.final.model}):")
        print(record.final.answer)
        if record.final.rationale:
            print(f"Обоснование: {record.final.rationale}")
    print("\nВызовы инструментов:")
    for call in platform.blackboard.get_tool_calls(task_id):
        mark = "OK" if call["ok"] else "ОШИБКА"
        print(f"  {call['caller']:12} {call['tool']}.{call['action']} [{mark}] "
              f"{(call['summary'] or call['error'] or '')[:90]}")
    return 0


def cmd_run(prompt: str, mode: str, tools: list[str] | None, quiet: bool,
            placement: str | None = None, planner_model: str | None = None,
            meta_json: str | None = None) -> int:
    from workflows.main_graph import Platform, Workflow
    from core.schemas import ExecutionMode, Task

    platform = Platform(on_event=None if quiet else _print_event)
    # Размещение планировщика передаётся через metadata задачи: так ядро
    # не обрастает отдельным параметром ради одного режима
    metadata = {"orchestrator_placement": placement} if placement else {}
    if planner_model:
        metadata["orchestrator_model"] = planner_model
    if meta_json:
        import json as _json
        try:
            metadata.update(_json.loads(meta_json))
        except ValueError as exc:
            print(f"ОШИБКА: --meta не JSON: {exc}")
            return 2
    task = Task(prompt=prompt, mode=ExecutionMode(mode), allowed_tools=tools,
                metadata=metadata)
    mode_ru = {"auto": "автоматический", "local_only": "строго локальный",
               "orchestrated": "оркестратор"}.get(mode, mode)
    print(f"Задача {task.task_id} (режим {mode_ru})")
    state = Workflow(platform).run(task)

    if state.get("error"):
        print(f"\nОШИБКА: {state['error']}")
        return 1
    final = state.get("final")
    print("\n" + "=" * 70)
    print(final.answer if final else "Ответ не получен.")
    print("=" * 70)
    if final:
        print(f"Решение принято: {final.decided_by} (модель {final.model}"
              f"{', облако' if final.used_cloud else ', локально'})")
        if final.rationale:
            print(f"Обоснование: {final.rationale[:400]}")
    print(f"Подробности: python main.py --show {task.task_id}")
    return 0


def cmd_index(paths: list[str]) -> int:
    """Наполнение локальной базы знаний для Retrieval Agent."""
    from workflows.main_graph import Platform
    platform = Platform()
    agent = platform.retrieval_agent()
    total = 0
    for raw in paths:
        path = Path(raw)
        files = sorted(path.rglob("*.txt")) + sorted(path.rglob("*.md")) if path.is_dir() else [path]
        for file_path in files:
            try:
                added = agent.index_file(file_path)
                total += added
                print(f"  + {file_path.name}: {added} фрагментов")
            except Exception as exc:  # noqa: BLE001
                print(f"  ! {file_path.name}: {type(exc).__name__}: {exc}")
    print(f"Готово. Добавлено фрагментов: {total}. Всего в индексе: {len(agent.index)}")
    print("Не забудьте включить флаг retrieval в config/settings.yaml")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-platform",
        description="Локально-первая мультиагентная платформа (CLI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--task", "-t", help="Текст задачи")
    parser.add_argument("--mode", "-m",
                        choices=["auto", "local_only", "orchestrated"], default=None,
                        help="Режим: auto (локально+облако), local_only (строго офлайн) "
                             "или orchestrated (планировщик делит задачу на шаги)")
    parser.add_argument("--tools", help="Разрешённые инструменты через запятую, напр. qgis")
    parser.add_argument("--planner", choices=["cloud", "local", "auto"], default=None,
                        help="Где работает планировщик в режиме orchestrated "
                             "(исполнители шагов всегда локальные)")
    parser.add_argument("--planner-model", default=None, metavar="МОДЕЛЬ",
                        help="Какой локальной моделью планировать в режиме "
                             "orchestrated (список: --planner-models)")
    parser.add_argument("--meta", default=None, metavar="JSON",
                        help='Метаданные задачи JSON, напр. --meta "{\"meeting_rounds\":3}"')
    parser.add_argument("--planner-models", action="store_true",
                        help="Модели, доступные планировщику, с замерами")
    parser.add_argument("--quiet", "-q", action="store_true", help="Без потока событий")
    parser.add_argument("--status", action="store_true", help="Состояние системы")
    parser.add_argument("--list-tools", action="store_true", help="Список плагинов")
    parser.add_argument("--history", type=int, metavar="N", help="Последние N задач")
    parser.add_argument("--show", metavar="TASK_ID", help="Детали задачи")
    parser.add_argument("--index", nargs="+", metavar="PATH",
                        help="Проиндексировать файлы/папки в базу знаний")
    parser.add_argument("--keys", action="store_true",
                        help="Показать состояние ключей доступа")
    parser.add_argument("--set-key", metavar="ИМЯ=ЗНАЧЕНИЕ",
                        help="Задать ключ, напр. OPENROUTER_API_KEY=sk-or-v1-...")
    parser.add_argument("--ui", action="store_true", help="Запустить панель управления")
    parser.add_argument("--version", action="version",
                        version=f"agent-platform {PLATFORM_VERSION}")
    return parser


def cmd_keys() -> int:
    """Показать состояние ключей доступа."""
    from config.secrets import STORE, secret_status
    print(f"Файл ключей: {STORE.path}")
    print("Вписать ключи можно в панели управления: экран «Ключи доступа».\n")
    for item in secret_status():
        if item["filled"]:
            where = "система" if item["source"] == "окружение" else "приложение"
            print(f"  [задан {where:11}] {item['env']:22} {item['masked']}")
        else:
            mark = "нужен" if item["required"] else "не задан"
            print(f"  [{mark:17}] {item['env']:22} —")
    return 0


def cmd_set_key(pair: str) -> int:
    """Задать ключ из командной строки: --set-key OPENROUTER_API_KEY=sk-..."""
    from config.secrets import SECRETS_BY_ENV, STORE, mask
    if "=" not in pair:
        print("Формат: --set-key ИМЯ_КЛЮЧА=значение")
        return 1
    env, _, value = pair.partition("=")
    env, value = env.strip(), value.strip()
    if env not in SECRETS_BY_ENV:
        print(f"Неизвестный ключ: {env}. Известные: {', '.join(SECRETS_BY_ENV)}")
        return 1
    STORE.set(env, value)
    if value:
        print(f"Ключ {env} сохранён: {mask(value)}")
    else:
        print(f"Ключ {env} удалён")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Ключи, введённые в интерфейсе, применяем и для CLI
    from config.secrets import load_secrets
    load_secrets()

    if args.keys:
        return cmd_keys()
    if args.set_key:
        return cmd_set_key(args.set_key)
    if args.ui:
        from ui.app import run_ui
        run_ui()
        return 0
    if args.status:
        return cmd_status()
    if args.list_tools:
        return cmd_list_tools()
    if args.planner_models:
        return cmd_planner_models()
    if args.history is not None:
        return cmd_history(args.history)
    if args.show:
        return cmd_show(args.show)
    if args.index:
        return cmd_index(args.index)
    if args.task:
        from config.loader import load_settings
        mode = args.mode or str(load_settings().get("mode", "auto"))
        tools = [t.strip() for t in args.tools.split(",")] if args.tools else None
        # --planner без --mode orchestrated не имеет смысла: подсказываем вместо
        # тихого игнорирования флага
        if (args.planner or args.planner_model) and mode != "orchestrated":
            print("Примечание: --planner и --planner-model работают только с "
                  "--mode orchestrated. Включаю режим оркестратора.")
            mode = "orchestrated"
        return cmd_run(args.task, mode, tools, args.quiet, args.planner,
                   args.planner_model, meta_json=args.meta)

    build_parser().print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
