"""
QGIS-плагин платформы — первый пример инструмента.

Почему через subprocess, а не прямой `import qgis`:
PyQGIS собран под интерпретатор, поставляемый вместе с QGIS (свои DLL, Qt,
GDAL, PYTHONPATH). Импортировать его в произвольный venv на практике не
получается. Поэтому ядро запускает worker.py питоном самой QGIS и общается
с ним по JSON. Это же даёт изоляцию: падение QGIS не роняет платформу.

Инструмент домен-агностичному ядру не известен — он подхватывается
Tool Registry через manifest.yaml.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from core.base_tool import BaseTool, ToolAction
from core.schemas import ToolStatus

MARKER = "__AGENT_PLATFORM_RESULT__"

# Где искать установленный QGIS, если путь не задан в конфиге.
_SEARCH_ROOTS = [
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\OSGeo4W",
    r"C:\OSGeo4W64",
]
# Порядок важен: LTR-скрипт у стабильной ветки, обычный — у 4.x.
_LAUNCHER_NAMES = ["python-qgis-ltr.bat", "python-qgis.bat"]


def _find_qgis_launcher() -> Path | None:
    """Найти bat-скрипт, запускающий питон с окружением QGIS."""
    candidates: list[Path] = []
    for root in _SEARCH_ROOTS:
        root_path = Path(root)
        if not root_path.is_dir():
            continue
        # C:\Program Files\QGIS 3.44.12\bin\python-qgis-ltr.bat
        for entry in root_path.glob("QGIS*"):
            for name in _LAUNCHER_NAMES:
                launcher = entry / "bin" / name
                if launcher.is_file():
                    candidates.append(launcher)
        for name in _LAUNCHER_NAMES:
            launcher = root_path / "bin" / name
            if launcher.is_file():
                candidates.append(launcher)
    if not candidates:
        return None
    # Предпочитаем LTR — он стабильнее для камеральной работы.
    candidates.sort(key=lambda p: (0 if "ltr" in p.name else 1, str(p)))
    return candidates[0]


class QgisTool(BaseTool):
    """Чтение/запись слоёв и атрибутов QGIS, экспорт в GeoJSON."""

    name = "qgis"
    version = "1.1.0"
    description = (
        "Работа с геоданными через QGIS: информация о слое, чтение и запись "
        "атрибутов, список слоёв, экспорт в GeoJSON."
    )

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config)
        self.worker = Path(__file__).resolve().parent / "worker.py"
        configured = self.config.get("launcher")
        self.launcher: Path | None = Path(configured) if configured else _find_qgis_launcher()
        self.timeout = int(self.config.get("timeout_sec", 180))

    # ---- контракт BaseTool ----------------------------------------------

    def health(self) -> tuple[ToolStatus, str]:
        if sys.platform != "win32" and self.launcher is None:
            return ToolStatus.UNAVAILABLE, "QGIS не найден (не-Windows окружение)"
        if self.launcher is None:
            return ToolStatus.UNAVAILABLE, (
                "Не найден запускающий скрипт QGIS (python-qgis-ltr.bat). "
                "Укажите путь в tools/qgis/manifest.yaml → config.launcher"
            )
        if not self.worker.is_file():
            return ToolStatus.ERROR, f"Не найден worker.py: {self.worker}"
        return ToolStatus.READY, f"QGIS найден: {self.launcher}"

    def actions(self) -> list[ToolAction]:
        path_prop = {"type": "string", "description": "Путь к файлу слоя (.shp/.gpkg/.geojson)"}
        layer_prop = {"type": "string", "description": "Имя слоя внутри контейнера (для .gpkg)"}
        return [
            ToolAction(
                "list_layers",
                "Показать слои в GeoPackage или геофайлы в папке.",
                {"type": "object",
                 "properties": {"path": {"type": "string", "description": "Путь к папке или .gpkg"}},
                 "required": ["path"]},
            ),
            ToolAction(
                "layer_info",
                "Метаданные слоя: число объектов, система координат, охват, список полей.",
                {"type": "object",
                 "properties": {"path": path_prop, "layer_name": layer_prop},
                 "required": ["path"]},
            ),
            ToolAction(
                "read_attributes",
                "Прочитать атрибутивную таблицу слоя (с фильтром и ограничением строк).",
                {"type": "object",
                 "properties": {
                     "path": path_prop,
                     "layer_name": layer_prop,
                     "limit": {"type": "integer", "description": "Максимум строк (по умолчанию 50)"},
                     "columns": {"type": "array", "items": {"type": "string"},
                                 "description": "Какие поля вернуть; пусто — все"},
                     "filter": {"type": "string",
                                "description": "Выражение QGIS, например \"TYPE = 'гранит'\""},
                 },
                 "required": ["path"]},
            ),
            ToolAction(
                "export_geojson",
                "Экспортировать слой в GeoJSON, при необходимости с перепроекцией.",
                {"type": "object",
                 "properties": {
                     "path": path_prop,
                     "layer_name": layer_prop,
                     "output_path": {"type": "string", "description": "Куда сохранить .geojson"},
                     "target_crs": {"type": "string", "description": "Целевая CRS, например EPSG:4326"},
                 },
                 "required": ["path", "output_path"]},
            ),
            ToolAction(
                "update_attributes",
                "Записать значения атрибутов в существующие объекты слоя.",
                {"type": "object",
                 "properties": {
                     "path": path_prop,
                     "layer_name": layer_prop,
                     "updates": {
                         "type": "array",
                         "description": "Список изменений",
                         "items": {
                             "type": "object",
                             "properties": {
                                 "fid": {"type": "integer", "description": "Идентификатор объекта"},
                                 "values": {"type": "object", "description": "Поле → новое значение"},
                             },
                             "required": ["fid", "values"],
                         },
                     },
                 },
                 "required": ["path", "updates"]},
            ),
            ToolAction(
                "list_algorithms",
                "Показать список доступных алгоритмов обработки QGIS (буфер, "
                "пересечение, обрезка, зональная статистика и другие). "
                "Вызови ЭТО перед run_processing — имена алгоритмов не угадывай.",
                {"type": "object",
                 "properties": {"query": {"type": "string",
                                          "description": "Фильтр, например «буфер» или «статистика»"}}},
            ),
            ToolAction(
                "run_processing",
                "Запустить алгоритм обработки QGIS: буферы, пересечения, обрезка "
                "по границе, перепроецирование, зональная статистика, вычисление "
                "полей. Имя алгоритма и параметры возьми из list_algorithms.",
                {"type": "object",
                 "properties": {
                     "algorithm": {"type": "string",
                                   "description": "Идентификатор, например native:buffer"},
                     "parameters": {"type": "object",
                                    "description": ("Параметры алгоритма. Обычно INPUT — путь к "
                                                    "слою, OUTPUT — путь результата. Например "
                                                    '{"INPUT": "C:/d/a.shp", "DISTANCE": 100, '
                                                    '"OUTPUT": "C:/d/buffer.gpkg"}')},
                 },
                 "required": ["algorithm", "parameters"]},
            ),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if self.launcher is None:
            return {"ok": False, "error": "QGIS не найден на этой машине", "data": {}, "summary": ""}
        return self._run_worker({"action": action, "args": self._normalize_args(args)})

    # ---- санитария аргументов от LLM --------------------------------

    @staticmethod
    def _fix_path(value: str) -> str:
        """
        Починить путь, искажённый моделью. На практике малые модели
        (например 3B) теряют букву диска и URL-кодируют пробелы:
        '\\path\\to\\sample%20test.shp'. Проверено тестом.
        """
        path = value.strip().strip('"').strip("'")
        if path.lower().startswith("file:///"):
            path = path[8:]
        if "%" in path:
            path = unquote(path)
        path = path.replace("/", "\\") if sys.platform == "win32" else path
        # восстановить букву диска для путей вида "\Users\..."
        if sys.platform == "win32" and re.match(r"^\\Users\\", path, re.IGNORECASE):
            candidate = "C:" + path
            if Path(candidate).exists():
                path = candidate
        return path

    def _normalize_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """Привести аргументы от модели к виду, понятному QGIS."""
        clean = dict(args)
        for key in ("path", "output_path"):
            if isinstance(clean.get(key), str):
                clean[key] = self._fix_path(clean[key])
        # layer_name для shapefile не нужен — модели часто добавляют его зря
        # и ломают открытие слоя через '|layername='.
        source = str(clean.get("path", "")).lower()
        if source.endswith((".shp", ".geojson", ".json", ".kml")) and clean.get("layer_name"):
            clean.pop("layer_name")
        if isinstance(clean.get("limit"), str) and clean["limit"].isdigit():
            clean["limit"] = int(clean["limit"])
        # run_processing: пути лежат ВНУТРИ parameters, их тоже надо чинить —
        # иначе искажённый моделью путь дойдёт до алгоритма как есть
        params = clean.get("parameters")
        if isinstance(params, dict):
            fixed: dict[str, Any] = {}
            for key, value in params.items():
                if isinstance(value, str) and self._looks_like_path(value):
                    fixed[key] = self._fix_path(value)
                else:
                    fixed[key] = value
            clean["parameters"] = fixed
        return clean

    @staticmethod
    def _looks_like_path(value: str) -> bool:
        """
        Похоже ли значение на путь к файлу.

        Проверяем расширение или наличие разделителей: параметры алгоритмов
        бывают и числами, и выражениями ("id > 5"), их портить нельзя.
        """
        lowered = value.lower().strip()
        if lowered.endswith((".shp", ".gpkg", ".geojson", ".json", ".kml", ".tif",
                             ".tiff", ".csv", ".gml")):
            return True
        return bool(re.match(r"^[A-Za-z]:[\/]", lowered)) or lowered.startswith("file:///")

    # ---- запуск worker'а -------------------------------------------------

    def _build_command(self) -> tuple[str | list[str], bool]:
        """
        Собрать команду запуска. Возвращает (команда, shell).

        Нюанс Windows, проверенный на практике: .bat-файл нельзя запустить
        через subprocess со списком аргументов — Python повторно экранирует
        строку для cmd, и путь с пробелом ("C:\\Program Files\\QGIS ...")
        разваливается на части. Рабочий вариант — одна строка команды с
        shell=True, где кавычки расставлены вручную.
        """
        launcher = str(self.launcher)
        worker = str(self.worker)
        if sys.platform == "win32":
            return f'"{launcher}" "{worker}"', True
        return [launcher, worker], False

    def _run_worker(self, payload: dict[str, Any]) -> dict[str, Any]:
        command, use_shell = self._build_command()
        try:
            proc = subprocess.run(
                command,
                input=json.dumps(payload, ensure_ascii=False),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout,
                shell=use_shell,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"QGIS не ответил за {self.timeout} с (действие прервано)"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Не удалось запустить QGIS: {type(exc).__name__}: {exc}"}

        parsed = self._extract_result(proc.stdout)
        if parsed is not None:
            return parsed
        tail = (proc.stderr or proc.stdout or "").strip()[-800:]
        return {"ok": False, "data": {}, "summary": "",
                "error": f"QGIS вернул неожиданный ответ (код {proc.returncode}). {tail}"}

    @staticmethod
    def _extract_result(stdout: str) -> dict[str, Any] | None:
        """Достать JSON между маркерами — QGIS печатает в stdout свой шум."""
        if not stdout or MARKER not in stdout:
            return None
        try:
            chunk = stdout.split(MARKER)[1]
            return json.loads(chunk)
        except (IndexError, json.JSONDecodeError):
            return None
