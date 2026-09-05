"""
Разбор вызовов инструментов, которые модель вернула ТЕКСТОМ, а не в поле
tool_calls.

Зачем это нужно (проверено на реальных моделях через Ollama):
  - qwen2.5-coder:7b печатает JSON {"name": ..., "arguments": {...}} в content;
  - glm4:9b часто отвечает прозой и вовсе игнорирует схему функций;
  - часть моделей оборачивает вызов в <tool_call>...</tool_call> или ```json.

Без этого слоя половина Proposers "не видит" инструменты — это и была
типичная причина того, что tools не работают.
"""

from __future__ import annotations

import json
import re
from typing import Any

from core.schemas import ToolCallRequest

# <tool_call>{...}</tool_call>  — формат Qwen/Hermes
_TAG_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
# ```json { ... } ```
_FENCE_RE = re.compile(r"```(?:json|tool_code)?\s*(\{.*?\})\s*```", re.DOTALL)


def _iter_json_objects(text: str):
    """Найти в тексте верхнеуровневые JSON-объекты (по балансу скобок)."""
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:i + 1]
                    start = -1


def _to_dict(obj: Any) -> Any:
    """
    Привести объект к словарю.

    Клиент Ollama возвращает tool_calls как Pydantic-объекты, а не dict —
        без этой нормализации штатные вызовы теряются (проверено тестом).
    """
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        try:
            return obj.model_dump()
        except Exception:  # noqa: BLE001
            pass
    if hasattr(obj, "dict") and callable(getattr(obj, "dict")):
        try:
            return obj.dict()
        except Exception:  # noqa: BLE001
            pass
    try:
        return dict(obj)
    except Exception:  # noqa: BLE001
        return obj


def _as_request(obj: Any, known_functions: set[str] | None) -> ToolCallRequest | None:
    """Превратить структуру произвольной формы в ToolCallRequest, если это вызов."""
    obj = _to_dict(obj)
    if not isinstance(obj, dict):
        return None

    # Форматы: {"name":..,"arguments":..} | {"function":{...}} | {"tool":..,"action":..}
    if "function" in obj:
        inner = _to_dict(obj["function"])
        if isinstance(inner, dict):
            obj = inner

    name = obj.get("name") or obj.get("tool_name")
    args = obj.get("arguments")
    if args is None:
        args = obj.get("parameters")
    if args is None:
        args = obj.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    args = _to_dict(args)
    if not isinstance(args, dict):
        args = {}
    args = {str(k): v for k, v in args.items()}

    if name:
        tool, _, action = str(name).partition("__")
        if not action:
            # "qgis.layer_info" или просто "layer_info" при явном поле tool
            tool, _, action = str(name).partition(".")
        if not action and obj.get("action"):
            tool, action = str(name), str(obj["action"])
        if tool and action:
            full = f"{tool}__{action}"
            if known_functions is None or full in known_functions:
                return ToolCallRequest(tool=tool, action=action, args=args)
        return None

    # {"tool": "qgis", "action": "layer_info", "args": {...}}
    if obj.get("tool") and obj.get("action"):
        full = f"{obj['tool']}__{obj['action']}"
        if known_functions is None or full in known_functions:
            return ToolCallRequest(tool=str(obj["tool"]), action=str(obj["action"]), args=args)
    return None


def parse_text_tool_calls(
    content: str,
    known_functions: set[str] | None = None,
    limit: int = 4,
) -> list[ToolCallRequest]:
    """
    Извлечь вызовы инструментов из текстового ответа модели.

    known_functions — множество имён вида "qgis__layer_info"; если задано,
    посторонние JSON-объекты (например, обычные данные) отбрасываются.
    """
    if not content:
        return []

    found: list[ToolCallRequest] = []
    seen: set[str] = set()

    chunks: list[str] = []
    chunks += _TAG_RE.findall(content)
    chunks += _FENCE_RE.findall(content)
    chunks += list(_iter_json_objects(content))

    for chunk in chunks:
        try:
            obj = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        candidates = obj if isinstance(obj, list) else [obj]
        for candidate in candidates:
            request = _as_request(candidate, known_functions)
            if request is None:
                continue
            key = f"{request.tool}__{request.action}:{json.dumps(request.args, sort_keys=True, ensure_ascii=False)}"
            if key in seen:
                continue
            seen.add(key)
            found.append(request)
            if len(found) >= limit:
                return found
    return found


def extract_tool_calls(message: dict[str, Any], known_functions: set[str] | None = None) -> list[ToolCallRequest]:
    """
    Единая точка: сначала штатное поле tool_calls, затем разбор текста.
    Принимает message из ответа Ollama/OpenAI-совместимого API.
    """
    message = _to_dict(message)
    if not isinstance(message, dict):
        return []

    requests: list[ToolCallRequest] = []
    for call in message.get("tool_calls") or []:
        request = _as_request(call, known_functions)
        if request is not None:
            requests.append(request)
    if requests:
        return requests
    return parse_text_tool_calls(message.get("content") or "", known_functions)
