"""
Инструмент работы с файлами.

Создан по следам реальных задач от 03.08.2026: пользователь просил описать
содержимое .txt-файла, Router верно ставил needs_tools=true, но единственным
инструментом был QGIS. Модели честно пытались открыть текстовый файл как
геослой и получали «QGIS не смог открыть слой», после чего сообщали
«файл отсутствует» — то есть выдумывали вывод из технической ошибки.

Дизайн-решения (из опыта отладки QGIS-плагина):
  - агрессивная нормализация путей: малые модели присылают URL-кодированные
    пути, кавычки, потерянную букву диска;
  - при ошибке кодировки не падаем, а перебираем кодировки (Windows-1251
    встречается в русских текстах постоянно);
  - объём ответа ограничен: 65 КБ текста не влезут в контекст 8192 токенов,
    поэтому возвращаем фрагмент и честно пишем, что он усечён.
"""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from core.base_tool import BaseTool, ToolAction
from core.fs_paths import describe_known_folders, known_folders
from core.fs_paths import normalize as _normalize_path
from core.schemas import ToolStatus

# Кодировки в порядке вероятности для русскоязычных файлов на Windows
_ENCODINGS = ["utf-8-sig", "utf-8", "cp1251", "cp866", "latin-1"]


def _read_text(path: Path) -> tuple[str, str]:
    """Прочитать файл, подобрав кодировку. Возвращает (текст, имя кодировки)."""
    data = path.read_bytes()
    for encoding in _ENCODINGS:
        try:
            return data.decode(encoding), encoding
        except (UnicodeDecodeError, LookupError):
            continue
    # Последняя линия обороны: не падаем, помечаем битые символы
    return data.decode("utf-8", errors="replace"), "utf-8 (с потерями)"


