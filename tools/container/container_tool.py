"""
Инструмент container: исполнение команд ВНУТРИ docker-контейнера TB.

Зачем (31.08): задачи Terminal-Bench исполняются в контейнере — входные файлы,
окружение и проверяемые артефакты живут в /app. Платформа на хосте не может
ни прочитать контекст задачи, ни запустить установку пакетов, ни выполнить
chmod/компиляцию. Этот инструмент даёт модели полноценный shell внутри
контейнера через `docker exec`.

Активация: инструмент работает ТОЛЬКО если задана переменная окружения
LOCALIS_TB_CONTAINER (имя/ID контейнера) — её выставляет адаптер при
TB-прогоне. Без неё инструмент UNAVAILABLE: в обычной работе платформа
не должна трогать чужие контейнеры.

Безопасность:
- Контейнер одноразовый (создаётся TB на одну задачу и удаляется после).
- Команда исполняется от root внутри изолированного контейнера — хост
  не затрагивается.
- Лимит времени на команду (default 300s).
"""

from __future__ import annotations

import os
import subprocess
from typing import Any

from core.base_tool import BaseTool, ToolAction, ToolStatus


class ContainerTool(BaseTool):
    name = "container"
    version = "1.0.0"
    description = (
        "Shell ВНУТРИ docker-контейнера задачи: exec (выполнить команду и "
        "получить вывод), read_file (прочитать файл), write_file (создать/"
        "перезаписать файл). Используй для установки пакетов, запуска "
        "скриптов, chmod, git-операций и любых действий в окружении задачи."
    )
    requires_context = True

    def __init__(self, config: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, context=context)
        self._timeout = int((config or {}).get("exec_timeout_sec", 300))

    # ------------------------------------------------------------------
    def _check(self) -> tuple[ToolStatus, str]:
        name = os.environ.get("LOCALIS_TB_CONTAINER", "").strip()
        if not name:
            return (ToolStatus.UNAVAILABLE,
                    "Переменная LOCALIS_TB_CONTAINER не задана: "
                    "режим контейнера активен только в TB-прогонах")
        r = subprocess.run(
            ["docker", "exec", name, "echo", "ok"],
            capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return (ToolStatus.UNAVAILABLE,
                    f"Контейнер {name} не отвечает: {r.stderr[:120]}")
        return (ToolStatus.READY, f"Контейнер {name} доступен")

    def actions(self) -> list[ToolAction]:
        return [
            ToolAction(
                "exec",
                "Выполнить shell-команду внутри контейнера задачи. Возвращает "
                "stdout+stderr и код возврата. Примеры: 'pip install pandas', "
                "'chmod +x process_data.sh', 'python solve.py', 'git log --oneline'.",
                {"command": {"type": "string",
                             "description": "Команда для shell контейнера"},
                 "timeout_sec": {"type": "integer",
                                 "description": "Лимит сек (default 300)"},
                 },
            ),
            ToolAction(
                "read_file",
                "Прочитать текстовый файл внутри контейнера и вернуть содержимое.",
                {"path": {"type": "string",
                          "description": "Абсолютный путь внутри контейнера"}},
            ),
            ToolAction(
                "write_file",
                "Создать/перезаписать текстовый файл внутри контейнера.",
                {"path": {"type": "string",
                          "description": "Абсолютный путь внутри контейнера"},
                 "content": {"type": "string",
                             "description": "Полное содержимое файла"}},
            ),
        ]

    # ------------------------------------------------------------------
    def _exec(self, command: str, timeout: int) -> tuple[int, str]:
        name = os.environ["LOCALIS_TB_CONTAINER"].strip()
        r = subprocess.run(
            ["docker", "exec", name, "sh", "-c", command],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")
        return r.returncode, out.strip() or "(пустой вывод)"

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if "LOCALIS_TB_CONTAINER" not in os.environ:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Инструмент недоступен вне TB-прогона"}

        if action == "exec":
            command = str(args.get("command") or "").strip()
            if not command:
                return {"ok": False, "data": {}, "summary": "",
                        "error": "Пустая команда"}
            timeout = min(int(args.get("timeout_sec") or self._timeout), 900)
            try:
                rc, out = self._exec(command, timeout)
            except subprocess.TimeoutExpired:
                return {"ok": False, "data": {}, "summary": "",
                        "error": f"Таймаут {timeout}с: команда не завершилась"}
            summary = out[-300:]
            return {"ok": rc == 0,
                    "data": {"exit_code": rc, "output": out},
                    "summary": f"rc={rc}: {summary}",
                    "error": None if rc == 0 else f"exit code {rc}"}

        if action == "read_file":
            path = str(args.get("path") or "").strip()
            rc, out = self._exec(f"cat {path!r}", 30)
            ok = rc == 0
            return {"ok": ok,
                    "data": {"content": out} if ok else {},
                    "summary": out[:300] if ok else out[-200:],
                    "error": None if ok else out[-200:]}

        if action == "write_file":
            path = str(args.get("path") or "").strip()
            content = str(args.get("content") or "")
            if not path:
                return {"ok": False, "data": {}, "summary": "",
                        "error": "Не указан путь"}
            # base64 — безопасная передача любого контента через shell
            import base64
            b64 = base64.b64encode(content.encode()).decode()
            rc, out = self._exec(
                f"mkdir -p $(dirname {path!r}) && echo {b64} | base64 -d > {path!r}", 30)
            return {"ok": rc == 0, "data": {"path": path},
                    "summary": f"Записано {len(content)} символов в {path}",
                    "error": None if rc == 0 else out[-200:]}

        return {"ok": False, "data": {}, "summary": "",
                "error": f"Неизвестное действие: {action}"}
