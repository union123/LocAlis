"""
Инструмент сборки: npm install / npx tsc --noEmit / ng build в папке проекта.

B6 (24.08): до этого платформа не могла гарантировать компилируемость
сгенерированного кода. Теперь проверка компиляции — обычный tool call:
ошибки компилятора возвращаются модели текстом, и она их исправляет.

Безопасность: выполняются ТОЛЬКО три фиксированные команды из белого списка,
внутри разрешённого корня задачи (get_allowed_roots) — произвольный shell
не даём ни при каких аргументах.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from core.base_tool import BaseTool, ToolAction
from core.fs_paths import normalize as _normalize_path
from core.schemas import ToolStatus


class BuildTool(BaseTool):
    name = "build"
    version = "1.0.0"
    description = "Собрать проект и проверить типы: npm install, tsc --noEmit, ng build"

    requires_context = True  # нужен get_allowed_roots для проверки корня

    # Белый список: имя действия -> команда (аргументы после npm/npx фиксированы)
    _COMMANDS = {
        "npm_install": ["npm", "install"],
        "tsc_check": ["npx", "tsc", "--noEmit"],
        "ng_build": ["ng", "build"],
    }

    def health(self) -> tuple[ToolStatus, str]:
        import shutil
        ok = shutil.which("npm") and shutil.which("node")
        if ok:
            return ToolStatus.READY, "Node.js и npm доступны в PATH"
        return (ToolStatus.UNAVAILABLE,
                "npm/node не найдены в PATH — установи Node.js >= 22")

    def actions(self) -> list[ToolAction]:
        where = " Папка должна быть внутри разрешённой для задачи."
        return [
            ToolAction("npm_install",
                       "Установить зависимости проекта (npm install)." + where,
                       {"type": "object", "properties": {
                           "project_path": {"type": "string"}},
                           "required": ["project_path"]}),
            ToolAction("tsc_check",
                       "Проверить типы TypeScript без эмиссии (npx tsc --noEmit).",
                       {"type": "object", "properties": {
                           "project_path": {"type": "string"},
                           "tsconfig": {"type": "string",
                                        "description": "Имя tsconfig, напр. tsconfig.app.json"}},
                           "required": ["project_path"]}),
            ToolAction("ng_build",
                       "Собрать Angular-проект (ng build).",
                       {"type": "object", "properties": {
                           "project_path": {"type": "string"}},
                           "required": ["project_path"]}),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action not in self._COMMANDS:
            return {"ok": False, "error": f"Неизвестное действие: {action}",
                    "data": {}, "summary": ""}

        raw = args.get("project_path")
        if not raw or not str(raw).strip():
            return {"ok": False, "error": "Не указан project_path",
                    "data": {}, "summary": ""}
        project = Path(_normalize_path(str(raw)))
        if not project.is_absolute():
            return {"ok": False,
                    "error": (f"project_path='{raw}' относительный — укажи полный "
                              f"путь начиная с буквы диска."),
                    "data": {}, "summary": ""}
        if not (project / "package.json").is_file():
            return {"ok": False,
                    "error": f"В {project} нет package.json — это не корень Node-проекта.",
                    "data": {}, "summary": ""}

        # Корень задачи: если задан — сборка разрешена только внутри.
        get_roots = self.capability("get_allowed_roots")
        roots = (get_roots() if callable(get_roots) else None) or []
        if roots:
            allowed = False
            for root in roots:
                try:
                    project.resolve().relative_to(Path(root).expanduser().resolve())
                    allowed = True
                    break
                except (ValueError, OSError):
                    continue
            if not allowed:
                return {"ok": False,
                        "error": (f"project_path вне разрешённых папок задачи: {roots}"),
                        "data": {}, "summary": ""}

        argv = list(self._COMMANDS[action])
        extra_tsconfig = args.get("tsconfig")
        if action == "tsc_check" and extra_tsconfig:
            argv += ["-p", str(extra_tsconfig)]

        timeout_sec = int(self.config.get("timeout_sec", 600))
        max_chars = int(self.config.get("max_output_chars", 8000))

        try:
            proc = subprocess.run(
                argv, cwd=str(project), capture_output=True,
                text=True, encoding="utf-8", errors="replace",
                timeout=timeout_sec, shell=False)
        except subprocess.TimeoutExpired:
            return {"ok": False,
                    "error": f"Команда не завершилась за {timeout_sec} с — прервана.",
                    "data": {}, "summary": ""}
        except FileNotFoundError as exc:
            return {"ok": False, "error": f"Исполняемый файл не найден: {exc}",
                    "data": {}, "summary": ""}

        output = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
        if len(output) > max_chars:
            output = "(...начало обрезано...)\n" + output[-max_chars:]

        ok = proc.returncode == 0
        summary = (f"{action}: OK (код 0)" if ok
                   else f"{action}: ошибка, код {proc.returncode}")
        return {"ok": ok, "data": {"returncode": proc.returncode,
                                   "output_tail": output[-max_chars:]},
                "summary": summary + ("\nВывод:\n" + output if not ok else ""),
                "error": None if ok else output[-2000:]}
