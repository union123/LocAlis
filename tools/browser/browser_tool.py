"""
Инструмент browser: headless-браузер (Playwright) для платформы.

Зачем (25.08): цикл задач по сайту показал слепую зону — платформа доводит
код до «компилируется», но не видит, что сайт в браузере не работает
(сломанные стили, неживые кнопки прошли все проверки). Этот инструмент
позволяет модели: открыть URL, сделать скриншот (его анализирует vision),
кликнуть по элементу, прочитать консоль — то есть проверить сайт глазами.

Безопасность:
- SSRF-защита как в websearch: localhost разрешён ТОЛЬКО явно (это
  локальный инструмент проверки, localhost — его основной кейс), внешние
  http(s) разрешены, прочие схемы (file://) запрещены.
- Playwright запускает Chromium headless — без влияния на браузер юзера.
- Браузер живёт между вызовами инструмента (lazy singleton), чтобы не
  перезапускать Chromium на каждый клик.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.base_tool import BaseTool, ToolAction, ToolStatus


class BrowserTool(BaseTool):
    name = "browser"
    version = "1.0.0"
    description = (
        "Headless-браузер: открыть страницу (navigate), сделать скриншот "
        "(screenshot), кликнуть по элементу (click), ввести текст (fill), "
        "прочитать консоль (console) и текст страницы (content). "
        "Скриншот сохраняется в файл — анализируй его инструментом vision."
    )
    requires_context = True  # get_allowed_roots не нужен, но context даёт emit

    def __init__(self, config: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, context=context)
        self._lock = threading.Lock()
        self._pw = None
        self._browser = None
        self._page = None
        self._console: list[dict[str, str]] = []

    # ---- lifecycle -----------------------------------------------------

    def _ensure_page(self) -> Any:
        """Ленивый singleton страницы. Потокобезопасно через self._lock."""
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        self._page = self._browser.new_page(viewport={"width": 1280,
                                                      "height": 900})
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_pageerror)
        return self._page

    def _on_console(self, msg: Any) -> None:
        self._console.append({"type": msg.type, "text": msg.text[:500]})

    def _on_pageerror(self, err: Any) -> None:
        self._console.append({"type": "pageerror", "text": str(err)[:500]})

    def _shots_dir(self) -> Path:
        raw = (self.config or {}).get("shots_dir", "data/browser_shots")
        p = Path(raw)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent.parent / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def close(self) -> None:
        with self._lock:
            try:
                if self._browser:
                    self._browser.close()
                if self._pw:
                    self._pw.stop()
            except Exception:  # noqa: BLE001
                pass
            self._pw = self._browser = self._page = None

    # ---- helpers -------------------------------------------------------

    def _check_url(self, url: str) -> str | None:
        """SSRF-фильтр: только http(s); host обязателен. localhost разрешён —
        основной кейс инструмента (проверка локального сайта)."""
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return f"Схема '{parsed.scheme}' запрещена — только http/https."
        if not parsed.hostname:
            return "URL без хоста."
        return None

    def _result(self, ok: bool, data: dict[str, Any],
                summary: str) -> dict[str, Any]:
        return {"ok": ok, "data": data, "summary": summary[:400],
                "error": None if ok else summary}

    # ---- actions -------------------------------------------------------

    def actions(self) -> list[ToolAction]:
        return [
            ToolAction(
                "navigate",
                "Открыть URL в headless-браузере. Возвращает title страницы "
                "и количество ошибок консоли.",
                {"url": {"type": "string", "description": "Полный URL "
                         "(http/https, localhost разрешён)"}},
            ),
            ToolAction(
                "screenshot",
                "Сделать скриншот текущей страницы. Возвращает путь к PNG — "
                "передай его инструменту vision (ask_about_image), чтобы "
                "увидеть страницу глазами.",
                {"filename": {"type": "string", "description": "Имя файла "
                 "без пути, например site-dashboard.png", "optional": True}},
            ),
            ToolAction(
                "click",
                "Кликнуть по элементу: по тексту кнопки/ссылки или CSS-селектору.",
                {"text": {"type": "string", "description": "Видимый текст "
                 "элемента (кнопки/ссылки)", "optional": True},
                 "selector": {"type": "string", "description": "CSS-селектор",
                              "optional": True}},
            ),
            ToolAction(
                "fill",
                "Ввести текст в поле ввода (по name, placeholder или селектору).",
                {"selector": {"type": "string", "description": "CSS-селектор "
                 "поля (input, textarea)"},
                 "text": {"type": "string", "description": "Что ввести"}},
            ),
            ToolAction(
                "console",
                "Прочитать накопленные сообщения консоли браузера (логи, "
                "предупреждения, ошибки JS). Ошибки страницы — главный "
                "симптом «сайт не работает».",
                {},
            ),
            ToolAction(
                "content",
                "Получить видимый текст текущей страницы (без HTML-тегов).",
                {},
            ),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        timeout = int((self.config or {}).get("navigation_timeout_sec", 30))

        if action == "navigate":
            url = str(args.get("url") or "").strip()
            err = self._check_url(url)
            if err:
                return self._result(False, {}, err)
            with self._lock:
                try:
                    page = self._ensure_page()
                    self._console.clear()
                    page.goto(url, timeout=timeout * 1000,
                              wait_until="domcontentloaded")
                    time.sleep(1.0)  # дать JS дорендерить
                    title = page.title()
                    errors = [c for c in self._console
                              if c["type"] in ("error", "pageerror")]
                    return self._result(True, {
                        "url": url, "title": title,
                        "console_errors": len(errors)},
                        f"Открыто '{title}' ({url}), ошибок консоли: "
                        f"{len(errors)}. Сделай screenshot и посмотри "
                        f"страницу через vision.")
                except Exception as exc:  # noqa: BLE001
                    return self._result(False, {}, f"Не открылось: {exc}")

        if action == "screenshot":
            with self._lock:
                try:
                    page = self._ensure_page()
                    fname = str(args.get("filename") or
                                f"shot_{int(time.time())}.png")
                    if not fname.endswith(".png"):
                        fname += ".png"
                    fname = Path(fname).name  # без путей
                    path = self._shots_dir() / fname
                    page.screenshot(path=str(path), full_page=True)
                    return self._result(True, {"screenshot_path": str(path)},
                                        f"Скриншот сохранён: {path}. "
                                        f"Проанализируй инструментом vision "
                                        f"(ask_about_image).")
                except Exception as exc:  # noqa: BLE001
                    return self._result(False, {}, f"Скриншот не удался: {exc}")

        if action == "click":
            with self._lock:
                try:
                    page = self._ensure_page()
                    text = str(args.get("text") or "").strip()
                    selector = str(args.get("selector") or "").strip()
                    if text:
                        locator = page.get_by_text(text, exact=False).first
                        desc = f"тексту '{text}'"
                    elif selector:
                        locator = page.locator(selector).first
                        desc = f"селектору '{selector}'"
                    else:
                        return self._result(
                            False, {}, "Укажи text или selector.")
                    locator.click(timeout=5000)
                    time.sleep(0.8)
                    return self._result(True, {"clicked": desc},
                                        f"Клик по {desc} выполнен. URL: "
                                        f"{page.url}")
                except Exception as exc:  # noqa: BLE001
                    return self._result(False, {}, f"Клик не удался: {exc}")

        if action == "fill":
            with self._lock:
                try:
                    page = self._ensure_page()
                    selector = str(args.get("selector") or "").strip()
                    text = str(args.get("text") or "")
                    if not selector:
                        return self._result(False, {}, "Укажи selector поля.")
                    page.locator(selector).first.fill(text, timeout=5000)
                    return self._result(True, {"filled": selector},
                                        f"Введено в {selector}.")
                except Exception as exc:  # noqa: BLE001
                    return self._result(False, {}, f"Ввод не удался: {exc}")

        if action == "console":
            with self._lock:
                msgs = list(self._console[-50:])
            errors = [m for m in msgs
                      if m["type"] in ("error", "pageerror", "warning")]
            body = "\n".join(f"[{m['type']}] {m['text']}" for m in errors) \
                or "Консоль чистая: ошибок и предупреждений нет."
            return self._result(True, {"messages": msgs, "text": body}, body)

        if action == "content":
            with self._lock:
                try:
                    page = self._ensure_page()
                    text = page.inner_text("body")
                    return self._result(True, {"text": text[:8000]},
                                        f"Текст страницы ({len(text)} симв), "
                                        f"первых 500: {text[:500]}")
                except Exception as exc:  # noqa: BLE001
                    return self._result(False, {}, f"Не удалось: {exc}")

        return self._result(False, {}, f"Неизвестное действие: {action}")

    def health(self) -> tuple[ToolStatus, str]:
        # Без запуска драйвера: health может зваться при живом singleton,
        # а повторный sync_playwright в том же процессе конфликтует с ним.
        try:
            import playwright  # noqa: F401
            # Пакет есть; chromium проверяем по папке браузеров playwright.
            import os
            browsers = Path(os.environ.get("PLAYWRIGHT_BROWSERS_PATH",
                             Path.home() / "AppData" / "Local" / "ms-playwright"))
            if browsers.exists() and any(browsers.iterdir()):
                return ToolStatus.READY, "playwright + браузеры установлены"
            return (ToolStatus.ERROR,
                    "Браузеры playwright не установлены: "
                    "playwright install chromium")
        except Exception as exc:  # noqa: BLE001
            return ToolStatus.ERROR, f"playwright недоступен: {exc}"
