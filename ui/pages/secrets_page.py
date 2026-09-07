"""
API keys screen — enter cloud API keys right in the UI.

Fields are built from the config/secrets.py registry.
"""

from __future__ import annotations

from typing import Any

from nicegui import ui

from config.secrets import KNOWN_SECRETS, STORE, secret_status
from ui.state import STATE
from ui import i18n


def render() -> None:
    """Render the screen. Called from ui/app.py."""
    ui.label(i18n.t("Ключи доступа")).classes("text-2xl font-bold mb-1")
    ui.label(i18n.t(
        "Здесь вводятся ключи для облачных сервисов. Без них платформа работает "
        "полностью локально — облако лишь необязательное усиление."
    )).classes("text-sm opacity-70 mb-3")

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
            # Summary card: what currently blocks cloud usage
            cloud = STATE.platform.mode_controller.status()
            with ui.card().classes("w-full"):
                with ui.row().classes("items-center gap-2"):
                    ui.icon("cloud_done" if cloud.allowed else "cloud_off").classes(
                        "text-green-8" if cloud.allowed else "text-orange-8")
                    ui.label(i18n.t("Облако доступно") if cloud.allowed
                             else i18n.t("Облако сейчас не используется")).classes("font-bold")
                reason = cloud.reason
                if i18n._lang() == "en":
                    reason = i18n.t(reason) if i18n.t(reason) != reason else _reason_en(reason)
                ui.label(reason).classes("text-xs opacity-70")

            for group, items in groups.items():
                with ui.card().classes("w-full"):
                    ui.label(i18n.t(group)).classes("font-bold")
                    for item in items:
                        with ui.column().classes("w-full gap-0 mt-2"):
                            with ui.row().classes("items-center gap-2 w-full"):
                                field = ui.input(
                                    label=i18n.t(item["title"]),
                                    placeholder=item["placeholder"],
                                    password=True,
                                    password_toggle_button=True,
                                ).classes("grow").props("outlined dense")
                                inputs[item["env"]] = field

                                if item["filled"]:
                                    ui.icon("check_circle").classes("text-green-8")
                                    ui.button(
                                        i18n.t("Удалить"), icon="delete",
                                        on_click=lambda e=item["env"]: remove(e),
                                    ).props("size=sm flat color=negative")
                                else:
                                    ui.icon("radio_button_unchecked").classes("opacity-40")

                            hint = i18n.t(item["description"])
                            if item["filled"]:
                                where = (i18n.t("задан в системе") if item["source"] == "окружение"
                                         else i18n.t("сохранён в приложении"))
                                hint = (i18n.t("Сохранён: ") + item["masked"]
                                        + f" ({where}). " + hint)
                            ui.label(hint).classes("text-xs opacity-70")
                            if item["url"]:
                                ui.link(i18n.t("Где получить ключ"), item["url"],
                                        new_tab=True).classes("text-xs")
                            if item["source"] == "окружение":
                                ui.label(i18n.t(
                                    "Внимание: этот ключ задан переменной окружения системы — "
                                    "она имеет приоритет над введённым здесь значением."
                                )).classes("text-xs text-orange-9")

            with ui.card().classes("w-full bg-blue-1"):
                ui.label(i18n.t("Где хранятся ключи")).classes("font-bold text-sm")
                ui.label(
                    i18n.t("Файл: ") + str(STORE.path) + i18n.t(
                        ". Он исключён из системы контроля версий. "
                        "Шифрование не применяется, поэтому не открывайте доступ к этой "
                        "папке другим пользователям компьютера.")
                ).classes("text-xs opacity-80")

    def save() -> None:
        """Save only non-empty fields (empty = keep current)."""
        changed: list[str] = []
        for env, field in inputs.items():
            value = (field.value or "").strip()
            if value:
                STORE.set(env, value)
                changed.append(env)
        if not changed:
            ui.notify(i18n.t("Введите хотя бы один ключ"), type="warning")
            return
        STATE.reload_platform()
        STATE.platform.mode_controller.reset_breaker()
        ui.notify(i18n.t("Сохранено ключей: ") + str(len(changed)) + i18n.t(". Проверяю связь…"),
                  type="positive")
        refresh()

    def remove(env: str) -> None:
        STORE.delete(env)
        STATE.reload_platform()
        ui.notify(i18n.t("Ключ удалён"), type="info")
        refresh()

    def check() -> None:
        """Live check: does the cloud actually respond with this key."""
        controller = STATE.platform.mode_controller
        controller.reset_breaker()
        ok = controller.health_check(force=True)
        status = controller.status()
        if status.allowed:
            ui.notify(i18n.t("Облако отвечает, ключ принят"), type="positive")
        elif ok:
            ui.notify(i18n.t("Связь есть, но облако не используется: ") + _reason_en(status.reason),
                      type="warning")
        else:
            ui.notify(i18n.t("Проверка не прошла: ") + _reason_en(status.reason), type="negative")
        refresh()

    refresh()
    with ui.row().classes("mt-3 gap-2"):
        ui.button(i18n.t("Сохранить"), icon="save", on_click=save).props("color=primary")
        ui.button(i18n.t("Проверить связь с облаком"), icon="network_check",
                  on_click=check).props("outline")
        ui.button(i18n.t("Обновить"), icon="refresh", on_click=refresh).props("flat")

    ui.label(i18n.t(
        "Пустое поле при сохранении означает «оставить как было» — "
        "уже сохранённый ключ не стирается."
    )).classes("text-xs opacity-60 mt-2")


_REASON_EN = {
    "ключ не задан": "key not set",
    "ключ отключён пользователем": "key disabled by the user",
    "сбои облака: разомкнута защита": "cloud failures: breaker open",
    "проверка связи с облаком не прошла: ключ отклонён сервисом (проверьте правильность)":
        "cloud check failed: key rejected by the service (check it)",
    "облако выключено в настройках": "cloud disabled in settings",
}


def _reason_en(reason: str) -> str:
    return _REASON_EN.get(reason.strip(), reason)


__all__ = ["render", "KNOWN_SECRETS"]
