# -*- coding: utf-8 -*-
"""Welcome-экран + setup-мастер первого запуска.

Показывается один раз — пока пользователь не нажал «Начать».
Флаг хранится в data/welcome_state.json (не в БД: это UI-состояние,
а не данные задач). Кнопка «?» в шапке открывает его повторно.

Экраны:
  0. Ollama      — доступен ли :11434
  1. Модели      — обязательные (qwen2.5:3b, bge-m3) скачаны?
  2. Конфиг      — models.yaml существует?
  3. Готово      — кнопка «Начать»
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from nicegui import ui

from ui.state import STATE

_WELCOME_FILE = Path("data/welcome_state.json")

# --- состояние -----------------------------------------------------------


def _load() -> dict:
    try:
        return json.loads(_WELCOME_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(state: dict) -> None:
    _WELCOME_FILE.parent.mkdir(parents=True, exist_ok=True)
    _WELCOME_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1),
                             encoding="utf-8")


def is_first_run() -> bool:
    """True, пока пользователь не прошёл welcome."""
    return not _load().get("seen")


def open_about() -> None:
    """Кнопка «?» — показать welcome поверх любого экрана."""
    with ui.dialog() as dlg, ui.card().classes("w-[720px] max-w-full"):
        _render_welcome(dlg)
    dlg.open()


# --- проверки окружения (детерминированные, без LLM) ---------------------


def _check_ollama() -> tuple[bool, str]:
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            data = json.loads(r.read())
            names = [m.get("name", "") for m in data.get("models", [])]
            return True, f"доступен, моделей: {len(names)}"
    except Exception as exc:  # noqa: BLE001
        return False, f"недоступен ({exc.__class__.__name__})"


def _check_models() -> tuple[bool, list[str]]:
    """Возвращает (все_обязательные_есть, список_отсутствующих)."""
    required = ["qwen2.5:3b-instruct", "bge-m3"]
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=3) as r:
            data = json.loads(r.read())
            names = {m.get("name", "") for m in data.get("models", [])}
    except Exception:
        return False, required
    missing = [m for m in required
               if not any(n == m or n.split(":")[0] == m.split(":")[0] for n in names)]
    return not missing, missing


def _check_config() -> tuple[bool, str]:
    cfg = Path("config/models.yaml")
    if cfg.exists():
        return True, "config/models.yaml найден"
    example = Path("config/models.example.yaml")
    if example.exists():
        return False, "нет config/models.yaml (скопируйте из models.example.yaml)"
    return False, "нет config/models.yaml и models.example.yaml"


# --- рендер ---------------------------------------------------------------


def _step_card(number: int, title: str, ok: bool, detail: str,
               action_label: str = "", action_fn=None) -> None:
    """Одна карточка шага: номер, статус, деталь, кнопка действия."""
    color = "text-green-500" if ok else ("text-orange-400" if detail else "text-red-400")
    icon = "check_circle" if ok else "error"
    with ui.row().classes("w-full items-start gap-3 ap-card p-3"):
        ui.label(str(number)).classes(
            "ap-mono text-lg w-8 h-8 shrink-0 flex items-center justify-center "
            "rounded-full bg-secondary")
        with ui.column().classes("grow gap-1"):
            with ui.row().classes("items-center gap-2"):
                ui.icon(icon, name=None).classes(f"{color}")
                ui.label(title).classes("font-medium")
            ui.label(detail).classes("ap-muted text-sm")
            if action_label and not ok and action_fn:
                ui.button(action_label, on_click=action_fn).props(
                    "outline dense size=sm")


def _render_welcome(dlg) -> None:
    STATE.platform  # инициализация ленивых компонентов до проверок
    ollama_ok, ollama_detail = _check_ollama()
    models_ok, missing = _check_models()
    cfg_ok, cfg_detail = _check_config()

    all_ok = ollama_ok and models_ok and cfg_ok

    ui.label("Добро пожаловать в LocAlis").classes("text-2xl font-semibold")
    ui.label(
        "Локальная мультиагентная платформа: модели обсуждают план, "
        "исполняют его инструментами и проверяют друг друга — без облака."
    ).classes("ap-muted")

    ui.separator()

    # --- шаг 0: Ollama ---
    _step_card(0, "Ollama", ollama_ok, ollama_detail if ollama_ok else
               "не отвечает на http://127.0.0.1:11434 — "
               "установите с ollama.com и нажмите «Проверить снова»",
               "Проверить снова",
               lambda: (dlg.close(), open_about()))

    # --- шаг 1: модели ---
    if models_ok:
        _step_card(1, "Обязательные модели", True,
                   "qwen2.5:3b-instruct (роутер), bge-m3 (эмбеддер) — на месте")
    else:
        _step_card(
            1, "Обязательные модели", False,
            "не хватает: " + ", ".join(missing) +
            " — выполните в консоли: ollama pull " + " && ollama pull ".join(missing),
            "Проверить снова", lambda: (dlg.close(), open_about()))

    # --- шаг 2: конфиг ---
    _step_card(2, "Конфигурация моделей", cfg_ok, cfg_detail,
               "Открыть папку config",
               lambda: os.startfile(str(Path("config").resolve()))  # noqa: S606
               if hasattr(os, "startfile") else None)

    ui.separator()

    # --- шаг 3: готово / старт ---
    if all_ok:
        ui.label("Всё готово — можно работать.").classes("text-green-500")

        def _start() -> None:
            st = _load()
            st["seen"] = True
            _save(st)
            dlg.close()
            ui.navigate.to("/new")

        ui.button("Начать", on_click=_start).props("unelevated")
    else:
        ui.label("Выполните шаги выше и откройте этот экран снова "
                 "(кнопка «?» в шапке).").classes("ap-muted")
        ui.button("Проверить снова",
                  on_click=lambda: (dlg.close(), open_about())).props("outline")

    # подпись внизу
    with ui.row().classes("w-full justify-end"):
        ui.link("Пропустить и не показывать больше",
                "/new").on(lambda: _save({**_load(), "seen": True}))


import os  # noqa: E402  (os.startfile — только Windows)