class FilesTool(BaseTool):
    """Чтение файлов и просмотр папок."""

    name = "files"
    version = "1.1.0"
    description = (
        "Прочитать текстовый файл, посмотреть список файлов в папке, "
        "найти текст в файлах, узнать размер и дату файла."
    )

    # ---- готовность ------------------------------------------------------

    def health(self) -> tuple[ToolStatus, str]:
        roots = self.config.get("allowed_roots") or []
        if roots:
            missing = [r for r in roots if not Path(r).exists()]
            if missing:
                return ToolStatus.ERROR, f"Указанные папки не существуют: {missing}"
            return ToolStatus.READY, f"Доступ ограничен папками: {len(roots)}"
        return ToolStatus.READY, "Доступ к файловой системе разрешён"

    # ---- проверки доступа -------------------------------------------------

    def _check_access(self, path: Path) -> str | None:
        """Причина отказа или None, если доступ разрешён."""
        roots = self.config.get("allowed_roots") or []
        if roots:
            allowed = False
            for root in roots:
                try:
                    path.resolve().relative_to(Path(root).resolve())
                    allowed = True
                    break
                except (ValueError, OSError):
                    continue
            if not allowed:
                return (f"Доступ к этому пути запрещён настройками. "
                        f"Разрешены только: {roots}")
        blocked = [str(e).lower() for e in (self.config.get("blocked_extensions") or [])]
        if path.suffix.lower() in blocked:
            return (f"Файлы с расширением {path.suffix} не читаются "
                    "(бинарный или архивный формат)")
        return None

    def _resolve(self, args: dict[str, Any], key: str = "path") -> tuple[Path | None, str]:
        """Получить существующий путь из аргументов или текст ошибки."""
        raw = _normalize_path(args.get(key))
        if not raw:
            return None, f"Не указан параметр '{key}'"
        path = Path(raw)
        # TB-режим: /app/x -> корень песочницы (чтение входных файлов задачи)
        if not path.exists():
            tb_root = os.environ.get("LOCALIS_TB_WORKDIR", "").strip()
            if tb_root:
                alt = str(path).lstrip("\\").replace("\\", "/").lstrip("/")
                if alt.startswith("app/"):
                    alt = alt[4:]
                if alt and not alt.startswith(".."):
                    cand = Path(tb_root) / alt
                    if cand.exists():
                        return cand, ""
        if not path.exists():
            # Ошибка «не найдено» бесполезна для модели: она либо выдумает
            # содержимое, либо повторит тот же путь. Поэтому сразу ищем файл
            # сами и возвращаем ГОТОВЫЕ полные пути (жалоба 05.08).
            hint = ""
            parent = path.parent
            if parent.is_dir():
                stem = path.stem.lower()[:6]
                similar = [str(item) for item in parent.iterdir()
                           if stem and stem in item.name.lower()][:5]
                if similar:
                    hint = (" Возможно, вы имели в виду один из этих файлов "
                            "(передайте полный путь): " + "; ".join(similar))
                else:
                    # Частый случай: файл лежит в подпапке — ищем вглубь,
                    # прежде чем сообщать «файла нет»
                    nested: list[dict[str, Any]] = []
                    try:
                        self._walk(parent, path.name.lower(), None, nested, 5, max_depth=3)
                    except Exception:  # noqa: BLE001
                        nested = []
                    if nested:
                        hint = (" Файл лежит во вложенной папке — используйте полный путь: "
                                + "; ".join(item["path"] for item in nested))
                    else:
                        hint = (f" Папка существует, но файла в ней нет "
                                f"({len(list(parent.iterdir()))} объектов). "
                                "Вызовите list_dir по этой папке или find_file по имени.")
            else:
                found: list[dict[str, Any]] = []
                self._walk_known(path.name, found, limit=5)
                if found:
                    hint = (" Файл с таким именем найден в другом месте — "
                            "используйте полный путь: "
                            + "; ".join(item["path"] for item in found))
                else:
                    hint = (" Такой папки нет. Реальные папки этого компьютера: "
                            + describe_known_folders()
                            + ". Или вызовите find_file по имени файла.")
            return None, f"Путь не найден: {path}.{hint}"
        denied = self._check_access(path)
        if denied:
            return None, denied
        return path, ""

    def _walk_known(self, name: str, hits: list[dict[str, Any]], limit: int = 5) -> None:
        """Поиск файла по имени во всех обычных папках — для подсказок в ошибках."""
        needle = name.lower()
        if not needle:
            return
        folders = known_folders()
        for key in ("desktop", "documents", "downloads", "onedrive", "home"):
            root = folders.get(key)
            if root is None or len(hits) >= limit:
                continue
            try:
                self._walk(root, needle, None, hits, limit, max_depth=3)
            except Exception:  # noqa: BLE001 — подсказка не должна ломать основной ответ
                return

    # ---- описание действий ------------------------------------------------

    def actions(self) -> list[ToolAction]:
        # В описании параметра перечисляем РЕАЛЬНЫЕ папки этого компьютера.
        # Без этого модель подставляет стандартный C:/Users/<имя>/Desktop,
        # которого здесь нет (рабочий стол перенаправлен в OneDrive).
        path_param = {"type": "string",
                      "description": ("Путь к файлу или папке. Можно писать имя известной папки: "
                                      "'Рабочий стол/файл.txt', '~/Downloads/файл.txt'. "
                                      "Реальные папки: " + describe_known_folders())}
        return [
            ToolAction(
                "known_folders",
                "Показать реальные пути известных папок этого компьютера "
                "(рабочий стол, документы, загрузки). Вызови ЭТО ПЕРВЫМ, если "
                "точный путь к файлу неизвестен — не угадывай путь сам.",
                {"type": "object", "properties": {}},
            ),
            ToolAction(
                "find_file",
                "Найти файл или папку по части имени во всех обычных местах "
                "(рабочий стол, документы, загрузки, профиль, OneDrive). "
                "Используй, когда путь неизвестен или чтение вернуло 'путь не найден'.",
                {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string",
                                 "description": "Часть имени файла, например 'введение' или '*.shp'"},
                        "search_root": {"type": "string",
                                        "description": "Необязательно: где искать. По умолчанию — все обычные папки"},
                        "max_results": {"type": "integer", "description": "Сколько совпадений вернуть (по умолчанию 20)"},
                    },
                    "required": ["name"],
                },
            ),
            ToolAction(
                "read_text",
                "Прочитать содержимое файла: .txt, .md, .csv, .json, .py, а также .pdf, "
                ".docx, .xlsx. "
                "Используй это для любых вопросов о содержании файла.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_param,
                        "max_chars": {"type": "integer",
                                      "description": "Сколько символов вернуть (по умолчанию 20000)"},
                        "offset": {"type": "integer",
                                   "description": "С какого символа начать — для чтения продолжения"},
                    },
                    "required": ["path"],
                },
            ),
            ToolAction(
                "list_dir",
                "Показать список файлов и папок внутри указанной папки с размерами.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_param,
                        "pattern": {"type": "string",
                                    "description": "Фильтр по маске, например *.txt"},
                    },
                    "required": ["path"],
                },
            ),
            ToolAction(
                "file_info",
                "Сведения о файле: размер, дата изменения, тип, читаем ли как текст.",
                {"type": "object", "properties": {"path": path_param}, "required": ["path"]},
            ),
            ToolAction(
                "search_in_files",
                "Найти текст внутри файлов папки и показать строки с совпадениями.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Папка для поиска"},
                        "query": {"type": "string", "description": "Что искать"},
                        "pattern": {"type": "string",
                                    "description": "В каких файлах искать, например *.txt"},
                    },
                    "required": ["path", "query"],
                },
            ),
            ToolAction(
                "summarize",
                "Посчитать распределение значений по колонкам в CSV/GeoJSON "
                "БЕЗ чтения всего файла в контекст. Вернёт компактную сводку: "
                "сколько записей, уникальные значения каждой колонки и их частоты.",
                {
                    "type": "object",
                    "properties": {
                        "path": path_param,
                        "columns": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Колонки/поля, по которым считать распределение",
                        },
                    },
                    "required": ["path", "columns"],
                },
            ),
        ]

    # ---- выполнение -------------------------------------------------------

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Неизвестное действие: {action}"}
        return handler(args)

    # ---- поиск расположения файлов ----------------------------------------

    # Папки, которые не нужно обходить: сотни тысяч файлов и никакого смысла
    _SKIP_DIRS = {"appdata", "node_modules", ".git", ".venv", "__pycache__",
                  "windows", "program files", "program files (x86)",
                  "$recycle.bin", "programdata", "onedrivetemp"}

    def _do_known_folders(self, args: dict[str, Any]) -> dict[str, Any]:
        folders = {k: str(v) for k, v in known_folders().items()}
        listing = "; ".join(f"{k}: {v}" for k, v in folders.items())
        return {"ok": True, "data": {"folders": folders},
                "summary": ("Реальные папки этого компьютера — " + listing +
                            ". Используй именно эти пути, не подставляй стандартные.")}

    def _walk(self, root: Path, needle: str, pattern: str | None,
              hits: list[dict[str, Any]], limit: int, max_depth: int = 4) -> None:
        """Обход в ширину с ограничением глубины: быстрее и не уходит в системные дебри."""
        import fnmatch
        queue: list[tuple[Path, int]] = [(root, 0)]
        while queue and len(hits) < limit:
            current, depth = queue.pop(0)
            try:
                entries = list(current.iterdir())
            except (OSError, PermissionError):
                continue
            for entry in entries:
                if len(hits) >= limit:
                    return
                name = entry.name
                if pattern:
                    matched = fnmatch.fnmatch(name.lower(), pattern)
                else:
                    matched = needle in name.lower()
                if matched:
                    resolved = str(entry)
                    # Дедупликация: Рабочий стол вложен в OneDrive, а OneDrive —
                    # в профиль, поэтому без проверки один файл выдаётся трижды
                    if not any(h["path"] == resolved for h in hits):
                        try:
                            size = entry.stat().st_size if entry.is_file() else 0
                        except OSError:
                            size = 0
                        hits.append({"path": resolved, "name": name,
                                     "is_dir": entry.is_dir(), "size": size})
                if entry.is_dir() and depth < max_depth and name.lower() not in self._SKIP_DIRS:
                    queue.append((entry, depth + 1))

    def _do_find_file(self, args: dict[str, Any]) -> dict[str, Any]:
        raw = str(args.get("name") or "").strip().strip("\"'`")
        if not raw:
            return {"ok": False, "data": {}, "summary": "", "error": "Не указано имя файла для поиска"}
        limit = int(args.get("max_results") or 20)
        pattern = raw.lower() if ("*" in raw or "?" in raw) else None
        needle = raw.lower()

        # Если модель передала целый путь — ищем по имени файла из него
        if not pattern and (os.sep in raw or "/" in raw):
            needle = Path(_normalize_path(raw)).name.lower()

        roots: list[Path] = []
        requested = args.get("search_root")
        if requested:
            candidate = Path(_normalize_path(requested))
            if not candidate.is_dir():
                return {"ok": False, "data": {}, "summary": "",
                        "error": f"Папка для поиска не найдена: {candidate}"}
            roots = [candidate]
        else:
            folders = known_folders()
            order = ["desktop", "documents", "downloads", "onedrive", "home"]
            for key in order:
                path = folders.get(key)
                if path and path not in roots:
                    roots.append(path)

        hits: list[dict[str, Any]] = []
        for root in roots:
            if self._check_access(root):
                continue
            self._walk(root, needle, pattern, hits, limit)
            if len(hits) >= limit:
                break

        if not hits:
            return {"ok": True, "data": {"hits": [], "searched": [str(r) for r in roots]},
                    "summary": (f"Файлы с «{raw}» не найдены. Просмотрены папки: "
                                + ", ".join(str(r) for r in roots)
                                + ". Уточни имя у пользователя или вызови list_dir по нужной папке.")}
        listing = "; ".join(("папка " if h["is_dir"] else "") + h["path"] for h in hits[:10])
        return {"ok": True, "data": {"hits": hits, "searched": [str(r) for r in roots]},
                "summary": (f"Найдено совпадений: {len(hits)}. Полные пути: {listing}. "
                            "Передавай в read_text именно полный путь из этого списка.")}

    def _do_read_text(self, args: dict[str, Any]) -> dict[str, Any]:
        path, error = self._resolve(args)
        if path is None:
            return {"ok": False, "data": {}, "summary": "", "error": error}
        if path.is_dir():
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Это папка, а не файл. Используйте list_dir для {path}"}

        limit = int(args.get("max_chars") or self.config.get("max_chars", 20000))
        offset = max(0, int(args.get("offset") or 0))

        suffix = path.suffix.lower()

        # Принудительная агрегация большого структурированного файла.
        # gpt-oss упорно читает read_text по кускам и игнорирует files__summarize,
        # хотя тот есть в схеме. Поэтому для больших таблиц сразу отдаём сводку.
        if offset == 0 and args.get("max_chars") is None and suffix in (
                ".csv", ".tsv", ".geojson", ".json", ".gpkg"):
            try:
                if path.stat().st_size > max(20000, limit):
                    auto = self._do_summarize({"path": str(path),
                                               "columns": args.get("columns")})
                    if auto.get("ok"):
                        auto["summary"] = ("[АВТОПЕРЕХОД: файл большой и структурированный; "
                                           "read_text вернул бы лишь фрагмент. "
                                           "Это распределение всех значений:]\n" + auto["summary"])
                        return auto
            except OSError:
                pass

        try:
            if suffix == ".pdf":
                text, encoding = self._read_pdf(path), "pdf"
            elif suffix == ".docx":
                text, encoding = self._read_docx(path), "docx"
            elif suffix in (".xlsx", ".xlsm"):
                text, encoding = self._read_xlsx(path), "xlsx"
            else:
                text, encoding = _read_text(path)
        except ImportError as exc:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Для чтения {suffix} нужен пакет: {exc}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Не удалось прочитать файл: {type(exc).__name__}: {exc}"}

        total = len(text)
        chunk = text[offset:offset + limit]
        truncated = (offset + len(chunk)) < total
        lines = text.count("\n") + 1

        summary = (f"Файл {path.name}: {total} символов, {lines} строк, кодировка {encoding}. "
                   f"Содержимое{' (фрагмент)' if truncated or offset else ''}:\n{chunk}")
        if truncated:
            summary += (f"\n[...прочитано {offset + len(chunk)} из {total} символов. "
                        f"Для продолжения вызови read_text с offset={offset + len(chunk)}]")
        return {
            "ok": True,
            "data": {"path": str(path), "name": path.name, "text": chunk,
                     "total_chars": total, "lines": lines, "encoding": encoding,
                     "truncated": truncated, "next_offset": offset + len(chunk) if truncated else None},
            "summary": summary,
        }

    @staticmethod
    def _read_pdf(path: Path) -> str:
        """
        Текст из PDF. Требует pypdf.

        Помечаем страницы: у пользователя лежат конспекты лекций, и ссылка
        «на странице 4» полезнее сплошной простыни текста. Страницы без
        извлекаемого текста (сканы) отмечаем честно, а не пропускаем молча —
        иначе модель решит, что документ пустой.
        """
        try:
            from pypdf import PdfReader  # type: ignore
        except ImportError as exc:
            raise ImportError("pypdf") from exc
        reader = PdfReader(str(path))
        parts: list[str] = []
        empty = 0
        for number, page in enumerate(reader.pages, start=1):
            try:
                text = (page.extract_text() or "").strip()
            except Exception:  # noqa: BLE001 — битая страница не должна ронять чтение
                text = ""
            if text:
                parts.append(f"=== Страница {number} ===\n{text}")
            else:
                empty += 1
        if empty:
            parts.append(f"[страниц без извлекаемого текста: {empty} из "
                         f"{len(reader.pages)} — возможно, это сканы]")
        return "\n\n".join(parts)

    @staticmethod
    def _read_docx(path: Path) -> str:
        """Текст из .docx. Требует python-docx."""
        try:
            import docx  # type: ignore
        except ImportError as exc:
            raise ImportError("python-docx") from exc
        document = docx.Document(str(path))
        parts = [p.text for p in document.paragraphs if p.text.strip()]
        for table in document.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    parts.append(" | ".join(cells))
        return "\n".join(parts)

    @staticmethod
    def _read_xlsx(path: Path) -> str:
        """Значения ячеек из .xlsx/.xlsm. Требует openpyxl."""
        try:
            import openpyxl  # type: ignore
        except ImportError as exc:
            raise ImportError("openpyxl") from exc
        book = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
        parts: list[str] = []
        for sheet in book.worksheets:
            parts.append(f"=== Лист: {sheet.title} ===")
            for i, row in enumerate(sheet.iter_rows(values_only=True)):
                if i >= 200:
                    parts.append("[...остальные строки не показаны]")
                    break
                values = [str(v) for v in row if v is not None]
                if values:
                    parts.append(" | ".join(values))
        return "\n".join(parts)

    def _do_list_dir(self, args: dict[str, Any]) -> dict[str, Any]:
        path, error = self._resolve(args)
        if path is None:
            return {"ok": False, "data": {}, "summary": "", "error": error}
        if not path.is_dir():
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Это файл, а не папка. Используйте read_text для {path}"}

        pattern = str(args.get("pattern") or "*").strip() or "*"
        limit = int(self.config.get("max_entries", 200))
        try:
            entries = sorted(path.glob(pattern), key=lambda p: (p.is_file(), p.name.lower()))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Не удалось прочитать папку: {exc}"}

        items: list[dict[str, Any]] = []
        for entry in entries[:limit]:
            try:
                size = entry.stat().st_size if entry.is_file() else 0
            except OSError:
                size = 0
            items.append({"name": entry.name, "is_dir": entry.is_dir(), "size": size})

        dirs = [i["name"] for i in items if i["is_dir"]]
        files = [f"{i['name']} ({i['size']} б)" for i in items if not i["is_dir"]]
        summary = f"В папке {path.name}: папок {len(dirs)}, файлов {len(files)}."
        if dirs:
            summary += f" Папки: {', '.join(dirs[:15])}."
        if files:
            summary += f" Файлы: {', '.join(files[:25])}."
        if len(entries) > limit:
            summary += f" [показано {limit} из {len(entries)}]"
        return {"ok": True, "summary": summary,
                "data": {"path": str(path), "entries": items, "total": len(entries)}}

    def _do_file_info(self, args: dict[str, Any]) -> dict[str, Any]:
        path, error = self._resolve(args)
        if path is None:
            return {"ok": False, "data": {}, "summary": "", "error": error}
        from datetime import datetime
        stat = path.stat()
        modified = datetime.fromtimestamp(stat.st_mtime).strftime("%d.%m.%Y %H:%M")
        kind = "папка" if path.is_dir() else f"файл {path.suffix or 'без расширения'}"
        readable = path.suffix.lower() in (
            ".txt", ".md", ".csv", ".json", ".py", ".yaml", ".yml", ".log",
            ".xml", ".html", ".ini", ".cfg", ".sql", ".docx", ".xlsx", ".xlsm",
            ".pdf")
        return {
            "ok": True,
            "data": {"path": str(path), "name": path.name, "size": stat.st_size,
                     "modified": modified, "is_dir": path.is_dir(), "readable_as_text": readable},
            "summary": (f"{path.name}: {kind}, размер {stat.st_size} байт, "
                        f"изменён {modified}, "
                        f"{'читается как текст' if readable else 'как текст читать не стоит'}"),
        }

    def _do_search_in_files(self, args: dict[str, Any]) -> dict[str, Any]:
        path, error = self._resolve(args)
        if path is None:
            return {"ok": False, "data": {}, "summary": "", "error": error}
        query = str(args.get("query") or "").strip()
        if not query:
            return {"ok": False, "data": {}, "summary": "", "error": "Не указано, что искать"}
        if not path.is_dir():
            files = [path]
        else:
            pattern = str(args.get("pattern") or "*.txt").strip() or "*.txt"
            files = [f for f in sorted(path.glob(pattern)) if f.is_file()][:100]

        needle = query.lower()
        hits: list[dict[str, Any]] = []
        scanned = 0
        for file_path in files:
            if self._check_access(file_path):
                continue
            try:
                text, _ = _read_text(file_path)
            except Exception:  # noqa: BLE001 — нечитаемый файл просто пропускаем
                continue
            scanned += 1
            for number, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    hits.append({"file": file_path.name, "line": number,
                                 "text": line.strip()[:200]})
                    if len(hits) >= 40:
                        break
                        break
            if len(hits) >= 40:
                break

        if not hits:
            return {"ok": True, "data": {"hits": [], "scanned": scanned},
                    "summary": f"Совпадений с «{query}» не найдено (просмотрено файлов: {scanned})"}
        listing = "; ".join(f"{h['file']}:{h['line']} — {h['text'][:90]}" for h in hits[:12])
        return {"ok": True, "data": {"hits": hits, "scanned": scanned},
                "summary": f"Найдено совпадений: {len(hits)} в {scanned} файлах. {listing}"}

