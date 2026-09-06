# -*- coding: utf-8 -*-
"""i18n: строки панели ru/en + переключатель.

Ключ = русская каноническая строка (исходник). t() возвращает английский
при lang=en. Выбор хранится в data/ui_lang.json. По умолчанию EN
(публичный проект), переключатель — кнопка RU/EN в шапке.
"""
from __future__ import annotations

import json
from pathlib import Path

_LANG_FILE = Path("data/ui_lang.json")

_EN: dict[str, str] = {
    "Журнал пуст": "Log is empty",
    "Измените запрос": "Change the request",
    "Выполните первую задачу — она появится здесь": "Run the first task — it will appear here",
    "Исполнители: ": "Executors: ",
    " — выполните в консоли: ollama pull ": " — run in a console: ollama pull ",
    " — платформа работает без них в simple-режиме; для orchestrated/team скачайте их (см. README, раздел Models)": " — the platform works without them in simple mode; for orchestrated/team download them (see README, Models section)",
    "Lead Agent: планировщик LLM не используется — один сильный ": "Lead Agent: no LLM planner — one strong ",
    "Ollama": "Ollama",
    "Ollama не отвечает — проверьте, запущен ли сервис": "Ollama is not responding — check that the service is running",
    "config/models.yaml найден": "config/models.yaml found",
    "Аварийное выключение": "Emergency shutdown",
    "Аварийное выключение прерывает выполняющуюся задачу и закрывает ": "Emergency shutdown interrupts the running task and closes ",
    "Автоматически переходить в локальный режим при признаках конфиденциальности": "Automatically switch to local mode when confidentiality is suspected",
    "Автоматический": "Automatic",
    "Ансамбль моделей": "Model ensemble",
    "База знаний": "Knowledge base",
    "База пуста. Добавь знания через задачи или ": "The base is empty. Add knowledge via tasks or ",
    "Больше контекст = больше VRAM под KV-кеш. 128K — ": "More context = more VRAM for the KV cache. 128K ",
    "В Lead Agent-режиме эти настройки не применяются: ": "In Lead Agent mode these settings do not apply: ",
    "Ввести ключи доступа": "Enter API keys",
    "Все выполненные задачи с результатами и планами": "All completed tasks with results and plans",
    "Всё готово — можно работать.": "All set — you can start working.",
    "Вызовы инструментов": "Tool calls",
    "Выключенный узел исключается из графа целиком — полезно для отладки ": "A disabled node is excluded from the graph entirely — useful for debugging ",
    "Выполните шаги выше и откройте этот экран снова (кнопка «?» в шапке).": "Complete the steps above and reopen this screen (the “?” button in the header).",
    "Главное требование к планировщику — стабильный JSON и перенос ": "The planner's main requirement is stable JSON and carrying ",
    "Готовых инструментов нет — откройте экран «Инструменты», ": "No tools available — open the “Tools” screen, ",
    "Детали задачи": "Task details",
    "Добавить": "Add",
    "Добро пожаловать в LocAlis": "Welcome to LocAlis",
    "Журнал изменений": "Changelog",
    "Завершено с ошибкой": "Completed with error",
    "Задача выполнена": "Task completed",
    "Задача ещё не запускалась": "No task has been started yet",
    "Задача не найдена": "Task not found",
    "Задача поставлена в очередь": "Task queued",
    "Запустить задачу": "Run task",
    "Защита от сбоев облака": "Cloud failure guard",
    "Измените запрос": "Change the request",
    "Инструменты": "Tools",
    "Инструменты пока не вызывались": "No tools have been called yet",
    "Исполнители: ": "Executors: ",
    "Использование инструментов": "Tool usage",
    "История задач": "History",
    "Итоговый ответ": "Final answer",
    "К истории": "Back to history",
    "Каждый шаг — отдельный вызов модели. На 8 ГБ видеопамяти больше ": "Each step is a separate model call. On 8 GB VRAM more than ",
    "Как выполнять": "How to run",
    "Ключ облака вводится на экране «Ключи доступа» или переменной ": "The cloud key is entered on the “API keys” screen or via an ",
    "Ключи доступа": "API keys",
    "Конфигурация моделей": "Model configuration",
    "Локальная мультиагентная платформа: модели обсуждают план, исполняют его инструментами и проверяют друг друга — без облака.": "A local-first multi-agent platform: models debate the plan, execute it with tools, and verify each other — no cloud.",
    "Локальные модели (Ollama)": "Local models (Ollama)",
    "Модели по ролям": "Models by role",
    "НОВАЯ ЗАДАЧА": "NEW TASK",
    "Настройки": "Settings",
    "Начать": "Start",
    "Ничего не найдено": "Nothing found",
    "Ничего не найдено.": "Nothing found.",
    "Новая задача": "New task",
    "О платформе / setup": "About / setup",
    "ОТКЛЮЧИТЬ ОБЛАКО": "DISABLE CLOUD",
    "Обновить": "Refresh",
    "Обслуживание": "Maintenance",
    "Обязательные модели": "Required models",
    "Остановить задачу": "Stop task",
    "Отключить облако": "Disable cloud",
    "Откройте «Новая задача», опишите, что нужно сделать, ": "Open “New task”, describe what to do, ",
    "Открыть папку config": "Open config folder",
    "Панель управления": "Dashboard",
    "Перезагрузить компоненты": "Reload components",
    "Перезагрузка компонентов применяет изменения конфигов, моделей и ": "Component reload applies config, model and ",
    "Перезапустить панель (полный рестарт)": "Restart panel (full restart)",
    "Перечитать плагины": "Rescan plugins",
    "Плагины не найдены в папке tools/": "No plugins found in tools/",
    "План оркестратора": "Orchestrator plan",
    "Планировщик делит задачу на шаги и поручает их малым локальным ": "The planner splits the task into steps and assigns them to small local ",
    "Подробно": "Details",
    "Поиск по тексту задачи": "Search task text",
    "Проверить снова": "Check again",
    "Пропустить и не показывать больше": "Skip and don't show again",
    "Режим «Оркестратор»": "Orchestrator mode",
    "Режим работы": "Mode",
    "Режим: Lead Agent — задачу выполняет один сильный ": "Mode: Lead Agent — one strong executor ",
    "Результаты шагов": "Step results",
    "Роли и модели": "Roles & models",
    "СБРОСИТЬ": "RESET",
    "Сбросить": "Reset",
    "События появятся через несколько секунд...": "Events will appear in a few seconds...",
    "Совещание: все модели предлагают планы, видят планы ": "Meeting: all models propose plans, see each other's plans ",
    "Состояние платформы: режим, облако, модели, инструменты, задачи": "Platform state: mode, cloud, models, tools, tasks",
    "Сохранить настройки": "Save settings",
    "Список моделей задаётся в config/models.yaml. Добавление модели ": "The model list lives in config/models.yaml. Adding a model ",
    "Сравнение исполнителей": "Executor comparison",
    "Строго локальный": "Strictly local",
    "Требования: ": "Requirements: ",
    "Удалить": "Delete",
    "Узлы графа": "Graph nodes",
    "Файл CHANGELOG.md не найден": "CHANGELOG.md not found",
    "Ход выполнения": "Run",
    "Что нужно сделать": "What to do",
    "Что умеет": "Capabilities",
    "Шаги всегда выполняют локальные модели: содержимое ": "Steps always run local models: file contents ",
    "все модели ансамбля доступны (ollama + llama-server)": "all ensemble models are available (ollama + llama-server)",
    "выполнено": "done",
    "доступен, моделей:": "available, models:",
    "замкнут — норма": "closed — normal",
    "локально + облако": "local + cloud",
    "локальный режим": "local mode",
    "не отвечает на http://127.0.0.1:11434 — установите с ollama.com и нажмите «Проверить снова»": "not responding on http://127.0.0.1:11434 — install from ollama.com and press “Check again”",
    "не хватает:": "missing:",
    "нет config/models.yaml (скопируйте из models.example.yaml)": "no config/models.yaml (copy it from models.example.yaml)",
    "нет config/models.yaml и models.example.yaml": "no config/models.yaml and no models.example.yaml",
    "облако доступно": "cloud available",
    "облако отключено": "cloud off",
    "ошибка": "error",
    "по умолчанию: автоматический": "default: automatic",
    "сбоев подряд:": "failures in a row:",
    "состояние неизвестно": "state unknown",
    "успешно": "ok",
}


def _lang() -> str:
    """Текущий язык панели. По умолчанию EN."""
    try:
        return json.loads(_LANG_FILE.read_text(encoding="utf-8")).get("lang", "en")
    except Exception:
        return "en"


def set_lang(lang: str) -> None:
    _LANG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LANG_FILE.write_text(json.dumps({"lang": lang}), encoding="utf-8")


def t(ru: str) -> str:
    """Перевод строки панели: en -> _EN, ru -> как есть."""
    if _lang() == "en":
        return _EN.get(ru, ru)
    return ru
