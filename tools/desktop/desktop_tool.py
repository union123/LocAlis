"""
Инструмент desktop: управление ноутбуком целиком (Windows).

Мотивация (25.08): browser-инструмент закрыл слепую зону «сайт в браузере»,
но у платформы остались другие слепые зоны — любые приложения вне браузера:
Excel с геологическими данными, QGIS, проводник, калькулятор. Этот инструмент
даёт платформе глаза (скриншот экрана/окна -> vision) и руки (мышь, клавиатура).

Безопасность:
- pyautogui.FAILSAFE включён: мышь в левый верхний угол мгновенно прерывает
  любое действие (стандартная защита от «обезьяны с пистолетом»).
- Печатает в ТО, ЧТО В ФОКУСЕ — перед type/hotkey модель должна понимать,
  какое окно активно (list_windows + focus_window).
- Скриншоты складываются в data/browser_shots/ и анализируются vision'ом.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from core.base_tool import BaseTool, ToolAction, ToolStatus


class DesktopTool(BaseTool):
    name = "desktop"
    version = "1.0.0"
    description = (
        "Управление ноутбуком Windows: скриншот экрана или окна (screenshot), "
        "клик мышью (click), двойной клик (double_click), ввод текста (type), "
        "горячие клавиши (hotkey), нажатие клавиш (press), список окон "
        "(list_windows), фокус на окно (focus_window), прокрутка (scroll). "
        "Скриншот анализируй инструментом vision (ask_about_image), чтобы "
        "увидеть экран. Внимание: управляет реальной мышью/клавиатурой!"
    )
    requires_context = True

    def __init__(self, config: dict[str, Any] | None = None,
                 context: dict[str, Any] | None = None) -> None:
        super().__init__(config=config, context=context)
        self._lock = threading.Lock()

    # ---- helpers -------------------------------------------------------

    def _shots_dir(self) -> Path:
        raw = (self.config or {}).get("shots_dir", "data/browser_shots")
        p = Path(raw)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent.parent / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    def _pause(self) -> None:
        time.sleep(float((self.config or {}).get("action_pause_sec", 0.5)))

    def _result(self, ok: bool, data: dict[str, Any],
                summary: str) -> dict[str, Any]:
        return {"ok": ok, "data": data, "summary": summary[:400],
                "error": None if ok else summary}

    @staticmethod
    def _list_windows() -> list[dict[str, Any]]:
        """Окна верхнего уровня через ctypes user32 (без внешних пакетов)."""
        import ctypes
        from ctypes import wintypes

        windows: list[dict[str, Any]] = []
        user32 = ctypes.windll.user32
        EnumWindows = user32.EnumWindows
        GetWindowText = user32.GetWindowTextW
        IsWindowVisible = user32.IsWindowVisible
        GetWindowRect = user32.GetWindowRect
        GetClassName = user32.GetClassNameW

        WNDENUMPROC = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd: int, _lparam: int) -> bool:
            if IsWindowVisible(hwnd):
                buf = ctypes.create_unicode_buffer(256)
                GetWindowText(hwnd, buf, 256)
                title = buf.value.strip()
                if title:
                    rect = wintypes.RECT()
                    GetWindowRect(hwnd, ctypes.byref(rect))
                    cls_buf = ctypes.create_unicode_buffer(128)
                    GetClassName(hwnd, cls_buf, 128)
                    windows.append({
                        "hwnd": hwnd,
                        "title": title[:120],
                        "class": cls_buf.value,
                        "rect": [rect.left, rect.top,
                                 rect.right - rect.left,
                                 rect.bottom - rect.top],
                    })
            return True

        EnumWindows(WNDENUMPROC(_cb), 0)
        return windows

    # ---- actions -------------------------------------------------------

    def actions(self) -> list[ToolAction]:
        return [
            ToolAction(
                "screenshot",
                "Скриншот всего экрана или конкретного окна. Вернёт путь к PNG — "
                "проанализируй инструментом vision (ask_about_image), чтобы "
                "увидеть экран.",
                {"window_title": {"type": "string", "description": "Часть "
                 "заголовка окна (напр. 'Excel', 'QGIS'). Пусто = весь экран",
                 "optional": True},
                 "filename": {"type": "string", "description": "Имя файла PNG",
                              "optional": True}},
            ),
            ToolAction(
                "list_windows",
                "Список видимых окон с заголовками и координатами. Вызывай "
                "перед focus_window, чтобы узнать точный заголовок.",
                {},
            ),
            ToolAction(
                "focus_window",
                "Вывести окно на передний план (по части заголовка). Делай "
                "перед type/hotkey, чтобы ввод попал в нужное приложение.",
                {"window_title": {"type": "string",
                                  "description": "Часть заголовка окна"}},
            ),
            ToolAction(
                "click",
                "Клик мышью по координатам экрана [x, y]. Координаты бери со "
                "скриншота (vision может указать положение элементов).",
                {"x": {"type": "integer", "description": "X на экране"},
                 "y": {"type": "integer", "description": "Y на экране"},
                 "double": {"type": "boolean", "description": "Двойной клик",
                            "optional": True}},
            ),
            ToolAction(
                "type",
                "Напечатать текст в активное окно (сначала focus_window!).",
                {"text": {"type": "string", "description": "Текст для ввода"}},
            ),
            ToolAction(
                "hotkey",
                "Нажать сочетание клавиш, напр. ['ctrl','s'] или ['alt','tab'].",
                {"keys": {"type": "array", "description": "Клавиши сочетания"}},
            ),
            ToolAction(
                "press",
                "Нажать одиночную клавишу: enter, esc, tab, f5 и т.п.",
                {"key": {"type": "string", "description": "Имя клавиши"}},
            ),
            ToolAction(
                "scroll",
                "Прокрутить колёсиком на amount «щёлков» (положительное — вверх).",
                {"amount": {"type": "integer", "description": "Щёлков колеса"}},
            ),
        ]

    def execute(self, action: str, args: dict[str, Any]) -> dict[str, Any]:
        import pyautogui
        pyautogui.FAILSAFE = True  # мышь в угол экрана = аварийный стоп
        pyautogui.PAUSE = float((self.config or {}).get("action_pause_sec", 0.5))

        if action == "screenshot":
            try:
                title = str(args.get("window_title") or "").strip()
                fname = str(args.get("filename") or
                            f"desktop_{int(time.time())}.png")
                fname = Path(fname if fname.endswith(".png") else fname + ".png").name
                path = self._shots_dir() / fname
                if title:
                    target = None
                    for w in self._list_windows():
                        if title.lower() in w["title"].lower():
                            target = w
                            break
                    if not target:
                        return self._result(
                            False, {},
                            f"Окно с '{title}' не найдено. Вызови list_windows.")
                    l, t, w, h = target["rect"]
                    img = pyautogui.screenshot(region=(l, t, w, h))
                else:
                    img = pyautogui.screenshot()
                img.save(str(path))
                return self._result(
                    True, {"screenshot_path": str(path),
                           "size": list(img.size)},
                    f"Скриншот сохранён: {path}. Проанализируй инструментом "
                    f"vision (ask_about_image), чтобы увидеть экран.")
            except Exception as exc:  # noqa: BLE001
                return self._result(False, {}, f"Скриншот не удался: {exc}")

        if action == "list_windows":
            try:
                wins = self._list_windows()
                lines = [f"- {w['title']} [{w['class']}] {w['rect']}"
                         for w in wins[:25]]
                return self._result(
                    True, {"windows": wins},
                    f"Окон видно: {len(wins)}. " + "; ".join(lines[:8]))
            except Exception as exc:  # noqa: BLE001
                return self._result(False, {}, f"Не удалось: {exc}")

        if action == "focus_window":
            try:
                import ctypes
                title = str(args.get("window_title") or "").strip()
                target = None
                for w in self._list_windows():
                    if title.lower() in w["title"].lower():
                        target = w
                        break
                if not target:
                    return self._result(
                        False, {},
                        f"Окно с '{title}' не найдено. Вызови list_windows.")
                user32 = ctypes.windll.user32
                # восстановить, если свёрнуто, и поднять
                user32.ShowWindow(target["hwnd"], 9)  # SW_RESTORE
                user32.SetForegroundWindow(target["hwnd"])
                self._pause()
                return self._result(
                    True, {"focused": target["title"]},
                    f"Окно '{target['title']}' в фокусе. Теперь можно "
                    f"type/hotkey — ввод пойдёт в него.")
            except Exception as exc:  # noqa: BLE001
                return self._result(False, {}, f"Не удалось: {exc}")

        if action == "click":
            try:
                x = int(args.get("x", 0))
                y = int(args.get("y", 0))
                double = bool(args.get("double"))
                if double:
                    pyautogui.doubleClick(x, y)
                else:
                    pyautogui.click(x, y)
                self._pause()
                return self._result(
                    True, {"clicked": [x, y], "double": double},
                    f"{'Двойной ' if double else ''}клик по ({x},{y}) "
                    f"выполнен. Сделай screenshot, чтобы увидеть результат.")
            except Exception as exc:  # noqa: BLE001
                return self._result(False, {}, f"Клик не удался: {exc}")

        if action == "type":
            try:
                text = str(args.get("text") or "")
                # pyautogui.typewrite не умеет кириллицу — используем clip+paste
                if any("\u0400" <= ch <= "\u04FF" for ch in text):
                    import pyperclip
                    pyperclip.copy(text)
                    pyautogui.hotkey("ctrl", "v")
                else:
                    pyautogui.typewrite(text, interval=0.02)
                self._pause()
                return self._result(True, {"typed": len(text)},
                                    f"Введено {len(text)} символов в активное "
                                    f"окно.")
            except Exception as exc:  # noqa: BLE001
                return self._result(False, {}, f"Ввод не удался: {exc}")

        if action == "hotkey":
            try:
                keys = [str(k) for k in (args.get("keys") or [])]
                if not keys:
                    return self._result(False, {}, "Укажи keys.")
                pyautogui.hotkey(*keys)
                self._pause()
                return self._result(True, {"hotkey": keys},
                                    f"Нажато {'+'.join(keys)}.")
            except Exception as exc:  # noqa: BLE001
                return self._result(False, {}, f"Не удалось: {exc}")

        if action == "press":
            try:
                key = str(args.get("key") or "").strip()
                if not key:
                    return self._result(False, {}, "Укажи key.")
                pyautogui.press(key)
                self._pause()
                return self._result(True, {"pressed": key},
                                    f"Нажата клавиша {key}.")
            except Exception as exc:  # noqa: BLE001
                return self._result(False, {}, f"Не удалось: {exc}")

        if action == "scroll":
            try:
                amount = int(args.get("amount", 3))
                pyautogui.scroll(amount)
                self._pause()
                return self._result(True, {"scrolled": amount},
                                    f"Прокрутка {amount}.")
            except Exception as exc:  # noqa: BLE001
                return self._result(False, {}, f"Не удалось: {exc}")

        return self._result(False, {}, f"Неизвестное действие: {action}")

    def health(self) -> tuple[ToolStatus, str]:
        try:
            import pyautogui  # noqa: F401
            size = pyautogui.size()
            return (ToolStatus.READY,
                    f"pyautogui готов, экран {size.width}x{size.height}")
        except Exception as exc:  # noqa: BLE001
            return (ToolStatus.UNAVAILABLE,
                    f"pyautogui недоступен: {exc}. Установи: "
                    "pip install pyautogui pyperclip")
