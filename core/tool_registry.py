"""
Tool Registry — автозагрузка плагинов из папки tools/.

Правила discovery:
  tools/<plugin>/manifest.yaml  — обязателен (имя, версия, entrypoint)
  tools/<plugin>/<entrypoint>   — модуль с классом-наследником BaseTool

Ошибка одного плагина не ломает остальные: он помечается статусом ERROR
и остаётся видимым в UI с текстом ошибки.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from core.base_tool import BaseTool
from core.schemas import ToolCallRequest, ToolCallResult, ToolManifest, ToolStatus

REGISTRY_VERSION = "1.0"
DEFAULT_TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"


@dataclass
class ToolEntry:
    """Одна запись реестра: плагин + его состояние."""

    manifest: ToolManifest
    path: Path
    instance: BaseTool | None = None
    status: ToolStatus = ToolStatus.ERROR
    message: str = ""
    schema: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def usable(self) -> bool:
        return self.instance is not None and self.status is ToolStatus.READY


class ToolRegistry:
    """Реестр инструментов. Создаётся один раз при старте приложения."""

    def __init__(self, tools_dir: Path | str | None = None, config: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None):
        self.tools_dir = Path(tools_dir) if tools_dir else DEFAULT_TOOLS_DIR
        # config: {"qgis": {...}} — переопределения настроек по имени плагина
        self.config = config or {}
        # context — возможности платформы для инструментов-агентов
        self.context: dict[str, Any] = context or {}
        self.entries: dict[str, ToolEntry] = {}
        # Кеш ЧТЕНИЙ в рамках одной задачи: ключ "инструмент.действие:аргументы".
        # Сбрасывается методом clear_cache() на старте каждой задачи.
        self._call_cache: dict[str, ToolCallResult] = {}

    def set_context(self, context: dict[str, Any]) -> None:
        """
        Обновить контекст и раздать его уже загруженным инструментам.

        Нужно потому, что реестр создаётся раньше, чем шлюз и база знаний
        (избегаем круговой зависимости при сборке платформы).
        """
        self.context = context or {}
        for entry in self.entries.values():
            if entry.instance is None:
                continue
            entry.instance.context = self.context
            # Готовность пересчитываем: инструмент-агент без контекста был
            # unavailable, а с контекстом становится ready — без этого он навсегда
            # остался бы выключенным. Выключенные вручную не трогаем.
            if entry.status is ToolStatus.DISABLED:
                continue
            try:
                entry.status, entry.message = entry.instance.health()
            except Exception as exc:  # noqa: BLE001
                entry.status = ToolStatus.ERROR
                entry.message = f"Ошибка проверки готовности: {type(exc).__name__}: {exc}"

    # ---- discovery -------------------------------------------------------

    def discover(self) -> dict[str, ToolEntry]:
        """Просканировать tools/ и загрузить все плагины."""
        self.entries.clear()
        if not self.tools_dir.is_dir():
            return self.entries

        for plugin_dir in sorted(p for p in self.tools_dir.iterdir() if p.is_dir()):
            if plugin_dir.name.startswith((".", "_")):
                continue
            manifest_path = plugin_dir / "manifest.yaml"
            if not manifest_path.is_file():
                continue
            entry = self._load_plugin(plugin_dir, manifest_path)
            self.entries[entry.name] = entry
        return self.entries

    def _load_plugin(self, plugin_dir: Path, manifest_path: Path) -> ToolEntry:
        # 1. манифест
        try:
            raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
            manifest = ToolManifest(**raw)
        except Exception as exc:  # noqa: BLE001
            bad = ToolManifest(name=plugin_dir.name, enabled=False)
            return ToolEntry(
                manifest=bad, path=plugin_dir,
                status=ToolStatus.ERROR, message=f"Ошибка манифеста: {exc}",
            )

        entry = ToolEntry(manifest=manifest, path=plugin_dir)

        # 2. выключен пользователем — не импортируем вовсе
        if not manifest.enabled:
            entry.status = ToolStatus.DISABLED
            entry.message = "Выключен в manifest.yaml"
            return entry

        # 3. импорт модуля по пути (без требований к пакетам/__init__)
        module_path = plugin_dir / manifest.entrypoint
        if not module_path.is_file():
            entry.status = ToolStatus.ERROR
            entry.message = f"Не найден entrypoint: {module_path.name}"
            return entry

        try:
            module = self._import_module(module_path, f"agent_platform_tools.{plugin_dir.name}")
        except Exception as exc:  # noqa: BLE001
            entry.status = ToolStatus.ERROR
            entry.message = f"Ошибка импорта: {type(exc).__name__}: {exc}"
            return entry

        # 4. поиск класса-наследника BaseTool
        cls = self._find_tool_class(module, manifest.class_name)
        if cls is None:
            entry.status = ToolStatus.ERROR
            entry.message = "В модуле не найден класс-наследник BaseTool"
            return entry

        # 5. инстанс + health-check
        try:
            plugin_cfg = {**(getattr(manifest, "config", None) or {}), **self.config.get(manifest.name, {})}
            # Обратная совместимость: плагины, написанные до появления
            # контекста, принимают только config — для них второй аргумент
            # не передаём, а контекст выставляем атрибутом.
            try:
                instance = cls(config=plugin_cfg, context=self.context)
            except TypeError:
                instance = cls(config=plugin_cfg)
                instance.context = self.context
            status, message = instance.health()
            entry.instance = instance
            entry.status = status
            entry.message = message
            entry.schema = instance.input_schema()
            # версия из кода приоритетнее манифеста — она ближе к реальности
            if getattr(instance, "version", None):
                entry.manifest.version = instance.version
        except AttributeError as exc:
            # Типичная причина: приложение запущено до обновления ядра и держит
            # в памяти старый BaseTool без новых методов. Подсказываем решение,
            # а не показываем сырой AttributeError.
            missing = str(exc).split("'")[-2] if "'" in str(exc) else str(exc)
            entry.status = ToolStatus.ERROR
            entry.message = (
                f"Несовместимая версия ядра: нет '{missing}'. "
                "Перезапустите приложение (scripts\\start.bat) — "
                "сейчас оно работает со старым кодом, загруженным в память."
            )
        except Exception as exc:  # noqa: BLE001
            entry.status = ToolStatus.ERROR
            entry.message = f"Ошибка инициализации: {type(exc).__name__}: {exc}"
        return entry

    @staticmethod
    def _import_module(module_path: Path, module_name: str):
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"Не удалось создать spec для {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def _find_tool_class(module, class_name: str | None) -> type[BaseTool] | None:
        if class_name:
            cls = getattr(module, class_name, None)
            return cls if isinstance(cls, type) and issubclass(cls, BaseTool) else None
        for _, obj in inspect.getmembers(module, inspect.isclass):
            if issubclass(obj, BaseTool) and obj is not BaseTool and obj.__module__ == module.__name__:
                return obj
        return None

    # ---- доступ ----------------------------------------------------------

    def get(self, name: str) -> ToolEntry | None:
        return self.entries.get(name)

    def available(self) -> list[ToolEntry]:
        """Только готовые к работе инструменты."""
        return [e for e in self.entries.values() if e.usable]

    def content_hints(self) -> dict[str, list[str]]:
        """
        Доменные подсказки инструментов из манифестов (content_hints).
        Используется детерминированным ремонтом планов (orchestrator):
        ядро не хардкодит имена инструментов — сверяет текст шага со
        словами, объявленными самими плагинами (24.08, критика ревью).
        """
        hints: dict[str, list[str]] = {}
        for name, entry in self.entries.items():
            if not entry.usable:
                continue
            # ToolManifest объявлен с extra="allow": content_hints попадает
            # в модель как обычное поле
            raw = getattr(entry.manifest, "content_hints", None)
            if raw:
                hints[name] = [str(h).lower() for h in raw]
        return hints

    def schemas(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        """JSON-схемы готовых инструментов — то, что уходит в промпт моделям."""
        return [
            e.schema for e in self.available()
            if allowed is None or e.name in allowed
        ]

    def openai_tools(self, allowed: list[str] | None = None) -> list[dict[str, Any]]:
        """
        Схемы в формате OpenAI/Ollama function calling.
        Каждое действие плагина = отдельная функция "<tool>__<action>",
        так модели надёжнее попадают в нужный вызов, чем через вложенный action.
        """
        out: list[dict[str, Any]] = []
        for entry in self.available():
            if allowed is not None and entry.name not in allowed:
                continue
            assert entry.instance is not None
            for action in entry.instance.actions():
                out.append({
                    "type": "function",
                    "function": {
                        "name": f"{entry.name}__{action.name}",
                        "description": f"[{entry.name} v{entry.manifest.version}] {action.description}",
                        "parameters": action.parameters,
                    },
                })
        return out

    # ---- вызов -----------------------------------------------------------

    # Действия, которые МЕНЯЮТ состояние: их кешировать нельзя, иначе
    # повторная запись файла молча не выполнится. Кешируем только чтение.
    _CACHEABLE_PREFIXES = ("read", "list", "find", "info", "search", "known",
                           "layer_", "get")

    def _cache_key(self, request: ToolCallRequest) -> str | None:
        """Ключ кеша или None, если действие кешировать нельзя."""
        action = request.action.lower()
        if not any(action.startswith(prefix) for prefix in self._CACHEABLE_PREFIXES):
            return None
        try:
            args = json.dumps(request.args, sort_keys=True, ensure_ascii=False)
        except (TypeError, ValueError):
            return None
        return f"{request.tool}.{request.action}:{args}"

    def clear_cache(self) -> None:
        """
        Сбросить кеш чтений. Вызывается на старте каждой задачи: файлы
        на диске могли измениться между задачами, и старый ответ соврёт.
        """
        self._call_cache.clear()

    def call(self, request: ToolCallRequest) -> ToolCallResult:
        """
        Единая точка вызова инструмента. Никогда не бросает исключение.

        Одинаковые ЧТЕНИЯ в рамках одной задачи берутся из кеша: в реальном
        прогоне files.read_text вызывался дважды на одном файле и повторно
        читал 36 тысяч символов. Изменяющие действия не кешируются никогда.
        """
        key = self._cache_key(request)
        if key is not None and key in self._call_cache:
            cached = self._call_cache[key]
            # Отдаём копию с актуальным call_id: иначе журнал вызовов
            # склеит два разных обращения в одно
            return cached.model_copy(update={"call_id": request.call_id,
                                             "from_cache": True})
        entry = self.entries.get(request.tool)
        if entry is None:
            return ToolCallResult(
                call_id=request.call_id, tool=request.tool, action=request.action,
                ok=False, error=f"Инструмент '{request.tool}' не найден. "
                                f"Доступны: {sorted(e.name for e in self.available())}",
            )
        if not entry.usable:
            return ToolCallResult(
                call_id=request.call_id, tool=request.tool, action=request.action,
                ok=False, tool_version=entry.manifest.version,
                error=f"Инструмент '{request.tool}' недоступен ({entry.status.value}): {entry.message}",
            )
        assert entry.instance is not None
        result = entry.instance.run(request)
        # Кешируем только успешные чтения: ошибку стоит повторить —
        # пользователь мог создать файл между вызовами
        if key is not None and result.ok:
            self._call_cache[key] = result
        return result

    def call_openai_name(self, function_name: str, args: dict[str, Any]) -> ToolCallResult:
        """Вызов по имени функции вида '<tool>__<action>' из ответа модели."""
        tool_name, _, action = function_name.partition("__")
        return self.call(ToolCallRequest(tool=tool_name, action=action, args=args))
