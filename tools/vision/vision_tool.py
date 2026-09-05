"""
Инструмент компьютерного зрения: описание изображений и ответы на вопросы
по картинке через локальную мультимодальную модель Ollama.

Прямой HTTP-вызов к Ollama (localhost:11434/api/generate), а не через
возможность платформы ASK_LOCAL_MODEL — requires_context = False.
Изображение можно передать тремя способами: путь к файлу на диске, URL
(скачивается через httpx) или готовая base64-строка.

Известное ограничение на момент установки: на сервере работает Ollama с
набором текстовых моделей, но без мультимодальной. health() и execute()
в этом случае возвращают понятную ошибку с точной командой для загрузки
модели, а не молча падают — см. CONTRIBUTING_TOOLS.md про health-check.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path
from typing import Any

import httpx

from core.base_tool import BaseTool, ToolAction
from core.schemas import ToolStatus


def _looks_like_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value, re.IGNORECASE))


class VisionTool(BaseTool):
    """Описание изображений и ответы на вопросы по картинке через Ollama (мультимодальная модель)."""

    name = "vision"
    version = "1.0.0"
    description = "Описать изображение или ответить на вопрос по картинке через локальную мультимодальную модель Ollama"
    requires_context = False

    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        return str(self.config.get("ollama_url", "http://localhost:11434")).rstrip("/")

    def _default_model(self) -> str:
        return str(self.config.get("model", "qwen2.5vl:7b"))

    def _list_models(self, timeout: float = 5.0) -> list[str] | None:
        try:
            resp = httpx.get(f"{self._base_url()}/api/tags", timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            return None

    def _looks_like_vision_model(self, model_name: str) -> bool:
        hints = self.config.get("vision_name_hints") or ["vl", "vision", "llava", "moondream", "bakllava", "minicpm-v"]
        low = model_name.lower()
        return any(hint.lower() in low for hint in hints)

    def health(self) -> tuple[ToolStatus, str]:
        models = self._list_models()
        if models is None:
            return (ToolStatus.UNAVAILABLE,
                    f"Ollama не отвечает на {self._base_url()}. Убедитесь, что Ollama запущен.")
        model = self._default_model()
        if model in models:
            return ToolStatus.READY, f"Ollama доступен, модель '{model}' загружена"
        vision_candidates = [m for m in models if self._looks_like_vision_model(m)]
        if vision_candidates:
            return (ToolStatus.READY,
                    f"Модель по умолчанию '{model}' не найдена, но есть другие мультимодальные "
                    f"модели: {', '.join(vision_candidates)}. Укажите model= в аргументах действия.")
        return (ToolStatus.UNAVAILABLE,
                f"В Ollama нет ни одной мультимодальной модели (проверено {len(models)} моделей: "
                f"{', '.join(models) if models else 'нет'}). Загрузите одну командой: "
                f"ollama pull {model}  (или лёгкий вариант: ollama pull moondream)")

    def actions(self) -> list[ToolAction]:
        return [
            ToolAction(
                "list_vision_models",
                "Показать, какие модели загружены в Ollama и какие из них похожи на "
                "мультимодальные (умеющие работать с изображениями).",
                {"type": "object", "properties": {}},
            ),
            ToolAction(
                "ask_about_image",
                "Описать изображение или ответить на вопрос по нему через локальную "
                "мультимодальную модель Ollama. Передай ровно один источник картинки: "
                "path (файл на диске), url (скачать) или image_base64 (уже в base64).",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Путь к файлу изображения на диске"},
                        "url": {"type": "string", "description": "Ссылка на изображение"},
                        "image_base64": {"type": "string", "description": "Изображение, уже закодированное в base64"},
                        "question": {
                            "type": "string",
                            "description": "Вопрос или инструкция к изображению. По умолчанию — "
                                           "подробное описание сцены. Для OCR укажи явно, "
                                           "например 'Распознай весь текст на изображении'.",
                        },
                        "model": {"type": "string", "description": "Имя модели Ollama (по умолчанию из config)"},
                    },
                    "required": [],
                },
            ),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        if action == "list_vision_models":
            return self._do_list_vision_models()
        if action == "ask_about_image":
            return self._do_ask_about_image(args)
        return {"ok": False, "data": {}, "summary": "",
                "error": f"Неизвестное действие: {action}"}

    # ------------------------------------------------------------------

    def _do_list_vision_models(self) -> dict[str, Any]:
        models = self._list_models()
        if models is None:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Ollama не отвечает на {self._base_url()}"}
        vision = [m for m in models if self._looks_like_vision_model(m)]
        summary = (f"Всего моделей: {len(models)}, похожих на мультимодальные: {len(vision)}"
                   + (f" ({', '.join(vision)})" if vision else " — ни одной, нужно 'ollama pull <модель>'"))
        return {"ok": True, "data": {"all_models": models, "vision_models": vision},
                "summary": summary, "error": None}

    def _resolve_image_base64(self, args: dict[str, Any]) -> tuple[str | None, str | None]:
        """Вернуть (base64_строка, ошибка)."""
        path = args.get("path")
        url = args.get("url")
        image_b64 = args.get("image_base64")

        provided = [v for v in (path, url, image_b64) if v]
        if len(provided) == 0:
            return None, "Укажи один из аргументов: path, url или image_base64"
        if len(provided) > 1:
            return None, "Укажи только ОДИН источник изображения: path, url или image_base64"

        if image_b64:
            return str(image_b64), None

        if path:
            p = Path(str(path)).expanduser()
            try:
                raw = p.read_bytes()
            except FileNotFoundError:
                return None, f"Файл не найден: {p}"
            except Exception as exc:  # noqa: BLE001
                return None, f"Не удалось прочитать файл {p}: {type(exc).__name__}: {exc}"
            return base64.b64encode(raw).decode("ascii"), None

        if url:
            u = str(url)
            if not _looks_like_url(u):
                return None, f"'{u}' не похож на http(s) ссылку"
            try:
                resp = httpx.get(u, timeout=30.0, follow_redirects=True)
                resp.raise_for_status()
                return base64.b64encode(resp.content).decode("ascii"), None
            except httpx.HTTPStatusError as exc:
                return None, f"HTTP {exc.response.status_code} при скачивании {u}"
            except httpx.RequestError as exc:
                return None, f"Ошибка сети при скачивании {u}: {type(exc).__name__}: {exc}"

        return None, "Не удалось определить источник изображения"

    def _do_ask_about_image(self, args: dict[str, Any]) -> dict[str, Any]:
        image_b64, err = self._resolve_image_base64(args)
        if err:
            return {"ok": False, "data": {}, "summary": "", "error": err}

        model = str(args.get("model") or self._default_model())
        question = str(args.get("question") or "Опиши это изображение подробно на русском языке.")
        timeout = float(self.config.get("request_timeout_sec", 180))

        payload = {
            "model": model,
            "prompt": question,
            "images": [image_b64],
            "stream": False,
        }
        try:
            resp = httpx.post(f"{self._base_url()}/api/generate", json=payload, timeout=timeout)
        except httpx.RequestError as exc:
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Ollama недоступен на {self._base_url()}: {type(exc).__name__}: {exc}"}

        if resp.status_code == 404:
            return {"ok": False, "data": {"model": model}, "summary": "",
                    "error": f"Модель '{model}' не найдена в Ollama. Загрузите: ollama pull {model}"}
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            return {"ok": False, "data": {"model": model}, "summary": "",
                    "error": f"Ollama вернул HTTP {exc.response.status_code}: {resp.text[:300]}"}

        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "data": {}, "summary": "",
                    "error": f"Не удалось разобрать ответ Ollama: {type(exc).__name__}: {exc}"}

        answer = str(data.get("response", "")).strip()
        if not answer:
            return {"ok": False, "data": {"model": model}, "summary": "",
                    "error": "Ollama вернул пустой ответ"}

        summary = answer if len(answer) <= 300 else answer[:300] + "…"
        return {"ok": True, "data": {"model": model, "question": question, "answer": answer},
                "summary": summary, "error": None}
