"""
Единый набор линейных SVG-иконок LocAlis — замена стоковых Material Symbols.

Идея стиля: тонкая геометрическая линия (1.6px, скруглённые концы), как в
инструментах геодезиста и топографических картах, плюс мотив "узлов и связей"
из логотипа платформы (три proposer-модели + голосование 2 из 3) — он
намеренно повторяется в нескольких иконках (groups/science/node), чтобы это
читалось как фирменный знак, а не случайное совпадение.

Каждая запись в _ICONS — это только внутренняя разметка (<path>/<circle>/...),
без внешнего <svg>: обёртку и общие атрибуты (stroke, viewBox и т.д.)
добавляет icon(). Заливка currentColor используется только для акцентных
точек — так иконка наследует и цвет, и размер от .classes() на вызывающей
стороне (text-primary, text-lg, text-4xl и т.п.), как раньше работал
ui.icon() на шрифте Material Symbols.
"""

from __future__ import annotations

from nicegui import ui

_ICONS: dict[str, str] = {
    # --- навигация в левом меню -------------------------------------
    "space_dashboard": (
        '<rect x="2.5" y="2.5" width="15" height="15" rx="2"/>'
        '<line x1="10.5" y1="2.5" x2="10.5" y2="17.5"/>'
        '<line x1="10.5" y1="10.5" x2="17.5" y2="10.5"/>'
    ),
    "add_circle": (
        '<path d="M10 2.3 L17 6.4 V13.6 L10 17.7 L3 13.6 V6.4 Z"/>'
        '<line x1="10" y1="7" x2="10" y2="13"/>'
        '<line x1="7" y1="10" x2="13" y2="10"/>'
    ),
    "timeline": (
        '<polyline points="3,16 8,10.5 12.5,12.5 17,4.5"/>'
        '<circle cx="3" cy="16" r="1.1" fill="currentColor" stroke="none"/>'
        '<circle cx="8" cy="10.5" r="1.1" fill="currentColor" stroke="none"/>'
        '<circle cx="12.5" cy="12.5" r="1.1" fill="currentColor" stroke="none"/>'
        '<circle cx="17" cy="4.5" r="1.1" fill="currentColor" stroke="none"/>'
    ),
    "history": (
        '<circle cx="10" cy="10.5" r="7"/>'
        '<polyline points="10,6.3 10,10.5 13.2,12.4"/>'
        '<path d="M4.3 5.7 L3.3 8.6 L6.1 7.5"/>'
    ),
    "extension": (
        '<path d="M8 3v3.4M12 3v3.4M6 6.4h8v3.1a4 4 0 0 1-8 0Z"/>'
        '<line x1="10" y1="13.5" x2="10" y2="17.3"/>'
    ),
    "extension_off": (
        '<path d="M8 3v3.4M12 3v3.4M6 6.4h8v3.1a4 4 0 0 1-8 0Z"/>'
        '<line x1="10" y1="13.5" x2="10" y2="17.3"/>'
        '<line x1="3" y1="17" x2="17" y2="3"/>'
    ),
    "vpn_key": (
        '<circle cx="6.3" cy="7.3" r="3"/>'
        '<line x1="8.5" y1="9.5" x2="16.5" y2="17.5"/>'
        '<line x1="13" y1="14" x2="14.7" y2="12.3"/>'
        '<line x1="15" y1="16" x2="16.7" y2="14.3"/>'
    ),
    "tune": (
        '<line x1="3" y1="5.5" x2="17" y2="5.5"/>'
        '<circle cx="8" cy="5.5" r="1.6" fill="currentColor" stroke="none"/>'
        '<line x1="3" y1="10" x2="17" y2="10"/>'
        '<circle cx="13" cy="10" r="1.6" fill="currentColor" stroke="none"/>'
        '<line x1="3" y1="14.5" x2="17" y2="14.5"/>'
        '<circle cx="10" cy="14.5" r="1.6" fill="currentColor" stroke="none"/>'
    ),
    "receipt_long": (
        '<path d="M5 2.5h10v15l-1.6-1.1-1.6 1.1-1.6-1.1-1.6 1.1-1.6-1.1-1.6 1.1z"/>'
        '<line x1="7.3" y1="6.5" x2="12.7" y2="6.5"/>'
        '<line x1="7.3" y1="9.5" x2="12.7" y2="9.5"/>'
        '<line x1="7.3" y1="12.5" x2="10.5" y2="12.5"/>'
    ),
    # --- статусы шагов/задач/инструментов ----------------------------
    "schedule": '<circle cx="10" cy="10" r="7.2"/><polyline points="10,6 10,10.3 13.2,12.2"/>',
    "autorenew": (
        '<path d="M15.5 8.5A5.5 5.5 0 0 0 5.4 6.4"/>'
        '<polyline points="5.1,3.2 5.4,6.4 8.6,6.1"/>'
        '<path d="M4.5 11.5A5.5 5.5 0 0 0 14.6 13.6"/>'
        '<polyline points="14.9,16.8 14.6,13.6 11.4,13.9"/>'
    ),
    "check_circle": '<circle cx="10" cy="10" r="7.2"/><polyline points="6.7,10.3 9,12.8 13.6,7.3"/>',
    "error": (
        '<circle cx="10" cy="10" r="7.2"/>'
        '<line x1="10" y1="6.3" x2="10" y2="11.3"/>'
        '<circle cx="10" cy="14" r="0.95" fill="currentColor" stroke="none"/>'
    ),
    "cancel": (
        '<circle cx="10" cy="10" r="7.2"/>'
        '<line x1="7.3" y1="7.3" x2="12.7" y2="12.7"/>'
        '<line x1="12.7" y1="7.3" x2="7.3" y2="12.7"/>'
    ),
    "task_alt": (
        '<path d="M10 2.6 L16.5 6.5 V13.5 L10 17.4 L3.5 13.5 V6.5 Z"/>'
        '<polyline points="6.8,10.2 9,12.6 13.4,7.4"/>'
    ),
    "warning": (
        '<path d="M10 3 L18 16.5 H2 Z"/>'
        '<line x1="10" y1="7.8" x2="10" y2="12"/>'
        '<circle cx="10" cy="14.3" r="0.9" fill="currentColor" stroke="none"/>'
    ),
    "toggle_off": (
        '<rect x="2.5" y="7" width="15" height="6" rx="3"/>'
        '<circle cx="6.5" cy="10" r="2.1" fill="currentColor" stroke="none"/>'
    ),
    "help": (
        '<circle cx="10" cy="10" r="7.2"/>'
        '<path d="M7.8 8.1a2.2 2.2 0 1 1 3.4 1.85c-.7.45-1.2.85-1.2 1.85"/>'
        '<circle cx="10" cy="14.1" r="0.95" fill="currentColor" stroke="none"/>'
    ),
    "help_outline": (
        '<circle cx="10" cy="10" r="7.2"/>'
        '<path d="M7.8 8.1a2.2 2.2 0 1 1 3.4 1.85c-.7.45-1.2.85-1.2 1.85"/>'
        '<circle cx="10" cy="14.1" r="0.95" fill="currentColor" stroke="none"/>'
    ),
    # --- узловой мотив (совпадает с логотипом) -----------------------
    "smart_toy": (
        '<circle cx="6.5" cy="7" r="2.1"/>'
        '<circle cx="14" cy="13.5" r="2.1"/>'
        '<line x1="8.3" y1="8.5" x2="12.2" y2="12"/>'
    ),
    "science": (
        '<circle cx="10" cy="4.3" r="1.7"/>'
        '<circle cx="4.3" cy="15" r="1.7"/>'
        '<circle cx="15.7" cy="15" r="1.7"/>'
        '<line x1="10" y1="6" x2="10" y2="11"/>'
        '<line x1="5.6" y1="13.4" x2="8.6" y2="11.4"/>'
        '<line x1="14.4" y1="13.4" x2="11.4" y2="11.4"/>'
        '<circle cx="10" cy="11.6" r="1" fill="currentColor" stroke="none"/>'
    ),
    "groups": (
        '<circle cx="10" cy="4.3" r="1.7"/>'
        '<circle cx="4.3" cy="15" r="1.7"/>'
        '<circle cx="15.7" cy="15" r="1.7"/>'
        '<line x1="10" y1="6" x2="10" y2="11"/>'
        '<line x1="5.6" y1="13.4" x2="8.6" y2="11.4"/>'
        '<line x1="14.4" y1="13.4" x2="11.4" y2="11.4"/>'
        '<circle cx="10" cy="11.6" r="1" fill="currentColor" stroke="none"/>'
    ),
    "account_tree": (
        '<rect x="7.5" y="2.5" width="5" height="4" rx="1"/>'
        '<rect x="2.5" y="13.5" width="5" height="4" rx="1"/>'
        '<rect x="12.5" y="13.5" width="5" height="4" rx="1"/>'
        '<line x1="10" y1="6.5" x2="10" y2="10"/>'
        '<line x1="5" y1="13.5" x2="5" y2="10"/>'
        '<line x1="15" y1="13.5" x2="15" y2="10"/>'
        '<line x1="5" y1="10" x2="15" y2="10"/>'
    ),
    "node": (
        '<circle cx="10" cy="4.5" r="1.6"/>'
        '<circle cx="4.5" cy="14.5" r="1.6"/>'
        '<circle cx="15.5" cy="14.5" r="1.6"/>'
        '<line x1="10" y1="6.1" x2="10" y2="11"/>'
        '<line x1="10" y1="11" x2="4.5" y2="12.9"/>'
        '<line x1="10" y1="11" x2="15.5" y2="12.9"/>'
    ),
    # --- прочее --------------------------------------------------------
    "memory": (
        '<rect x="5" y="5" width="10" height="10" rx="1.3"/>'
        '<line x1="7.5" y1="2.5" x2="7.5" y2="5"/>'
        '<line x1="12.5" y1="2.5" x2="12.5" y2="5"/>'
        '<line x1="7.5" y1="15" x2="7.5" y2="17.5"/>'
        '<line x1="12.5" y1="15" x2="12.5" y2="17.5"/>'
        '<line x1="2.5" y1="7.5" x2="5" y2="7.5"/>'
        '<line x1="2.5" y1="12.5" x2="5" y2="12.5"/>'
        '<line x1="15" y1="7.5" x2="17.5" y2="7.5"/>'
        '<line x1="15" y1="12.5" x2="17.5" y2="12.5"/>'
    ),
    "insights": (
        '<line x1="4" y1="16" x2="4" y2="11"/>'
        '<line x1="9" y1="16" x2="9" y2="8"/>'
        '<line x1="14" y1="16" x2="14" y2="5"/>'
        '<polyline points="3.3,10 8,6 13,4 17,2.2"/>'
        '<polyline points="14,2.2 17,2.2 17,5.2"/>'
    ),
    "info": (
        '<circle cx="10" cy="10" r="7.2"/>'
        '<circle cx="10" cy="6.6" r="0.95" fill="currentColor" stroke="none"/>'
        '<line x1="10" y1="9.3" x2="10" y2="13.8"/>'
    ),
    "inbox": (
        '<path d="M3 8 L6.6 8 L8.6 10.6 L11.4 10.6 L13.4 8 L17 8 L17 16 L3 16 Z"/>'
        '<line x1="3" y1="8" x2="4.4" y2="3.3"/>'
        '<line x1="17" y1="8" x2="15.6" y2="3.3"/>'
        '<line x1="4.4" y1="3.3" x2="15.6" y2="3.3"/>'
    ),
    "hourglass_empty": (
        '<path d="M5 3h10M5 17h10M6 3c0 4 2.6 5.5 4 7 -1.4 1.5-4 3-4 7'
        'M14 3c0 4-2.6 5.5-4 7 1.4 1.5 4 3 4 7"/>'
    ),
    "electric_bolt": '<polygon points="11.2,2.5 4.5,11.5 9,11.5 8.2,17.5 15.5,8 10.8,8"/>',
    "compare_arrows": (
        '<line x1="3" y1="7" x2="15" y2="7"/>'
        '<polyline points="12,4 15,7 12,10"/>'
        '<line x1="17" y1="13" x2="5" y2="13"/>'
        '<polyline points="8,10 5,13 8,16"/>'
    ),
    "lock": (
        '<rect x="4.5" y="9" width="11" height="8.5" rx="1.5"/>'
        '<path d="M6.7 9V6.3a3.3 3.3 0 0 1 6.6 0V9"/>'
        '<circle cx="10" cy="12.8" r="1" fill="currentColor" stroke="none"/>'
    ),
    "assignment": (
        '<rect x="4.5" y="3.5" width="11" height="14" rx="1.3"/>'
        '<rect x="7.3" y="2.3" width="5.4" height="2.4" rx="0.8"/>'
        '<line x1="7" y1="8" x2="13" y2="8"/>'
        '<line x1="7" y1="11" x2="13" y2="11"/>'
        '<line x1="7" y1="14" x2="10.5" y2="14"/>'
    ),
    "public": (
        '<circle cx="10" cy="10" r="7.2"/>'
        '<ellipse cx="10" cy="10" rx="3.1" ry="7.2"/>'
        '<line x1="2.8" y1="10" x2="17.2" y2="10"/>'
    ),
    "shield_moon": (
        '<path d="M10 2.6 L16.5 5 V10c0 4.3-2.7 6.7-6.5 8-3.8-1.3-6.5-3.7-6.5-8V5Z"/>'
        '<path d="M12.3 8.3a3 3 0 1 1-3.6-3.9 3.6 3.6 0 0 0 3.6 3.9Z" fill="currentColor" stroke="none"/>'
    ),
    "cloud_done": (
        '<path d="M6.5 15.5a3.5 3.5 0 0 1-.6-6.95A4.5 4.5 0 0 1 14.3 7.2 3.8 3.8 0 0 1 13.8 15.5Z"/>'
        '<polyline points="7.7,11.7 9.4,13.4 12.8,9.6"/>'
    ),
    "cloud_off": (
        '<path d="M6.5 15.5a3.5 3.5 0 0 1-.6-6.95A4.5 4.5 0 0 1 14.3 7.2 3.8 3.8 0 0 1 13.8 15.5Z"/>'
        '<line x1="4" y1="4" x2="16" y2="16"/>'
    ),
    "cloud": '<path d="M6.5 15.5a3.5 3.5 0 0 1-.6-6.95A4.5 4.5 0 0 1 14.3 7.2 3.8 3.8 0 0 1 13.8 15.5Z"/>',
    "radio_button_unchecked": '<circle cx="10" cy="10" r="6.5"/>',
}

_FALLBACK = '<circle cx="10" cy="10" r="2" fill="currentColor" stroke="none"/>'


def icon_svg(name: str) -> str:
    """
    Вернуть готовую HTML-разметку иконки (строкой) — для вставки внутрь
    других f-строк ui.html(...), например ap-chip в шапке, где нельзя
    вызвать icon() как отдельный элемент NiceGUI.
    """
    inner = _ICONS.get(name, _FALLBACK)
    return (
        '<span class="ap-icon"><svg viewBox="0 0 20 20" fill="none" '
        'stroke="currentColor" stroke-width="1.6" stroke-linecap="round" '
        f'stroke-linejoin="round">{inner}</svg></span>'
    )


def icon(name: str, cls: str = "") -> ui.html:
    """
    Инлайн SVG-иконка вместо ui.icon() на шрифте Material Symbols.

    Размер и цвет по-прежнему берутся из classes() на вызывающей стороне
    (text-lg, text-4xl, text-primary и т.п.) — svg отрисован в 1em x 1em
    и красится через currentColor, поэтому существующие места вызова можно
    было просто переименовать ui.icon(...) -> icon(...), не трогая classes().
    """
    element = ui.html(icon_svg(name))
    if cls:
        element.classes(cls)
    return element
