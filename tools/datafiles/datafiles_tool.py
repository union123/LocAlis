"""Создание переносимых файлов данных: GeoJSON, CSV, GeoPackage, TXT/JSON."""
from __future__ import annotations

import csv
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from core.base_tool import BaseTool, ToolAction
from core.fs_paths import describe_known_folders
from core.fs_paths import normalize as _normalize_path
from core.schemas import ToolStatus


class DataFilesTool(BaseTool):
    name = "datafiles"
    version = "1.0.0"
    description = "Создать стандартный файл данных, который смогут читать files и QGIS"

    def health(self) -> tuple[ToolStatus, str]:
        return ToolStatus.READY, "Готов создавать TXT, JSON, CSV и GeoJSON; GeoPackage — при наличии GDAL"

    def _path(self, raw: Any) -> tuple[Path | None, str]:
        if not raw or not str(raw).strip():
            return None, "Не указан output_path"
        # Тот же нормализатор, что у files: 'Рабочий стол/отчёт.xlsx'
        # и ~/Desktop/... превращаются в реальный путь этого компьютера
        p = Path(_normalize_path(raw))
        # Задача 4ce3abd8 (21.08): модель передала голые имена файлов
        # ('init-db.ts', 'server.ts') без пути. normalize() их не трогает,
        # Path остаётся ОТНОСИТЕЛЬНЫМ, и запись молча уходит в текущую
        # рабочую папку процесса. Явно отклоняем такие пути (тест 21.08
        # это требовал, но сама проверка в код добавлена не была).
        if not p.is_absolute():
            # TB-режим (LOCALIS_TB_CONTAINER задан): песочница = /app.
            # Модель честно пишет '/app/hello.txt' как в задании — маппим
            # в корень рабочей папки (task 3ee372400ef2 vs a207f61be8d3:
            # отказ guard'а вёл к записи в подпапку app/ и /app/app/...).
            tb_root = os.environ.get("LOCALIS_TB_WORKDIR", "").strip()
            if tb_root:
                rel = str(p).lstrip("\\/").replace("\\", "/").lstrip("/")
                if rel.startswith("app/"):
                    rel = rel[4:]
                if rel and not rel.startswith(".."):
                    p = Path(tb_root) / rel
                else:
                    return None, (
                        f"output_path='{raw}' не удалось сопоставить с песочницей"
                    )
            else:
                return None, (
                    f"output_path='{raw}' после нормализации остался относительным "
                    f"путём ('{p}') — иначе он был бы молча записан в текущую "
                    f"рабочую папку процесса, а не в нужную папку задачи. "
                    f"Укажи полный путь начиная с буквы диска."
                )
        # Песочница записи. Приоритет: ограничение КОНКРЕТНОЙ задачи
        # (task.metadata['allowed_roots'] через GET_ALLOWED_ROOTS, см.
        # core/tool_context.py) важнее статического allowed_roots из
        # manifest.yaml — так разные задачи изолированы друг от друга.
        # Добавлено после задачи 931c894e5624 (23.08): модель проигнорировала
        # промпт-запрет и записала файлы в папку прошлой попытки; проверка
        # на уровне инструмента от неё не зависит.
        roots: list[str] = []
        get_roots = self.capability("get_allowed_roots")
        dynamic = get_roots() if callable(get_roots) else None
        if dynamic:
            roots = list(dynamic)
        else:
            roots = list(self.config.get("allowed_roots") or [])
        if roots:
            allowed = False
            for root in roots:
                try:
                    p.resolve().relative_to(Path(root).expanduser().resolve())
                    allowed = True
                    break
                except (ValueError, OSError):
                    continue
            if not allowed:
                return None, (
                    f"output_path='{raw}' вне разрешённых для этой задачи папок: "
                    f"{roots}. Пиши файлы задачи только внутри указанной "
                    f"папки проекта — проверь путь и повтори."
                )
        return p, ""

    def _prepare(self, path: Path, overwrite: bool) -> str | None:
        if path.exists() and not overwrite and not self.config.get("overwrite_default", False):
            return f"Файл уже существует: {path}. Передайте overwrite=true для замены."
        path.parent.mkdir(parents=True, exist_ok=True)
        return None

    def actions(self) -> list[ToolAction]:
        # Куда сохранять: перечисляем реальные папки, иначе модели пишут
        # в C:/Users/<имя>/Desktop, которого на этом компьютере нет
        where = (" Можно указать известную папку, например "
                 "'Рабочий стол/отчёт.xlsx'. Реальные папки: "
                 + describe_known_folders())
        return [
            ToolAction("write_text", "Создать UTF-8 текстовый файл.", {"type":"object", "properties": {
                "output_path":{"type":"string", "description":"Путь к создаваемому файлу." + where}, "content":{"type":"string"}, "overwrite":{"type":"boolean"}}, "required":["output_path","content"]}),
            ToolAction("write_excel", "Создать настоящий Excel XLSX с листами, заголовками и строками. Требует openpyxl.", {"type":"object", "properties": {
                "output_path":{"type":"string", "description":"Путь с расширением .xlsx" + where}, "sheets":{"type":"object", "description":"Имя листа -> массив объектов-строк"}, "overwrite":{"type":"boolean"}}, "required":["output_path","sheets"]}),
            ToolAction("write_word", "Создать настоящий Word DOCX с заголовком, абзацами, списками и таблицами. Требует python-docx.", {"type":"object", "properties": {
                "output_path":{"type":"string", "description":"Путь с расширением .docx" + where}, "title":{"type":"string"}, "paragraphs":{"type":"array", "items":{"type":"string"}}, "tables":{"type":"array", "items":{"type":"object", "properties":{"headers":{"type":"array","items":{"type":"string"}}, "rows":{"type":"array","items":{"type":"array"}}}}}, "overwrite":{"type":"boolean"}}, "required":["output_path"]}),
            ToolAction("write_json", "Создать валидный UTF-8 JSON-файл.", {"type":"object", "properties": {
                "output_path":{"type":"string", "description":"Путь к создаваемому файлу." + where}, "data":{"type":"object"}, "overwrite":{"type":"boolean"}}, "required":["output_path","data"]}),
            ToolAction("write_csv", "Создать CSV UTF-8 с заголовками, читаемый files и QGIS.", {"type":"object", "properties": {
                "output_path":{"type":"string", "description":"Путь с расширением .csv" + where}, "rows":{"type":"array", "items":{"type":"object"}}, "field_order":{"type":"array","items":{"type":"string"}}, "delimiter":{"type":"string"}, "overwrite":{"type":"boolean"}}, "required":["output_path","rows"]}),
            ToolAction("write_geojson", "Создать валидный GeoJSON FeatureCollection для QGIS. Координаты: [долгота, широта].", {"type":"object", "properties": {
                "output_path":{"type":"string", "description":"Путь с расширением .geojson" + where}, "features":{"type":"array", "items":{"type":"object"}, "description":"GeoJSON Feature objects"}, "crs":{"type":"string", "description":"Необязательно, например EPSG:4326"}, "overwrite":{"type":"boolean"}}, "required":["output_path","features"]}),
            ToolAction("write_geopackage", "Создать GeoPackage из GeoJSON-подобных features для открытия в QGIS. Требует Fiona или GDAL.", {"type":"object", "properties": {
                "output_path":{"type":"string", "description":"Путь с расширением .gpkg" + where}, "layer_name":{"type":"string"}, "features":{"type":"array","items":{"type":"object"}}, "crs":{"type":"string"}, "overwrite":{"type":"boolean"}}, "required":["output_path","layer_name","features"]}),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        path, err = self._path(args.get("output_path"))
        if not path:
            return {"ok": False, "error": err, "data": {}, "summary": ""}
        # Защита от уничтожения данных: расширение вывода должно соответствовать
        # действию. Модели (напр. в прогоне 34ce9951) вызывали write_excel с
        # output_path="...Test_for_AI.geojson" и overwrite=true — это затирало
        # ИСХОДНЫЙ GeoJSON Excel-ом. Жёсткая проверка это исключает.
        #
        # Ослаблено 23.08 (задача 931c894e5624): жёсткое требование ".txt для
        # write_text" делало невыполнимой любую задачу «напиши исходный код» —
        # .ts/.py/.md/.json/.yaml отвергались. Теперь:
        #   - ДАННЫЕ (csv/json/xlsx/docx/geojson/gpkg) — строго своё
        #     расширение: защита от затирания исходных данных остаётся;
        #   - ПРОИЗВОЛЬНЫЙ ТЕКСТ (write_text) — разрешён ЛЮБОЙ суффикс,
        #     КРОМЕ расширений данных: так код и конфиги пишутся свободно,
        #     но write_text по-прежнему не может подменить data-файл.
        _DATA_EXTS = {".csv", ".xlsx", ".docx", ".geojson", ".gpkg"}
        # .json исключён из data-запрета (фидбек f4af7315, 24.08): под запрет
        # попадал package.json/tsconfig.json — любой код-проект было невозможно
        # создать. Строгое .json-правило осталось только у write_json.
        expected_ext = {
            "write_csv": ".csv", "write_json": ".json",
            "write_excel": ".xlsx", "write_word": ".docx",
            "write_geojson": ".geojson", "write_geopackage": ".gpkg",
        }.get(action)
        suffix = path.suffix.lower()
        if action == "write_text":
            if suffix in _DATA_EXTS:
                return {"ok": False, "error": (
                    f"write_text нельзя писать файлы данных ({suffix}) — "
                    f"используй профильное действие или другое имя файла. "
                    f"Так защищаем исходные данные от перезаписи."),
                    "data": {}, "summary": ""}
        elif expected_ext and suffix != expected_ext:
            return {"ok": False, "error": (f"Для {action} нужен путь с расширением "
                                           f"{expected_ext}, а не '{path.suffix or '—'}'. "
                                           f"Не пиши результат в исходный файл данных."),
                    "data": {}, "summary": ""}
        # write_excel с существующим файлом и overwrite=false — это ДОБАВЛЕНИЕ
        # листов (сборка отчёта по частям), а не перезапись: пропускаем
        # проверку «файл уже существует».
        is_excel_append = (action == "write_excel" and path.exists()
                           and not args.get("overwrite", False))
        if not is_excel_append:
            prep = self._prepare(path, bool(args.get("overwrite", False)))
            if prep:
                return {"ok": False, "error": prep, "data": {}, "summary": ""}
        try:
            if action == "write_text":
                content = str(args.get("content", ""))
                if len(content) > int(self.config.get("max_content_chars", 1000000)):
                    return {"ok": False, "error": "Содержимое слишком большое", "data": {}, "summary": ""}
                self._atomic_text(path, content)
            elif action == "write_excel":
                self._excel(path, args.get("sheets") or {},
                            append=path.exists() and not args.get("overwrite", False))
            elif action == "write_word":
                self._word(path, args)
            elif action == "write_json":
                self._atomic_text(path, json.dumps(args["data"], ensure_ascii=False, indent=2) + "\n")
            elif action == "write_csv":
                self._csv(path, args.get("rows") or [], args.get("field_order") or [], str(args.get("delimiter") or ","))
            elif action == "write_geojson":
                self._geojson(path, args.get("features") or [], args.get("crs"))
            elif action == "write_geopackage":
                return self._geopackage(path, args)
            else:
                return {"ok": False, "error": f"Неизвестное действие: {action}", "data": {}, "summary": ""}
            size = path.stat().st_size
            return {"ok": True, "data": {"path": str(path), "size": size, "format": path.suffix.lower()},
                    "summary": f"Создан файл {path} ({size} байт). Его можно читать через files, а геоформаты — открыть в QGIS."}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "data": {}, "summary": ""}

    @staticmethod
    def _atomic_text(path: Path, text: str) -> None:
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent), text=True)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(text)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp): os.unlink(tmp)

    def _excel(self, path: Path, sheets: dict[str, Any], append: bool = False) -> None:
            try:
                from openpyxl import Workbook, load_workbook
            except ImportError as exc:
                raise RuntimeError("Для XLSX установите openpyxl в .venv") from exc
            from openpyxl.styles import Font
            # Модели часто передают sheets как JSON-строку, а не словарь. Чиним молча:
            if isinstance(sheets, str):
                try:
                    sheets = json.loads(sheets or "{}")
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"sheets — некорректный JSON: {exc.msg} в позиции "
                        f"{exc.pos}. Передай объект {{лист: [строки]}} или "
                        f"корректную JSON-строку без двойного кодирования."
                    ) from exc
                # Двойное JSON-кодирование (наблюдалось в проде, 6d82e8b850c1):
                # после первого распарсивания осталась СТРОКА — пробуем ещё раз.
                if isinstance(sheets, str):
                    try:
                        sheets = json.loads(sheets)
                    except json.JSONDecodeError:
                        pass
            if not isinstance(sheets, dict):
                sheets = {"Данные": sheets if isinstance(sheets, list) else [sheets]}
            # Повторный вызов без overwrite ДОБАВЛЯЕТ листы к существующей
            # книге (сборка отчёта по частям), а не требует overwrite=true.
            if append and path.exists():
                try:
                    book = load_workbook(path)
                    if not book.sheetnames: book.create_sheet("Sheet1")
                except Exception as exc:
                    # Битый файл нельзя бесшумно дописать как книгу — но и
                    # терять работу модели тоже нельзя: просим overwrite=true.
                    raise ValueError(
                        f"Файл {path} существует, но не является читаемой "
                        f"XLSX-книгой ({type(exc).__name__}). Для перезаписи "
                        f"передай overwrite=true."
                    ) from exc
            else:
                book = Workbook()
                book.remove(book.active)
            for raw_name, raw_rows in sheets.items():
                name = str(raw_name)[:31] or "Sheet1"
                sheet = book.create_sheet(name)
                rows = [r if isinstance(r, dict) else {"value": r} for r in (raw_rows or [])]
                fields = list(dict.fromkeys(k for row in rows for k in row))
                if fields:
                    sheet.append(fields)
                    for cell in sheet[1]: cell.font = Font(bold=True)
                    for row in rows: sheet.append([row.get(k) for k in fields])
                sheet.freeze_panes = "A2"
                sheet.auto_filter.ref = sheet.dimensions
                for col in sheet.columns:
                    width = min(60, max(10, max(len(str(c.value or "")) for c in col) + 2))
                    sheet.column_dimensions[col[0].column_letter].width = width
            if not book.sheetnames: book.create_sheet("Sheet1")
            book.save(path)

    def _word(self, path: Path, args: dict[str, Any]) -> None:
        try:
            from docx import Document
        except ImportError as exc:
            raise RuntimeError("Для DOCX установите python-docx в .venv") from exc
        doc = Document()
        if args.get("title"): doc.add_heading(str(args["title"]), level=1)
        for paragraph in args.get("paragraphs") or []: doc.add_paragraph(str(paragraph))
        for table_data in args.get("tables") or []:
            headers = [str(x) for x in table_data.get("headers") or []]
            rows = table_data.get("rows") or []
            table = doc.add_table(rows=1 if headers else 0, cols=max(1, len(headers) or max((len(r) for r in rows), default=1)))
            table.style = "Table Grid"
            if headers:
                for cell, value in zip(table.rows[0].cells, headers): cell.text = value
            for row in rows:
                cells = table.add_row().cells
                for cell, value in zip(cells, row): cell.text = "" if value is None else str(value)
        doc.save(path)

    def _csv(self, path: Path, rows: list[Any], order: list[str], delimiter: str) -> None:
        records = [r if isinstance(r, dict) else {"value": r} for r in rows]
        fields = list(order) or list(dict.fromkeys(k for r in records for k in r))
        with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="", delete=False, dir=path.parent) as f:
            tmp = f.name
            w = csv.DictWriter(f, fieldnames=fields, delimiter=delimiter, extrasaction="ignore")
            w.writeheader(); w.writerows(records)
        os.replace(tmp, path)

    def _geojson(self, path: Path, features: list[Any], crs: str | None) -> None:
        normalized = []
        for feature in features:
            if not isinstance(feature, dict): raise ValueError("Каждый feature должен быть объектом")
            if feature.get("type") != "Feature":
                feature = {"type":"Feature", "properties": feature, "geometry": None}
            if "properties" not in feature: feature["properties"] = {}
            normalized.append(feature)
        obj = {"type":"FeatureCollection", "features": normalized}
        if crs and crs.upper() != "EPSG:4326":
            obj["crs"] = {"type":"name", "properties":{"name": crs}}
        self._atomic_text(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")

    def _geopackage(self, path: Path, args: dict[str, Any]) -> dict[str, Any]:
        try:
            import fiona
        except ImportError:
            return {"ok": False, "error": "Для GeoPackage нужен пакет fiona; используйте write_geojson или установите fiona.", "data": {}, "summary": ""}
        features = args.get("features") or []
        geom = next((f.get("geometry") for f in features if isinstance(f, dict) and f.get("geometry")), None)
        if not geom: return {"ok": False, "error": "Для GeoPackage нужен хотя бы один geometry", "data": {}, "summary": ""}
        props = next((f.get("properties", {}) for f in features if isinstance(f, dict)), {})
        schema = {"geometry": geom["type"], "properties": {k: "str" for k in props}}
        mode = "w"; layer = str(args["layer_name"])
        with fiona.open(path, mode=mode, driver="GPKG", layer=layer, schema=schema, crs=args.get("crs") or "EPSG:4326") as dst:
            for f in features: dst.write({"type":"Feature", "geometry":f.get("geometry"), "properties":{k:str(v) if v is not None else None for k,v in f.get("properties",{}).items()}})
        return {"ok": True, "data": {"path": str(path), "layer_name": layer, "size": path.stat().st_size}, "summary": f"Создан GeoPackage {path}, слой {layer}. Откройте его в QGIS."}
