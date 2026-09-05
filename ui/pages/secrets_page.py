"""
Экран «Ключи доступа» — ввод API-ключей прямо в интерфейсе.

Пользователю больше не нужен терминал и команда setx: ключ вписывается
здесь, сохраняется в data/secrets.json и применяется немедленно.

Список полей строится из реестра config/secrets.py — чтобы добавить новый
ключ, достаточно добавить туда запись, этот файл менять не нужно.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from config.secrets import KNOWN_SECRETS, STORE, secret_status
from ui.state import STATE


def render() -> None:
    """Отрисовать экран. Вызывается из ui/app.py."""
    ui.label("Ключи доступа").classes("text-2xl font-bold mb-1")
    ui.label(
        "Здесь вводятся ключи для облачных сервисов. Без них платформа работает "
        "полностью локально — облако лишь необязательное усиление."
    ).classes("text-sm opacity-70 mb-3")

    container = ui.column().classes("w-full max-w-3xl gap-3")
    inputs: dict[str, Any] = {}

    def refresh() -> None:
        container.clear()
        inputs.clear()
        statuses = secret_status()
        groups: dict[str, list[dict[str, Any]]] = {}
        for item in statuses:
            groups.setdefault(item["group"], []).append(item)

        with container:
            # Сводка сверху: что мешает работать с облаком прямо сейчас
            cloud = STATE.platform.mode_controller.status()
            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("cloud_done" if cloud.allowed else "cloud_off").classes(
                        "text-green-8" if cloud.allowed else "text-orange-8")
                    ui.label("Облако доступно" if cloud.allowed
                             else "Облако сейчас не используется").classes("font-bold")
                ui.label(cloud.reason).classes("text-xs opacity-70")

            for group, items in groups.items():
                with ui.card().classes("w-full"):
                    ui.label(group).classes("font-bold")
                    for item in items:
                        with ui.column().classes("w-full gap-0 mt-2"):
                            with ui.row().classes("items-center gap-2 w-full"):
                                field = ui.input(
                                    label=item["title"],
                                    placeholder=item["placeholder"],
                                    password=True,
                                    password_toggle_button=True,
                                ).classes("grow").props("outlined dense")
                                inputs[item["env"]] = field

                                if item["filled"]:
                                    ui.icon("check_circle").classes("text-green-8")
                                    ui.button(
                                        "Удалить", icon="delete",
                                        on_click=lambda e=item["env"]: remove(e),
                                    ).props("size=sm flat color=negative")
                                else:
                                    ui.icon("radio_button_unchecked").classes("opacity-40")

                            hint = item["description"]
                            if item["filled"]:
                                where = ("задан в системе" if item["source"] == "окружение"
                                         else "сохранён в приложении")
                                hint = f"Сохранён: {item['masked']} ({where}). {hint}"
                            ui.label(hint).classes("text-xs opacity-70")
                            if item["url"]:
                                ui.link("Где получить ключ", item["url"],
                                        new_tab=True).classes("text-xs")
                            if item["source"] == "окружение":
                                ui.label(
                                    "Внимание: этот ключ задан переменной окружения системы — "
                                    "она имеет приоритет над введённым здесь значением."
                                ).classes("text-xs text-orange-9")

            with ui.card().classes("w-full bg-blue-1"):
                ui.label("Где хранятся ключи").classes("font-bold text-sm")
                ui.label(
                    f"Файл: {STORE.path}. Он исключён из системы контроля версий. "
                    "Шифрование не применяется, поэтому не открывайте доступ к этой "
                    "папке другим пользователям компьютера."
                ).classes("text-xs opacity-80")

    def save() -> None:
        """Сохранить только непустые поля (пустое поле = «не менять»)."""
        changed: list[str] = []
        for env, field in inputs.items():
            value = (field.value or "").strip()
            if value:
                STORE.set(env, value)
                changed.append(env)
        if not changed:
            ui.notify("Введите хотя бы один ключ", type="warning")
            return
        # Перечитываем платформу: новый ключ должен подхватиться сразу
        STATE.reload_platform()
        STATE.platform.mode_controller.reset_breaker()
        ui.notify(f"Сохранено ключей: {len(changed)}. Проверяю связь…", type="positive")
        refresh()

    def remove(env: str) -> None:
        STORE.delete(env)
        STATE.reload_platform()
        ui.notify("Ключ удалён", type="info")
        refresh()

    def check() -> None:
        """Живая проверка: реально ли отвечает облако с этим ключом."""
        controller = STATE.platform.mode_controller
        controller.reset_breaker()
        ok = controller.health_check(force=True)
        status = controller.status()
        if status.allowed:
            ui.notify("Облако отвечает, ключ принят", type="positive")
        elif ok:
            ui.notify(f"Связь есть, но облако не используется: {status.reason}",
                      type="warning")
        else:
            ui.notify(f"Проверка не прошла: {status.reason}", type="negative")
        refresh()

    refresh()
    with ui.row().classes("mt-3 gap-2"):
        ui.button("Сохранить", icon="save", on_click=save).props("color=primary")
        ui.button("Проверить связь с облаком", icon="network_check",
                  on_click=check).props("outline")
        ui.button("Обновить", icon="refresh", on_click=refresh).props("flat")

    ui.label(
        "Пустое поле при сохранении означает «оставить как было» — "
        "уже сохранённый ключ не стирается."
    ).classes("text-xs opacity-60 mt-2")


__all__ = ["render", "KNOWN_SECRETS"]
