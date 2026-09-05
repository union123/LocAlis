"""
Инструмент построения графиков.

Продолжение datafiles: модели уже умеют считать и сохранять таблицы,
логичный следующий шаг — диаграмма, которую можно вложить в отчёт.

Важное решение: matplotlib работает в режиме Agg (без графического окна).
Иначе на Windows он пытается создать окно из фонового потока и падает —
а Proposers выполняются именно в пуле потоков.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

# ДО импорта pyplot: backend без графического окна
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from core.base_tool import BaseTool, ToolAction  # noqa: E402
from core.fs_paths import describe_known_folders  # noqa: E402
from core.fs_paths import normalize as _normalize_path  # noqa: E402
from core.schemas import ToolStatus  # noqa: E402

_KINDS = ("bar", "barh", "line", "pie", "scatter")

_KIND_RU = {"bar": "столбчатый", "barh": "горизонтальный столбчатый",
            "line": "линейный", "pie": "круговой", "scatter": "точечный"}


class ChartsTool(BaseTool):
    """Графики в PNG: столбцы, линия, круговая, точки."""

    name = "charts"
    version = "1.0.0"
    description = "Построить график по числовым данным и сохранить картинку PNG"

    def health(self) -> tuple[ToolStatus, str]:
        return ToolStatus.READY, f"matplotlib {matplotlib.__version__} (режим без окна)"

    def actions(self) -> list[ToolAction]:
        where = (" Можно указать известную папку, например 'Рабочий стол/график.png'. "
                 "Реальные папки: " + describe_known_folders())
        return [
            ToolAction(
                "plot",
                "Построить график и сохранить в PNG. Виды: bar (столбцы), "
                "barh (горизонтальные столбцы), line (линия), pie (круговая), "
                "scatter (точки). Данные передавай уже посчитанными.",
                {
                    "type": "object",
                    "properties": {
                        "output_path": {"type": "string",
                                        "description": "Путь к PNG-файлу." + where},
                        "kind": {"type": "string", "enum": list(_KINDS),
                                 "description": "Вид графика"},
                        "labels": {"type": "array", "items": {"type": "string"},
                                   "description": "Подписи по оси X или названия секторов"},
                        "values": {"type": "array", "items": {"type": "number"},
                                   "description": "Числовые значения одной серии"},
                        "series": {"type": "object",
                                   "description": ("Несколько серий: имя -> массив чисел. "
                                                   "Используй вместо values для сравнения")},
                        "title": {"type": "string", "description": "Заголовок графика"},
                        "x_label": {"type": "string"},
                        "y_label": {"type": "string"},
                        "overwrite": {"type": "boolean"},
                    },
                    "required": ["output_path", "kind"],
                },
            ),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action != "plot":
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Неизвестное действие: {action}"}
        return self._plot(args)

    # ---- вспомогательное --------------------------------------------------

    def _numbers(self, raw: Any) -> list[float]:
        """Привести значения к числам. Модели присылают числа строками."""
        limit = int(self.config.get("max_points", 200))
        out: list[float] = []
        for item in (raw or [])[:limit]:
            try:
                out.append(float(item))
            except (TypeError, ValueError):
                continue
        return out

    def _plot(self, args: dict[str, Any]) -> dict[str, Any]:
        raw_path = args.get("output_path")
        if not raw_path:
            return {"ok": False, "data": {}, "summary": "", "error": "Не указан output_path"}
        path = Path(_normalize_path(raw_path))
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
        if path.exists() and not args.get("overwrite"):
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Файл уже существует: {path}. Передайте overwrite=true."}

        kind = str(args.get("kind") or "bar").lower()
        if kind not in _KINDS:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Вид '{kind}' не поддерживается. Доступны: {', '.join(_KINDS)}"}

        limit = int(self.config.get("max_points", 200))
        labels = [str(x) for x in (args.get("labels") or [])][:limit]
        raw_series = args.get("series") if isinstance(args.get("series"), dict) else None
        if raw_series:
            data = {str(k): self._numbers(v) for k, v in raw_series.items()}
        else:
            data = {str(args.get("y_label") or "значение"): self._numbers(args.get("values"))}
        data = {k: v for k, v in data.items() if v}
        if not data:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Нет числовых данных: передайте values или series"}
        if kind == "pie" and len(data) > 1:
            return {"ok": False, "data": {}, "summary": "",
                    "error": "Круговая диаграмма строится по одной серии, не по нескольким"}

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            figure, axes = plt.subplots(
                figsize=(float(self.config.get("width_inch", 9)),
                         float(self.config.get("height_inch", 5))))
            if not labels:
                labels = [str(i + 1) for i in range(len(next(iter(data.values()))))]
            # Выравниваем длины: модель часто присылает 5 подписей и 4 значения
            width = min(len(labels), min(len(v) for v in data.values()))
            labels = labels[:width]
            data = {k: v[:width] for k, v in data.items()}
            first = next(iter(data.values()))

            if kind == "pie":
                axes.pie(first, labels=labels, autopct="%1.1f%%", startangle=90)
                axes.axis("equal")
            elif kind == "line":
                for name, points in data.items():
                    axes.plot(labels, points, marker="o", label=name)
            elif kind == "scatter":
                for name, points in data.items():
                    axes.scatter(labels, points, label=name)
            else:
                self._grouped(axes, labels, data, horizontal=(kind == "barh"))
                # Один-два столбца растягивались на всю ширину и выглядели
                # как сплошная заливка. Ограничиваем область по краям.
                if width <= 2:
                    if kind == "barh":
                        axes.set_ylim(-1, width)
                    else:
                        axes.set_xlim(-1, width)

            if kind in ("bar", "barh", "line", "scatter"):
                axes.grid(axis="x" if kind == "barh" else "y",
                          alpha=0.25, linestyle="--")
                axes.set_axisbelow(True)
                for side in ("top", "right"):
                    axes.spines[side].set_visible(False)
            if args.get("title"):
                axes.set_title(str(args["title"]))
            if kind != "pie":
                if args.get("x_label"):
                    axes.set_xlabel(str(args["x_label"]))
                if args.get("y_label"):
                    axes.set_ylabel(str(args["y_label"]))
                if len(data) > 1:
                    axes.legend()
                # Длинные подписи наклоняем, иначе они наезжают друг на друга
                # Наклон нужен только при тесноте: при 1-3 подписях он
                # выглядит неряшливо и ничего не решает
                if (labels and kind != "barh" and len(labels) > 3
                        and max(len(text) for text in labels) > 8):
                    plt.setp(axes.get_xticklabels(), rotation=30, ha="right")
            figure.tight_layout()
            figure.savefig(path, dpi=int(self.config.get("dpi", 130)))
            plt.close(figure)
        except Exception as exc:  # noqa: BLE001
            plt.close("all")
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Не удалось построить график: {type(exc).__name__}: {exc}"}

        size = path.stat().st_size
        return {
            "ok": True,
            "data": {"path": str(path), "size": size, "kind": kind,
                     "points": width, "series": list(data)},
            "summary": (f"Создан {_KIND_RU[kind]} график: {path} ({size} байт, "
                        f"точек: {width}, серий: {len(data)}). "
                        "Картинку можно вложить в отчёт Word через datafiles."),
        }

    @staticmethod
    def _grouped(axes: Any, labels: list[str], data: dict[str, list[float]],
                 horizontal: bool) -> None:
        """Одна серия — обычные столбцы, несколько — сгруппированные рядом."""
        count = len(data)
        positions = list(range(len(labels)))
        bar_width = 0.8 / count
        for index, (name, points) in enumerate(data.items()):
            offset = [p + index * bar_width - 0.4 + bar_width / 2 for p in positions]
            if horizontal:
                axes.barh(offset, points, height=bar_width, label=name)
            else:
                axes.bar(offset, points, width=bar_width, label=name)
        if horizontal:
            axes.set_yticks(positions)
            axes.set_yticklabels(labels)
        else:
            axes.set_xticks(positions)
            axes.set_xticklabels(labels)
