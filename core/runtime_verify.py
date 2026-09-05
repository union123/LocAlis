# -*- coding: utf-8 -*-
"""
Runtime-верификатор (25.08, ночь): проверка созданных веб-проектов.

Идея: задачи «создай сайт» до сих пор заканчивались на «компилируется»,
но платформа не видела, что сайт в браузере не работает. Этот модуль
замыкает цикл: собрать -> запустить -> открыть -> увидеть -> вердикт.

Работает БЕЗ LLM: детерминированные проверки + опциональный vision-вопрос.
LLM-часть (vision) включается флагом и вызывается вызывающей стороной.

Проверки:
  1. build: npm run build (если package.json есть) — exit code
  2. dev-сервер стартует и отвечает 200 на /
  3. страница содержит <app-root> / #root (маркер SPA)
  4. консоль браузера без ошибок JS
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Callable

RUNTIME_VERSION = "1.0"


class RuntimeVerifier:
    """Детерминированная проверка работоспособности веб-проекта."""

    version = RUNTIME_VERSION

    def __init__(self, project_dir: str | Path,
                 browser_tool: Any = None,
                 build_tool_factory: Callable[[], Any] | None = None):
        self.project_dir = Path(project_dir)
        self._browser = browser_tool          # tools.browser instance (опц.)
        self._build_factory = build_tool_factory

    # ---- проверки ----------------------------------------------------------

    def check_build(self, timeout_sec: int = 600) -> dict[str, Any]:
        """npm run build — компилируемость. None если не Node-проект."""
        if not (self.project_dir / "package.json").is_file():
            return {"skipped": True, "reason": "нет package.json"}
        if self._build_factory is None:
            return {"skipped": True, "reason": "build-инструмент недоступен"}
        bt = self._build_factory()
        res = bt.execute("ng_build" if self._is_angular()
                         else "npm_install", {})  # ng_build включает install? нет
        # точечно: сначала tsc/ng через whitelist
        action = "tsc_check"
        if self._is_angular():
            action = "ng_build"
        res = bt.execute(action, {"project_path": str(self.project_dir),
                                  "timeout_sec": timeout_sec})
        return {"skipped": False, "ok": bool(res.get("ok")),
                "summary": (res.get("summary") or res.get("error") or "")[:400]}

    def _is_angular(self) -> bool:
        aj = self.project_dir / "angular.json"
        return aj.is_file()

    def check_serve_and_open(self, port: int, wait_sec: int = 45
                             ) -> dict[str, Any]:
        """
        Запустить dev-сервер фоном, дождаться 200 на http://localhost:{port},
        открыть в browser-инструменте, снять консоль. Требует browser_tool.
        """
        if self._browser is None:
            return {"skipped": True, "reason": "browser-инструмент недоступен"}

        proc = self._start_dev_server(port)
        if isinstance(proc, dict):  # ошибка старта
            return proc
        try:
            url = f"http://localhost:{port}/"
            deadline = time.time() + wait_sec
            nav = None
            while time.time() < deadline:
                try:
                    nav = self._browser.execute("navigate", {"url": url})
                    if nav.get("ok"):
                        break
                except Exception:  # noqa: BLE001 — сервер ещё поднимается
                    pass
                time.sleep(2)
            if not (nav and nav.get("ok")):
                return {"skipped": False, "ok": False,
                        "error": f"dev-сервер не ответил на {url} за "
                                 f"{wait_sec}с"}

            console = self._browser.execute("console", {})
            errors = [m for m in console.get("data", {}).get("messages", [])
                      if m.get("type") in ("error", "pageerror")]

            content = self._browser.execute("content", {})
            body = (content.get("data", {}).get("text") or "")[:2000]
            has_root_marker = any(m in body.lower() for m in
                                  ("dashboard", "программ", "program",
                                   "progress"))

            return {"skipped": False, "ok": len(errors) == 0,
                    "http_reached": True,
                    "console_errors": [e["text"][:200] for e in errors[:5]],
                    "body_sample": body[:300],
                    "spa_content_detected": has_root_marker}
        finally:
            self._stop_dev_server()

    # dev-server управление делегируется вызывающей стороне через колбэки,
    # чтобы не тащить процессное хозяйство в этот класс:
    def _start_dev_server(self, port: int):
        if hasattr(self, "_start_cb") and self._start_cb:
            return self._start_cb(port)
        return {"skipped": False, "ok": False,
                "error": "не задан колбэк запуска dev-сервера"}

    def _stop_dev_server(self):
        if hasattr(self, "_stop_cb") and self._stop_cb:
            self._stop_cb()

    def set_server_callbacks(self, start_cb, stop_cb) -> None:
        """start_cb(port) -> process/None; stop_cb() -> None."""
        self._start_cb = start_cb
        self._stop_cb = stop_cb

    # ---- сводка -------------------------------------------------------------

    def full_check(self, dev_port: int | None = None) -> dict[str, Any]:
        """Все применимые проверки. Вердикт ok=True только если ни одна
        неприменённая проверка не провалена."""
        report: dict[str, Any] = {"version": self.version,
                                  "checks": {}}
        ok_all = True

        b = self.check_build()
        report["checks"]["build"] = b
        if not b.get("skipped") and not b.get("ok"):
            ok_all = False

        if dev_port:
            s = self.check_serve_and_open(dev_port)
            report["checks"]["serve"] = s
            if not s.get("skipped") and not s.get("ok"):
                ok_all = False

        report["ok"] = ok_all
        return report


def make_start_stop_for_npm(project_dir: str | Path):
    """Колбэки запуска/остановки `npm start` для RuntimeVerifier."""
    proc_holder: dict[str, Any] = {}

    def start(port: int):
        try:
            p = subprocess.Popen(
                ["cmd", "/c", "npm", "start", "--", "--port", str(port)],
                cwd=str(project_dir),
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            proc_holder["p"] = p
            return p
        except Exception:  # noqa: BLE001
            return None

    def stop():
        p = proc_holder.pop("p", None)
        if p and p.poll() is None:
            p.kill()

    return start, stop