def _summarize_records(path: Path):
    """Распарсить CSV/JSON/GeoJSON в (список записей, тип)."""
    text, _ = _read_text(path)
    suffix = path.suffix.lower()
    if suffix in (".geojson", ".json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return [], "json"
        if isinstance(data, dict) and isinstance(data.get("features"), list):
            out = []
            for f in data["features"]:
                p = f.get("properties") or {}
                out.append({k: v for k, v in p.items() if v is not None})
            return out, "geojson"
        if isinstance(data, list):
            return [r for r in data if isinstance(r, dict)], "json"
        return [], "json"
    elif suffix in (".csv", ".tsv"):
        try:
            reader = csv.DictReader(path.open(encoding="utf-8-sig"))
            out = []
            for r in reader:
                d = {k: v for k, v in r.items() if v not in (None, "")}
                if d:
                    out.append(d)
            return out, "csv"
        except Exception:
            return [], "csv"
    return [], suffix or "txt"


class _SummarizeMixin:
    """Заглушка — настоящий обработчик добавляется в FilesTool ниже."""
    def _do_summarize(self, args):
        """Посчитать распределение значений по колонкам без чтения файла целиком."""
        path, error = self._resolve(args)
        if path is None:
            return {"ok": False, "data": {}, "summary": "", "error": error}
        if path.is_dir():
            return {"ok": False, "data": {}, "summary": "", "error": "Это папка, а не файл"}
        try:
            records, ftype = _summarize_records(path)
        except Exception as exc:
            return {"ok": False, "data": {}, "summary": "", "error": f"Чтение: {exc}"}
        if not records:
            return {"ok": True, "data": {"records": 0, "type": ftype},
                    "summary": f"Файл {path.name}: тип {ftype}, записей 0"}
        all_cols = list({c for r in records for c in r.keys()})
        columns = args.get("columns")
        target = [c for c in (columns or all_cols) if c in all_cols]
        summary = {"file": path.name, "type": ftype, "records": len(records),
                   "columns": all_cols}
        text_lines = [f"Файл {path.name}: {len(records)} записей, тип {ftype}"]
        for col in target:
            c = Counter()
            missing = 0
            for r in records:
                v = r.get(col)
                if v is None or v == "":
                    missing += 1
                    continue
                c[str(v)] += 1
            summary["dist_" + col] = {"unique": len(c), "missing": missing,
                                      "top": c.most_common(30)}
            top_str = "; ".join(str(k) + "=" + str(v) for k, v in c.most_common(15))
            text_lines.append(f"Колонка {col}: уникальных={len(c)}, пустых={missing}. Топ: {top_str[:900]}")
        return {"ok": True, "data": summary, "summary": chr(10).join(text_lines)}

# Привязываем метод _do_summarize к FilesTool (миксин объявлен ниже класса)
FilesTool._do_summarize = _SummarizeMixin._do_summarize
