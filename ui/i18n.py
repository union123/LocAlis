# -*- coding: utf-8 -*-
"""i18n: строки панели на двух языках + переключатель.

Ключ = английская каноническая строка. t(ru_string) смотрит текущий язык:
ru -> возвращает как есть (русский — исходник), en -> перевод из _EN.

Хранение выбора: data/ui_lang.json. Переключатель — в шапке.
"""
from __future__ import annotations

import json
from pathlib import Path

_LANG_FILE = Path("data/ui_lang.json")

_EN: dict[str, str] = {
    # навигация
    "Панель управления": "Dashboard",
    "Новая задача": "New task",
    "Ход выполнения": "Run",
    "История задач": "History",
    "База знаний": "Knowledge base",
    "Инструменты": "Tools",
    "Ключи доступа": "API keys",
    "Настройки": "Settings",
    "Журнал изменений": "Changelog",
    # шапка
    "локальный режим": "local mode",
    "локально + облако": "local + cloud",
    "облако доступно": "cloud available",
    "облако отключено": "cloud off",
    "состояние неизвестно": "state unknown",
    # dashboard
    "Состояние платформы: режим, облако, модели, инструменты, задачи":
        "Platform state: mode, cloud, models, tools, tasks",
    "Режим работы": "Mode",
    "по умолчанию: автоматический": "default: automatic",
    "Защита от сбоев облачных моделей": "Cloud-model failure guard",
    "замкнут — норма": "closed — normal",
    "сбоев подряд:": "failures in a row:",
    "СБРОСИТЬ": "RESET",
    "ОТКЛЮЧИТЬ ОБЛАКО": "DISABLE CLOUD",
    "Локальные модели (Ollama и llama-server)": "Local models (Ollama & llama-server)",
    "готовы к работе": "ready",
    "Инструменты": "Tools",
    "готовы к работе": "ready to work",
    "успешно": "ok",
}


def _lang() -> str:
    try:
        return json.loads(_LANG_FILE.read_text(encoding="utf-8")).get("lang", "ru")
    except Exception:
        return "ru"


def set_lang(lang: str) -> None:
    _LANG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LANG_FILE.write_text(json.dumps({"lang": lang}), encoding="utf-8")


def t(ru: str) -> str:
    """Перевод строки панели. ru — исходник, en — из словаря."""
    if _lang() == "en":
        return _EN.get(ru, ru)
    return ru
