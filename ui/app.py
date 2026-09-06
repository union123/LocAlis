"""
Control Panel (слой 9) — локальное desktop-приложение, интерфейс на русском.

Экраны:
  «Панель управления», «Новая задача», «Ход выполнения», «История задач»,
  «Инструменты», «Ключи доступа», «Настройки», «Журнал изменений».

Добавление экрана: новый файл в ui/pages/ с функцией render() и регистрацией
в PAGES — код приложения при этом не меняется.

Оформление задаётся в одном месте (THEME + _CSS): чтобы поменять вид всей
панели, правится тема, а не каждый экран.

Запуск: python main.py --ui   или   scripts/start.bat
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from nicegui import ui

from ui.icons import icon as _icon, icon_svg as _icon_svg
from ui.state import STATE
from ui import welcome

UI_VERSION = "1.2"
ROOT = Path(__file__).resolve().parent.parent

# Название платформы — определено голосованием моделей 22.08.2026.
# Единственное место, где оно задаётся: правьте здесь, а не по всему файлу.
APP_NAME = "LocAlis"
APP_TAGLINE = "мультиагентная платформа · данные остаются на компьютере"

# Логотип-монограмма: шестигранник (граница платформы) + три узла (три
# proposer-модели) + линии консенсуса (голосование 2 из 3 у Verifier).
# Не картинка, а инлайн-SVG — не тяжелит панель и красится в цвета темы.
_LOGO_SVG = """
<svg width="30" height="30" viewBox="0 0 32 32" fill="none" aria-label="LocAlis">
  <path d="M16 2 L28 9 V23 L16 30 L4 23 V9 Z" stroke="#5b6ee1" stroke-width="2"/>
  <path d="M16 11 L10 20 M16 11 L22 20 M10 20 L22 20"
        stroke="#3fb98c" stroke-width="1.3" opacity="0.85"/>
  <circle cx="16" cy="11" r="2.6" fill="#7c5cff"/>
  <circle cx="10" cy="20" r="2.6" fill="#7c5cff"/>
  <circle cx="22" cy="20" r="2.6" fill="#7c5cff"/>
</svg>
"""

# Названия экранов — единственное место, где задаётся меню.
# Третий элемент — иконка Material: раньше она объявлялась, но не рисовалась.
PAGES: list[tuple[str, str, str]] = [
    ("/", "Панель управления", "space_dashboard"),
    ("/new", "Новая задача", "add_circle"),
    ("/run", "Ход выполнения", "timeline"),
    ("/history", "История задач", "history"),
    ("/knowledge", "База знаний", "auto_stories"),
    ("/tools", "Инструменты", "extension"),
    ("/secrets", "Ключи доступа", "vpn_key"),
    ("/settings", "Настройки", "tune"),
    ("/changelog", "Журнал изменений", "receipt_long"),
]

# Единая палитра. Тёмный графитовый фон + спокойный индиго-акцент:
# панель смотрят подолгу, наблюдая за ходом выполнения, поэтому яркий
# белый фон утомляет глаза.
THEME = {
    "primary": "#5b6ee1",     # акцент: кнопки, активный пункт меню
    "secondary": "#3f4a7a",
    "accent": "#7c5cff",
    "positive": "#3fb98c",
    "negative": "#e5556e",
    "warning": "#e2a33c",
    "info": "#4aa3df",
    "dark": "#161923",        # фон приложения
}

# Свои классы вместо повторения длинных цепочек Tailwind на каждом экране.
_CSS = """
:root { --ap-card: #1e2230; --ap-card-2: #232838; --ap-line: #2f3548;
        --ap-text: #e7e9f2; --ap-muted: #9aa3bd;
        /* Акцент домена: землисто-охристый цвет, отдельный от индиго/фиолетовой
           палитры АИ-инфраструктуры — используется только для QGIS/геоданных элементов,
           чтобы визуально разделять «мозг платформы» и «домен, в котором она работает». */
        --ap-domain: #c08a4e; --ap-domain-soft: rgba(192,138,78,.16);
        /* Стилистика прибора: технические значения (имена моделей, ID задач,
           время, счётчики) набираются моноширинным шрифтом отдельно от UI-текста —
           только системные гарнитуры, чтобы не тащить сеть и работать оффлайн. */
        --ap-font-mono: ui-monospace, "SF Mono", "Cascadia Code", "JetBrains Mono",
                        Consolas, monospace; }
body { background: #161923; color: var(--ap-text); }
.ap-icon { display: inline-flex; align-items: center; justify-content: center;
           line-height: 0; vertical-align: -0.15em; }
.ap-icon svg { width: 1em; height: 1em; display: block; }
.ap-card { background: var(--ap-card); border: 1px solid var(--ap-line);
           border-radius: 14px; padding: 16px; }
.ap-card-soft { background: var(--ap-card-2); border: 1px solid var(--ap-line);
                border-radius: 12px; padding: 12px; }
.ap-h1 { font-size: 1.6rem; font-weight: 700; letter-spacing: .2px; }
.ap-h2 { font-size: 1.05rem; font-weight: 600; }
.ap-muted { color: var(--ap-muted); font-size: .78rem; }
.ap-kpi { font-size: 1.9rem; font-weight: 700; line-height: 1.1; }
.ap-chip { display:inline-flex; align-items:center; gap:4px; padding: 2px 10px;
           border-radius: 999px; font-size: .72rem; border: 1px solid var(--ap-line);
           background: rgba(255,255,255,.04); }
/* Значок доменной (QGIS/геоданные) принадлежности — отдельный цвет от статусных чипов. */
.text-domain { color: var(--ap-domain); }
.ap-chip-domain { color: var(--ap-domain); border-color: var(--ap-domain);
                  background: var(--ap-domain-soft); }
/* Моноширинный реадаут для технических значений. */
.ap-mono { font-family: var(--ap-font-mono); }
.ap-nav { display:flex; align-items:center; gap:10px; padding: 9px 12px;
          border-radius: 10px; color: var(--ap-muted); text-decoration: none;
          transition: background .15s, color .15s; }
.ap-nav:hover { background: rgba(255,255,255,.05); color: var(--ap-text); }
.ap-nav-active { background: rgba(91,110,225,.16); color: #fff; font-weight: 600;
                 box-shadow: inset 2px 0 0 var(--ap-line, #5b6ee1); }
.ap-log { font-family: var(--ap-font-mono); font-size: .76rem; line-height: 1.5; }
.ap-step { border-left: 3px solid var(--ap-line); padding-left: 12px; }
.ap-step-run { border-left-color: #4aa3df; }
.ap-step-ok { border-left-color: #3fb98c; }
.ap-step-bad { border-left-color: #e5556e; }
.ap-step-warn { border-left-color: #e2a33c; }
/* Топографическая миллиметровая сетка для шапки — намёк на полевой инструмент
   геодезиста, настолько тихая, чтобы не спорить с текстом поверх неё. */
.ap-topo { background-image:
    repeating-linear-gradient(0deg, rgba(255,255,255,.05) 0 1px, transparent 1px 96px),
    repeating-linear-gradient(90deg, rgba(255,255,255,.05) 0 1px, transparent 1px 96px),
    repeating-linear-gradient(0deg, rgba(255,255,255,.022) 0 1px, transparent 1px 24px),
    repeating-linear-gradient(90deg, rgba(255,255,255,.022) 0 1px, transparent 1px 24px); }
"""


_NODE_RU = {
    "router": "Маршрутизатор",
    "retrieval": "Поиск по базе знаний",
    "secretary": "Секретарь (сводка контекста)",
    "proposers": "Исполнители (3 модели)",
    "verifier": "Проверяющий (голосование 2 из 3)",
    "judge": "Арбитр",
    "orchestrator": "Оркестратор (план и шаги)",
    "secretary_persist": "Секретарь (запись результата)",
}

_MODE_RU = {
    "auto": "автоматический",
    "local_only": "строго локальный",
    "orchestrated": "оркестратор",
}

_PLACEMENT_RU = {"cloud": "облачный планировщик", "local": "локальный планировщик",
                 "auto": "планировщик выбирается автоматически"}

# Кто принял итоговое решение — техническое значение переводим в понятное
_DECIDED_RU = {
    "verifier_consensus": "согласие исполнителей (2 из 3)",
    "judge_cloud": "облачный арбитр",
    "judge_local": "локальный арбитр",
    "orchestrator_cloud": "оркестратор (облачная сборка)",
    "orchestrator_local": "оркестратор (локальная сборка)",
    "simple_orchestrated": "один исполнитель (SIMPLE)",
    "fallback": "резервный путь",
}

# Состояние шага -> (значок, цвет, подпись)
# Состояние шага -> (значок, цвет, подпись, класс полосы слева)
_STEP_STATE = {
    "waiting": ("schedule", "text-grey-6", "ожидает", ""),
    "running": ("autorenew", "text-info", "выполняется", "ap-step-run"),
    "done": ("check_circle", "text-positive", "готово", "ap-step-ok"),
    "failed": ("error", "text-negative", "сбой", "ap-step-bad"),
}


def layout(active: str):
    """Общий каркас: верхняя панель + боковое меню с иконками."""
    ui.colors(**THEME)
    ui.add_head_html(f"<style>{_CSS}</style>")
    ui.dark_mode().enable()

    with ui.header().classes("items-center justify-between px-4 py-2 ap-topo").style(
            "background-color:#1b1f2c; border-bottom:1px solid #2f3548"):
        with ui.row().classes("items-center gap-2"):
            ui.html(_LOGO_SVG).classes("shrink-0")
            with ui.column().classes("gap-0"):
                ui.label(APP_NAME).classes("text-lg font-bold tracking-wide")
                ui.label(APP_TAGLINE).classes("ap-muted")
        with ui.row().classes("items-center gap-3"):
            ui.button(icon="help_outline", on_click=lambda: welcome.open_about()).props(
                "flat round dense size=sm").tooltip("About / setup")
            _header_status()
            ui.label(f"v{UI_VERSION}").classes("ap-muted")

    with ui.left_drawer(fixed=True).props("width=248").style(
            "background:#1b1f2c; border-right:1px solid #2f3548") as drawer:
        for path, title, icon in PAGES:
            css = "ap-nav ap-nav-active" if path == active else "ap-nav"
            with ui.link(target=path).classes(css).style("width:100%"):
                _icon(icon).classes("text-lg")
                ui.label(title)
    return drawer


def _header_status() -> None:
    """
    Индикатор режима и облака в шапке.

    Состояние нужно на каждой странице: раньше, чтобы понять, доступно ли
    облако, приходилось уходить на «Панель управления».
    """
    try:
        platform = STATE.platform
        status = platform.mode_controller.status()
        mode_text = ("локальный режим" if status.mode == "local_only"
                     else "локально + облако")
        ui.html(f'<span class="ap-chip">{_icon_svg("node")} {mode_text}</span>')
        color = "#3fb98c" if status.allowed else "#9aa3bd"
        cloud_text = "облако доступно" if status.allowed else "облако отключено"
        cloud_icon = _icon_svg("cloud_done" if status.allowed else "cloud_off")
        ui.html(f'<span class="ap-chip" style="color:{color}">{cloud_icon} {cloud_text}</span>')
    except Exception:  # noqa: BLE001 — шапка не должна ронять страницу
        ui.html('<span class="ap-chip">состояние неизвестно</span>')


def page_title(text: str, hint: str = "") -> None:
    """Заголовок экрана с необязательным пояснением."""
    ui.label(text).classes("ap-h1")
    if hint:
        ui.label(hint).classes("ap-muted mb-2")


def _status_color(ok: bool) -> str:
    return "text-positive" if ok else "text-negative"


# Плагины, помечаемые доменным (землисто-охристым) акцентом — определяем по
# имени папки плагина, а не по описанию: описание datafiles тоже упоминает
# GeoJSON/GeoPackage, но сам инструмент домен-агностичен (годится и для
# калистеники, и для геологии). Имя плагина — надёжный сигнал: только те,
# что сами называют себя в честь домена (qgis и т.п.), получают акцент.
_DOMAIN_NAME_MARKERS = ("qgis", "geo")


def _is_domain_tool(entry: Any) -> bool:
    """Относится ли плагин к геодомену (QGIS/геоданные), а не к AI-инфраструктуре."""
    name = entry.name.lower()
    return any(marker in name for marker in _DOMAIN_NAME_MARKERS)


# --------------------------------------------------------------------------
# Экран 1: Панель управления
# --------------------------------------------------------------------------


@ui.page("/")
def page_dashboard():
    layout("/")
    if welcome.is_first_run():
        welcome.open_about()
    page_title("Панель управления",
               "Состояние платформы: режим, облако, модели, инструменты, задачи")
    container = ui.column().classes("w-full gap-4")

    def kpi(title: str, value: str, hint: str = "", icon: str = "",
            color: str = "text-primary", value_mono: bool = False,
            hint_mono: bool = False) -> None:
        """
        Плитка с одним показателем — основа дашборда.

        value_mono/hint_mono — когда значение или подсказка чисто технические
        (счётчики), а не слова — включают моноширинный реадаут. Слова типа
        «Локально + облако» остаются обычным текстом.
        """
        with ui.column().classes("ap-card grow gap-1").style("min-width:220px"):
            with ui.row().classes("items-center gap-2"):
                if icon:
                    _icon(icon).classes(f"{color} text-lg")
                ui.label(title).classes("ap-muted")
            ui.label(value).classes("ap-kpi ap-mono" if value_mono else "ap-kpi")
            if hint:
                ui.label(hint).classes("ap-muted ap-mono" if hint_mono else "ap-muted")

    def refresh():
        container.clear()
        platform = STATE.platform
        status = platform.mode_controller.status()
        stats = platform.blackboard.stats()
        with container:
            # --- верхний ряд: главные показатели ---------------------------
            with ui.row().classes("w-full gap-4 items-stretch"):
                default_mode = str(platform.settings.get("mode", "auto"))
                kpi("Режим работы",
                    "Локальный" if status.mode == "local_only" else "Локально + облако",
                    f"по умолчанию: {_MODE_RU.get(default_mode, default_mode)}",
                    "shield_moon" if status.mode == "local_only" else "public")
                kpi("Облачные модели",
                    "Доступны" if status.allowed else "Недоступны",
                    status.reason[:60], "cloud_done" if status.allowed else "cloud_off",
                    "text-positive" if status.allowed else "text-warning")
                kpi("Задачи всего", str(stats["tasks_total"]),
                    ", ".join(f"{k}: {v}" for k, v in stats["by_status"].items()) or "—",
                    "assignment", "text-info", value_mono=True, hint_mono=True)
                ready = len(platform.registry.available())
                kpi("Инструменты", f"{ready} / {len(platform.registry.entries)}",
                    "готовы к работе", "extension", "text-accent", value_mono=True)

            # --- второй ряд: устойчивость и модели -------------------------
            with ui.row().classes("w-full gap-4 items-stretch"):
                with ui.column().classes("ap-card gap-2").style("min-width:320px; flex:1"):
                    with ui.row().classes("items-center gap-2"):
                        _icon("electric_bolt").classes("text-warning")
                        ui.label("Защита от сбоев облака").classes("ap-h2")
                    state_ru = {"closed": "замкнут — норма",
                                "open": "разомкнут — облако отключено",
                                "half-open": "проверка соединения"}
                    ui.label(state_ru.get(status.breaker_state, status.breaker_state)
                             ).classes("text-sm")
                    ui.label(f"сбоев подряд: {status.fail_counter}").classes("ap-muted ap-mono")
                    with ui.row().classes("gap-2 mt-1"):
                        ui.button("Сбросить", icon="restart_alt", on_click=lambda: (
                            platform.mode_controller.reset_breaker(), refresh(),
                            ui.notify("Защита сброшена", type="positive"))
                                  ).props("size=sm outline")
                        ui.button("Отключить облако", icon="cloud_off", on_click=lambda: (
                            platform.mode_controller.open_breaker(), refresh(),
                            ui.notify("Облако принудительно отключено", type="warning"))
                                  ).props("size=sm outline color=warning")

                with ui.column().classes("ap-card gap-2").style("min-width:320px; flex:1"):
                    with ui.row().classes("items-center gap-2"):
                        _icon("smart_toy").classes("text-primary")
                        ui.label("Роли и модели").classes("ap-h2")
                    try:
                        roles = [
                            ("Маршрутизатор", platform.gateway.spec("router").model),
                            ("Планировщик", platform.gateway.orchestrator_spec().model),
                            ("Проверяющий", platform.gateway.spec("verifier").model),
                            ("Арбитр локальный", platform.gateway.judge_specs()[1].model),
                        ]
                        for title, model in roles:
                            with ui.row().classes("items-center justify-between w-full"):
                                ui.label(title).classes("ap-muted")
                                ui.label(model).classes("text-xs ap-mono")
                        ui.separator().style("background:#2f3548")
                        for spec in platform.gateway.proposer_specs():
                            with ui.row().classes("items-center justify-between w-full"):
                                ui.label(f"Исполнитель {spec.id[-1].upper()}").classes("ap-muted")
                                ui.label(spec.model).classes("text-xs ap-mono")
                    except Exception as exc:  # noqa: BLE001
                        ui.label(f"Ошибка чтения models.yaml: {exc}").classes("text-negative")

            # --- третий ряд: доступность моделей и статистика --------------
            with ui.row().classes("w-full gap-4 items-stretch"):
                with ui.column().classes("ap-card gap-2").style("min-width:320px; flex:1"):
                    with ui.row().classes("items-center gap-2"):
                        _icon("memory").classes("text-info")
                        ui.label("Локальные модели (Ollama)").classes("ap-h2")
                    models = platform.gateway.local_models_available()
                    if not models:
                        ui.label("Ollama не отвечает — проверьте, запущен ли сервис"
                                 ).classes("text-negative text-sm")
                    for name, present in models.items():
                        with ui.row().classes("items-center gap-2"):
                            _icon("check_circle" if present else "cancel").classes(
                                f"{_status_color(present)} text-sm")
                            ui.label(name).classes("text-xs")

                with ui.column().classes("ap-card gap-2").style("min-width:320px; flex:1"):
                    with ui.row().classes("items-center gap-2"):
                        _icon("insights").classes("text-accent")
                        ui.label("Использование инструментов").classes("ap-h2")
                    usage = stats["tool_usage"]
                    if not usage:
                        ui.label("Инструменты пока не вызывались").classes("ap-muted")
                    for item in usage:
                        share = item["ok"] / item["calls"] if item["calls"] else 0
                        with ui.column().classes("gap-0 w-full"):
                            with ui.row().classes("items-center justify-between w-full"):
                                ui.label(item["tool"]).classes("text-xs")
                                ui.label(f"{item['ok']} из {item['calls']} успешно"
                                         ).classes("ap-muted")
                            ui.linear_progress(value=share, show_value=False,
                                               size="6px").classes("w-full")

            # --- версии: техническая информация, прячем в раскрытие -------
            with ui.expansion("Версии компонентов", icon="info").classes(
                    "w-full ap-card-soft"):
                from core.blackboard import BLACKBOARD_VERSION
                from core.tool_registry import REGISTRY_VERSION
                from workflows.main_graph import GRAPH_VERSION
                ui.label(f"граф выполнения: {GRAPH_VERSION}").classes("text-xs")
                ui.label(f"реестр инструментов: {REGISTRY_VERSION}").classes("text-xs")
                ui.label(f"хранилище: {BLACKBOARD_VERSION} "
                         f"(схема БД v{stats['schema_version']})").classes("text-xs")
                ui.label(f"интерфейс: {UI_VERSION}").classes("text-xs")

    refresh()
    with ui.row().classes("mt-4 gap-2"):
        ui.button("Обновить", icon="refresh", on_click=refresh).props("color=primary")
        ui.button("Новая задача", icon="add_circle",
                  on_click=lambda: ui.navigate.to("/new")).props("outline")


# --------------------------------------------------------------------------
# Экран 2: Новая задача
# --------------------------------------------------------------------------


@ui.page("/new")
def page_new_task():
    layout("/new")
    page_title("Новая задача", "Опишите задачу и выберите, как её выполнять")
    platform = STATE.platform

    # Пересканируем при открытии: плагин могли добавить или включить
    # уже после запуска приложения
    platform.registry.discover()
    platform.refresh_tool_context()

    selected_mode = {"value": str(platform.settings.get("mode", "auto"))}

    with ui.column().classes("w-full gap-4").style("max-width:1000px"):
        with ui.column().classes("ap-card gap-2 w-full"):
            ui.label("Что нужно сделать").classes("ap-h2")
            prompt = ui.textarea(
                placeholder="Например: прочитай введение.txt на рабочем столе, "
                            "посчитай строки и сохрани сводку в Excel",
            ).classes("w-full").props("outlined autogrow input-style=min-height:88px")
            with ui.row().classes("gap-2 flex-wrap"):
                # Готовые формулировки: быстрее, чем печатать, и заодно
                # показывают, что платформа вообще умеет
                for label, text in (
                    ("Прочитать файл", "Прочитай файл введение.txt на моём рабочем столе "
                                       "и кратко опиши содержание"),
                    ("Составной отчёт", "Прочитай введение.txt на рабочем столе, посчитай "
                                        "строки и сохрани сводку в Excel на рабочий стол"),
                    ("График", "Посчитай сколько файлов каждого типа на рабочем столе "
                               "и построй столбчатую диаграмму"),
                    ("Геообработка", "Построй буфер 100 метров вокруг слоя "
                                     "POLIGON TEST 2.shp и сохрани результат"),
                ):
                    ui.button(label, on_click=lambda t=text: prompt.set_value(t)
                              ).props("size=sm flat color=primary")

        # --- выбор режима карточками ------------------------------------
        with ui.column().classes("ap-card gap-3 w-full"):
            ui.label("Как выполнять").classes("ap-h2")
            mode_cards: dict[str, Any] = {}
            planner_box = ui.column().classes("gap-2 w-full")
            lead_box = ui.column().classes("gap-2 w-full")

            # Разрешённые папки записи (allowed_roots): песочница для задачи.
            # Пусто = без ограничения (статический allowed_roots инструментов).
            allowed_roots_input = ui.input(
                "Папки, куда разрешено писать (через ; , пусто = без ограничения)",
                placeholder=r"C:\path\to\workspace",
            ).classes("w-full").props("outlined dense")

            with lead_box:
                ui.separator().style("background:#2f3548")
                meeting_rounds_select = ui.select(
                    {0: "Без совещания (обычный Lead Agent)",
                     1: "1 раунд — модели предлагают планы, арбитр сводит",
                     2: "2 раунда — обмен планами + улучшение",
                     3: "3 раунда — максимальная проработка"},
                    value=0, label="Раунды совещания команды",
                    on_change=lambda e: on_meeting_rounds_change(e.value),
                ).classes("w-full").props("outlined dense")
                ui.label("Совещание: все модели предлагают планы, видят планы "
                         "коллег, улучшают свои. Затем арбитр сводит консенсус. "
                         "После выполнения — взаимная проверка крест-накрест. "
                         "Выполняет команда целиком: все три модели."
                         ).classes("ap-muted")

                # Одиночный Lead Agent (без совещания): выбор исполнителя и контекста
                lead_single_box = ui.column().classes("gap-2 w-full")
                with lead_single_box:
                    lead_specs = (platform.gateway.proposer_specs()
                                  + platform.gateway.lead_agent_extra_specs())
                    lead_model_select = ui.select(
                        {s.model: s.model for s in lead_specs},
                        value=(lead_specs[0].model if lead_specs else ""),
                        label="Lead-исполнитель (кто выполняет задачу)",
                    ).classes("w-full").props("outlined dense")
                    lead_ctx_number = ui.number(
                        "Контекст Lead-исполнителя, токенов",
                        value=32768, min=4096, max=131072, precision=0,
                    ).classes("w-full").props("outlined dense")
                    ui.label("Больше контекст = больше VRAM под KV-кеш. 128K — "
                             "только для очень длинных задач; на 8 ГБ VRAM "
                             "безопасно 32–64K.").classes("ap-muted")

            def select_mode(value: str) -> None:
                """Выбор режима: подсвечиваем карточку и показываем настройки."""
                selected_mode["value"] = value
                for key, card in mode_cards.items():
                    active = key == value
                    card.style("border:1px solid "
                               + ("#5b6ee1" if active else "#2f3548")
                               + "; background:" + ("rgba(91,110,225,.12)"
                                                    if active else "#232838"))
                orchestrated = value == "orchestrated"
                lead_on = orchestrated and bool(
                    (platform.settings.get("feature_flags") or {}).get("lead_agent", False))
                planner_box.set_visibility(orchestrated and not lead_on)
                lead_box.set_visibility(orchestrated and lead_on)
                # При совещании команду выполняют все модели — одиночный
                # Lead-исполнитель и его контекст не нужны.
                meeting_on = orchestrated and int(meeting_rounds_select.value or 0) > 0
                lead_single_box.set_visibility(orchestrated and lead_on
                                               and not meeting_on)

            def on_meeting_rounds_change(value: int) -> None:
                """Переключение раундов совещания: с командой одиночный
                Lead-исполнитель не нужен."""
                if selected_mode["value"] != "orchestrated":
                    return
                meeting_on = int(value or 0) > 0
                lead_single_box.set_visibility(not meeting_on)

            with ui.row().classes("w-full gap-3 items-stretch"):
                for value, title, icon, description in (
                    ("auto", "Автоматический", "groups",
                     "Три модели отвечают независимо, голосование 2 из 3, "
                     "арбитр при расхождении. Надёжно для одиночных вопросов."),
                    ("local_only", "Строго локальный", "shield_moon",
                     "То же, но облако не используется никогда. "
                     "Для непубличных данных."),
                    ("orchestrated", "Оркестратор", "account_tree",
                     "Lead Agent: один сильный исполнитель делает задачу "
                     "целиком с полным контекстом. Планировщик LLM не "
                     "используется. Для составных многофайловых задач."),
                ):
                    card = ui.column().classes("ap-card-soft gap-1 cursor-pointer").style(
                        "flex:1; min-width:230px")
                    with card:
                        with ui.row().classes("items-center gap-2"):
                            _icon(icon).classes("text-primary")
                            ui.label(title).classes("text-sm font-bold")
                        ui.label(description).classes("ap-muted")
                    card.on("click", lambda v=value: select_mode(v))
                    mode_cards[value] = card

            with planner_box:
                ui.separator().style("background:#2f3548")
                ui.label(
                    "Lead Agent: планировщик LLM не используется — один сильный "
                    "исполнитель делает задачу целиком с полным контекстом. "
                    "Настройки ниже относятся к резервному многошаговому режиму "
                    "(флаг lead_agent выключен в Настройках)."
                ).classes("ap-muted")
                planner = ui.select(
                    {"auto": "Автоматически — облако при доступности",
                     "cloud": "Облачный планировщик — сильнее планирует",
                     "local": "Локальный планировщик — ничего не уходит в сеть"},
                    value=str((platform.settings.get("orchestrator") or {}).get(
                        "placement", "auto")),
                    label="Где работает планировщик (резервный режим)",
                ).classes("w-full").props("outlined dense")

                # Выбор локального планировщика: от него зависит, дойдёт ли
                # конкретика из задачи до исполнителей. Подписи с замерами
                # берутся из models.yaml, чтобы выбор был осознанным.
                try:
                    candidates = list(platform.gateway.orchestrator_candidates())
                    default_planner = platform.gateway.orchestrator_spec().model
                except Exception:  # noqa: BLE001 — конфиг мог быть сломанным
                    candidates, default_planner = [], ""
                # Добавляем ВСЕХ пропозеров: планировщиком резервного режима
                # может быть любая из них, даже если её нет в candidates.
                candidate_models = set()
                for c in candidates:
                    # candidates — это ModelSpec-объекты, не словари
                    candidate_models.add(getattr(c, "model", None))
                for pspec in platform.gateway.proposer_specs():
                    if pspec.model not in candidate_models:
                        candidates.append(pspec)
                planner_options = {"": f"По умолчанию — {default_planner or 'из конфига'}"}
                for spec in candidates:
                    planner_options[spec.model] = str(
                        spec.extra.get("label") or spec.model)
                saved_planner = str((platform.settings.get("orchestrator") or {}).get(
                    "model", ""))
                planner_model = ui.select(
                    planner_options,
                    value=saved_planner if saved_planner in planner_options else "",
                    label="Модель планировщика (резервный многошаговый режим)",
                ).classes("w-full").props("outlined dense")
                with ui.row().classes("items-start gap-2"):
                    _icon("info").classes("text-primary text-sm mt-1")
                    ui.label("В Lead Agent-режиме эти настройки не применяются: "
                             "исполнитель — первый proposer из config/models.yaml "
                             "(сейчас glm-4.7-flash). Резервный многошаговый режим "
                             "включается флагом lead_agent=false в Настройках.").classes("ap-muted")

                with ui.row().classes("items-start gap-2"):
                    _icon("lock").classes("text-positive text-sm mt-1")
                    ui.label("Шаги всегда выполняют локальные модели: содержимое "
                             "ваших файлов не уходит в облако даже с облачным "
                             "планировщиком. Слова «конфиденциально», «ДСП» "
                             "переводят планировщик на локальный."
                             ).classes("ap-muted")

            select_mode(selected_mode["value"])

        # --- инструменты ------------------------------------------------
        with ui.column().classes("ap-card gap-2 w-full"):
            ui.label("Инструменты").classes("ap-h2")
            ready = platform.registry.available()
            options = {
                entry.name: f"{entry.name} — {(entry.manifest.description or '')[:60]}"
                for entry in ready
            }
            tools = ui.select(options, multiple=True,
                              label="Оставьте пустым, чтобы разрешить все готовые",
                              value=[]).classes("w-full").props("outlined dense")
            with ui.row().classes("gap-2 flex-wrap"):
                for entry in ready:
                    ui.html(f'<span class="ap-chip" style="color:#3fb98c">'
                            f'✓ {entry.name} <span class="ap-mono">v{entry.manifest.version}'
                            f'</span></span>')
                    if _is_domain_tool(entry):
                        ui.html('<span class="ap-chip ap-chip-domain">геодомен</span>')
            if not options:
                ui.label("Готовых инструментов нет — откройте экран «Инструменты», "
                         "там видна причина.").classes("text-warning text-xs")
            # Честно показываем неготовые плагины, а не прячем их
            broken = [e for e in platform.registry.entries.values()
                      if e.status.value in ("error", "unavailable")]
            for entry in broken:
                ui.label(f"Не готов: {entry.name} — {entry.message[:110]}"
                         ).classes("text-warning text-xs")

        def start():
            text = (prompt.value or "").strip()
            if not text:
                ui.notify("Введите описание задачи", type="warning")
                return
            mode_value = selected_mode["value"]
            selected = list(tools.value or []) or None
            placement = str(planner.value) if mode_value == "orchestrated" else ""
            planner_choice = (str(planner_model.value or "")
                              if mode_value == "orchestrated" else "")
            STATE.start_run(text, mode_value, placement)

            def worker():
                from core.schemas import ExecutionMode, Task
                from workflows.main_graph import Workflow
                import os as _os
                log = STATE.log
                try:
                    metadata = ({"orchestrator_placement": placement}
                                if placement else {})
                    if planner_choice:
                        metadata["orchestrator_model"] = planner_choice
                    # Песочница записи: папки, введённые на форме задачи.
                    raw_roots = str(allowed_roots_input.value or "").strip()
                    if raw_roots:
                        roots = [r.strip() for r in raw_roots.replace(";", ";").split(";")
                                 if r.strip()]
                        if roots:
                            metadata["allowed_roots"] = roots
                    # Lead Agent: исполнитель, контекст, раунды совещания.
                    if mode_value == "orchestrated":
                        if str(lead_model_select.value or "").strip():
                            metadata["lead_agent_model"] = str(lead_model_select.value)
                        ctx_val = int(lead_ctx_number.value or 0)
                        if ctx_val >= 4096:
                            metadata["lead_agent_num_ctx"] = ctx_val
                        rounds = int(meeting_rounds_select.value or 0)
                        if rounds > 0:
                            metadata["meeting_rounds"] = rounds
                    task = Task(prompt=text, mode=ExecutionMode(mode_value),
                                allowed_tools=selected, metadata=metadata)
                    log.task_id = task.task_id
                    state = Workflow(platform).run(task)
                    final = state.get("final")
                    if state.get("error"):
                        log.error = str(state["error"])
                    elif final is not None:
                        log.final_answer = final.answer
                        log.final_meta = (
                            f"Решение: {_DECIDED_RU.get(final.decided_by, final.decided_by)}"
                            f" · модель {final.model}"
                            f" · {'облако' if final.used_cloud else 'локально'}")
                    log.proposals = [
                        {"id": p.proposer_id, "model": p.model, "ok": p.ok,
                         "answer": p.answer or (p.error or ""), "ms": p.latency_ms,
                         "tools": len(p.tool_results)}
                        for p in (state.get("proposals") or [])
                    ]
                except Exception as exc:  # noqa: BLE001
                    log.error = f"{type(exc).__name__}: {exc}"
                finally:
                    log.running = False
                    log.finished = True

            threading.Thread(target=worker, daemon=True).start()
            ui.navigate.to("/run")

        ui.button("Запустить задачу", icon="play_arrow", on_click=start).props(
            "color=primary size=lg")


# --------------------------------------------------------------------------
# Экран 3: Ход выполнения (реалтайм)
# --------------------------------------------------------------------------

def render_plan_panel(log, task_id: str = "", on_rerun=None) -> None:
    """
    Панель плана оркестратора: шаг, исполнитель, инструменты, состояние.

    Используется и на экране «Ход выполнения», и на странице деталей задачи.
    on_rerun — обработчик кнопки «повторить шаг»; если не передан, кнопки нет
    (во время выполнения повторять шаг нельзя, модели заняты).
    """
    if not log.plan_steps and not log.plan_error:
        return
    with ui.column().classes("ap-card gap-2 w-full"):
        with ui.row().classes("items-center gap-2 w-full"):
            _icon("account_tree").classes("text-primary")
            ui.label("План оркестратора").classes("ap-h2")
            ui.space()
            if log.plan_model:
                ui.html(f'<span class="ap-chip">🧠 {log.plan_model}</span>')
            if log.placement:
                ui.html(f'<span class="ap-chip">'
                        f'{_PLACEMENT_RU.get(log.placement, log.placement)}</span>')
        if log.plan_reasoning:
            ui.label(log.plan_reasoning).classes("text-sm").style("color:#c8cde0")
        if log.plan_error:
            with ui.row().classes("items-start gap-2"):
                _icon("warning").classes("text-warning text-sm mt-1")
                ui.label(f"Планировщик не справился: {log.plan_error}. "
                         "Задача передана одному исполнителю целиком."
                         ).classes("ap-muted")

        if log.plan_steps:
            done = sum(1 for s in log.plan_steps if s.get("state") == "done")
            failed = sum(1 for s in log.plan_steps if s.get("state") == "failed")
            total = len(log.plan_steps)
            ui.linear_progress(value=done / total, show_value=False,
                               size="8px").classes("w-full")
            parts = [f"готово {done} из {total}"]
            if failed:
                parts.append(f"со сбоем: {failed}")
            suspicious = sum(1 for s in log.plan_steps if s.get("warnings"))
            if suspicious:
                parts.append(f"с вопросами: {suspicious}")
            ui.label(" · ".join(parts)).classes("ap-muted")

        for step in log.plan_steps:
            icon, color, caption, bar = _STEP_STATE.get(
                str(step.get("state")), _STEP_STATE["waiting"])
            # Шаг «готов», но к нему есть вопросы -> жёлтая полоса, не зелёная
            if step.get("warnings") and step.get("state") == "done":
                bar, caption, color = "ap-step-warn", "готово, есть вопросы", "text-warning"
            with ui.row().classes(f"items-start gap-3 w-full ap-step {bar} py-1"):
                _icon(icon).classes(f"{color} mt-1")
                with ui.column().classes("gap-0 grow"):
                    title = step.get("title") or step.get("id")
                    ui.label(f"{step.get('id')}. {title}").classes("text-sm font-bold")
                    meta = [f"исполнитель: {step.get('assignee') or '—'}"]
                    if step.get("model"):
                        meta.append(str(step["model"]))
                    if step.get("tools"):
                        meta.append("инструменты: " + ", ".join(step["tools"]))
                    if step.get("depends_on"):
                        meta.append("после " + ", ".join(step["depends_on"]))
                    if step.get("ms"):
                        meta.append(f"{step['ms']} мс")
                    ui.label(" · ".join(meta)).classes("ap-muted")
                    for warning in step.get("warnings") or []:
                        with ui.row().classes("items-start gap-1"):
                            _icon("help_outline").classes("text-warning text-xs mt-1")
                            ui.label(str(warning)).classes("text-warning text-xs")
                    if step.get("error"):
                        ui.label(str(step["error"])[:220]).classes("text-negative text-xs")
                    elif step.get("answer"):
                        ui.label(str(step["answer"])[:240]).classes("text-xs").style(
                            "color:#aab2cc")
                with ui.column().classes("items-end gap-1"):
                    ui.label(caption).classes(f"text-xs {color}")
                    if on_rerun and step.get("state") in ("done", "failed"):
                        ui.button(icon="replay",
                                  on_click=lambda s=step: on_rerun(s)).props(
                            "size=sm flat dense color=primary").tooltip("Повторить шаг")



@ui.page("/run")
def page_run():
    layout("/run")
    page_title("Ход выполнения", "Что делает платформа прямо сейчас")
    header = ui.column().classes("w-full gap-3")

    # Аварийный стоп выполняющейся задачи (24.08): пишет флаг-файл, который
    # шлюз проверяет перед каждым вызовом модели. Текущий вызов доиграет,
    # следующий не начнётся.
    def stop_task():
        STATE.request_stop()
        ui.notify("Стоп запрошен: текущий вызов модели доиграет, "
                  "затем задача остановится", type="warning")

    ui.button("Остановить задачу", icon="stop_circle", on_click=stop_task).props(
        "color=negative outline size=md")

    steps = ui.column().classes("w-full gap-0 ap-card ap-log").style("max-height:44vh; overflow:auto")
    answer_box = ui.column().classes("w-full gap-3")

    def log_line(text: str, color: str = "", indent: int = 0) -> None:
        """Строка журнала. Отступ показывает вложенность события."""
        pad = "padding-left:%dpx" % (indent * 18)
        ui.label(text).classes(f"text-xs {color}").style(pad)

    def refresh():
        log = STATE.snapshot()
        
        # Header обновляем всегда (статус может меняться)
        with header:
            header.clear()
            if not log.prompt:
                with ui.column().classes("ap-card items-center gap-2 w-full py-8"):
                    _icon("science").classes("text-4xl text-primary")
                    ui.label("Задача ещё не запускалась").classes("ap-h2")
                    ui.label("Откройте «Новая задача», опишите, что нужно сделать, "
                             "и здесь появится ход выполнения.").classes("ap-muted")
                    ui.button("Новая задача", icon="add_circle",
                              on_click=lambda: ui.navigate.to("/new")).props("color=primary")
                return
            with ui.column().classes("ap-card gap-2 w-full"):
                with ui.row().classes("items-center gap-3 w-full"):
                    if log.running:
                        ui.spinner(size="sm", color="primary")
                        ui.label(_NODE_RU.get(log.current_node, log.current_node)
                                 or "Выполняется…").classes("ap-h2")
                    elif log.error:
                        _icon("error").classes("text-negative text-xl")
                        ui.label("Завершено с ошибкой").classes("ap-h2 text-negative")
                    elif log.finished:
                        _icon("check_circle").classes("text-positive text-xl")
                        ui.label("Задача выполнена").classes("ap-h2 text-positive")
                    else:
                        _icon("hourglass_empty").classes("text-warning text-xl")
                        ui.label("Задача поставлена в очередь").classes(
                            "ap-h2 text-warning")
                    ui.space()
                    ui.html(f'<span class="ap-chip">{_MODE_RU.get(log.mode, log.mode)}</span>')
                    if log.task_id:
                        ui.html(f'<span class="ap-chip ap-mono">#{log.task_id[:8]}</span>')
                ui.label(log.prompt).classes("text-sm").style("color:#c8cde0")
            render_plan_panel(log)

        # Steps: инкрементальное обновление (не сбрасываем раскрытые аккордеоны)
        with steps:
            rendered_ids = getattr(steps, "_rendered_ids", set())
            
            if not log.events:
                steps.clear()
                steps._rendered_ids = set()
                ui.label("События появятся через несколько секунд...").classes("ap-muted")
            else:
                # Рендерим только новые события
                new_events = [e for e in log.events if e.get("id") not in rendered_ids]
                
                for event in new_events:
                    kind = event.get("kind")
                    if kind == "node_start":
                        log_line("▶ " + _NODE_RU.get(event.get("node", ""), event.get("node", "")),
                                 "font-bold text-primary")
                    elif kind == "node_end":
                        node = event.get("node", "")
                        facts = []
                        for key, label in (("ok", "успешно"), ("total", "всего"),
                                           ("steps", "шагов"), ("ok_steps", "шагов успешно"),
                                           ("chars", "символов"), ("consensus", "консенсус"),
                                           ("decided_by", "решение")):
                            if key in event:
                                value = event[key]
                                if key == "decided_by":
                                    value = _DECIDED_RU.get(str(value), value)
                                facts.append(f"{label}: {value}")
                        tail = (" · " + ", ".join(facts)) if facts else ""
                        log_line(f"✓ {_NODE_RU.get(node, node)}{tail}", "text-positive", 1)
                    elif kind == "node_skip":
                        log_line(f"↷ пропущен {_NODE_RU.get(event.get('node', ''), '')}: "
                                 f"{event.get('reason')}", "ap-muted", 1)
                    elif kind == "plan_start":
                        log_line(f"⁂ планирование: {event.get('model')} "
                                 f"({_PLACEMENT_RU.get(str(event.get('placement')), '')})",
                                 "text-accent", 1)
                    elif kind == "plan_ready":
                        log_line(f"✓ план готов, шагов: {event.get('steps')}", "text-positive", 1)
                    elif kind == "plan_error":
                        log_line(f"✗ планировщик {event.get('model')}: "
                                 f"{str(event.get('error'))[:150]}", "text-warning", 1)
                    elif kind == "step_start":
                        log_line(f"▷ шаг {event.get('step_id')} «{event.get('title')}» → "
                                 f"{event.get('assignee')} ({event.get('model')})",
                                 "text-info font-bold", 1)
                    elif kind == "step_end":
                        ok = bool(event.get("ok"))
                        warns = event.get("warnings") or []
                        mark = "✓" if ok else "✗"
                        extra = f": {str(event.get('error'))[:120]}" if not ok else ""
                        log_line(f"{mark} шаг {event.get('step_id')} за {event.get('ms')} мс{extra}",
                                 "text-positive" if ok else "text-negative", 1)
                        for warning in warns:
                            log_line(f"⚠ {warning}", "text-warning", 2)
                    elif kind == "tool_call":
                        who = event.get("proposer_id") or event.get("actor") or "?"
                        log_line(f"🔧 [{who}] {event.get('tool')}.{event.get('action')} "
                                 f"{str(event.get('args'))[:110]}", "text-info", 2)
                    elif kind == "tool_result":
                        ok = bool(event.get("ok"))
                        cached = " (из кеша)" if event.get("from_cache") else ""
                        log_line(f"{'✓' if ok else '✗'}{cached} "
                                 f"{str(event.get('summary') or event.get('error'))[:190]}",
                                 _status_color(ok), 3)
                    elif kind == "meeting_round":
                        log_line(f"⁂ Совещание, раунд {event.get('round')}/{event.get('of')}",
                                 "text-accent font-bold", 1)
                    elif kind == "meeting_speech":
                        full = str(event.get("full_text") or "")
                        with ui.expansion(
                                f"💬 [{event.get('speaker')}] "
                                f"{str(event.get('preview'))[:120]}…",
                                caption="нажми, чтобы раскрыть полный текст",
                                icon="chat").classes("w-full").style(
                                "border:1px solid #2f3548; border-radius:8px"):
                            ui.markdown(full or str(event.get("preview") or "")).classes(
                                "text-sm")
                    elif kind == "meeting_done":
                        log_line("✓ Совещание завершено, консенсус достигнут",
                                 "text-positive", 1)
                        cons_full = str(event.get("consensus_text") or "")
                        with ui.expansion(
                                f"📋 Консенсус-план: {cons_full[:120]}…",
                                caption="полный план согласован командой",
                                icon="description").classes("w-full").style(
                                "border:1px solid #2f3548; border-radius:8px"):
                            ui.markdown(cons_full).classes("text-sm")
                    elif kind == "adversarial_review":
                        ok = event.get("ok")
                        issues = event.get("issues_preview") or ""
                        log_line(f"{'✓' if ok else '✗'} Adversarial-ревью: {issues[:150]}",
                                 "text-positive" if ok else "text-negative", 1)
                    elif kind == "lead_fallback":
                        log_line(f"↻ Fallback: {event.get('from')} недоступна, "
                                 f"повтор на локальной модели", "text-warning", 1)
                    elif kind == "knowledge_context":
                        log_line(f"📚 База знаний: {event.get('chunks', 0)} "
                                 f"релевантных чанков в контекст", "text-info", 1)
                    elif kind == "llm_tps":
                        log_line(f"⚡ {event.get('model')}: {event.get('tps')} т/с "
                                 f"({event.get('eval_count')} ток. за "
                                 f"{event.get('latency_ms', 0)/1000:.1f}с)", "muted", 1)
                    elif kind == "strategy_crash":
                        log_line(f"💥 Крах стратегии {event.get('where')}: "
                                 f"{event.get('error')[:200]}", "text-negative", 1)
                    elif kind == "rerun_start":
                        log_line(f"↻ повторный прогон шага {event.get('step_id')}",
                                 "text-warning font-bold")
                    elif kind == "error":
                        log_line(f"! {str(event.get('error'))[:250]}", "text-negative", 1)
                    
                    rendered_ids.add(event.get("id"))
                steps._rendered_ids = rendered_ids

        # answer_box обновляем только если есть финал
        with answer_box:
            answer_box.clear()
            if log.final_answer:
                ui.label("Итоговый ответ").classes("text-lg font-bold")
                ui.markdown(log.final_answer).classes("text-sm")
            elif log.final_meta:
                ui.label(log.final_meta).classes("ap-muted")

    # Обновление раз в секунду: события приходят из фонового потока,
    # и без таймера экран остался бы статичным
    ui.timer(1.0, refresh)


# --------------------------------------------------------------------------
# Экран 4: История задач (+ детальный просмотр)
# --------------------------------------------------------------------------


@ui.page("/history")
def page_history():
    layout("/history")
    page_title("История задач", "Все выполненные задачи с результатами и планами")
    platform = STATE.platform
    search = ui.input(placeholder="Поиск по тексту задачи").props(
        "outlined dense clearable").classes("w-full").style("max-width:520px")
    table_box = ui.column().classes("w-full gap-2")

    _STATUS = {"done": ("выполнено", "text-positive", "check_circle"),
               "failed": ("ошибка", "text-negative", "error"),
               "running": ("выполняется", "text-info", "autorenew"),
               "created": ("создана", "ap-muted", "schedule")}

    def refresh():
        table_box.clear()
        rows = platform.blackboard.list_tasks(limit=200)
        query = (search.value or "").strip().lower()
        if query:
            rows = [r for r in rows if query in (r["prompt"] or "").lower()]
        with table_box:
            if not rows:
                with ui.column().classes("ap-card items-center gap-2 w-full py-8"):
                    _icon("inbox").classes("text-4xl text-primary")
                    ui.label("Ничего не найдено" if query else "Журнал пуст"
                             ).classes("ap-h2")
                    ui.label("Измените запрос" if query
                             else "Выполните первую задачу — она появится здесь"
                             ).classes("ap-muted")
                return
            for row in rows:
                caption, color, icon = _STATUS.get(row["status"],
                                                   (row["status"], "ap-muted", "help"))
                with ui.row().classes("ap-card items-center gap-3 w-full"):
                    _icon(icon).classes(f"{color} text-lg")
                    with ui.column().classes("gap-0 grow"):
                        ui.label(row["prompt"][:140]).classes("text-sm font-bold")
                        timestamp = row["created_at"][:19].replace("T", " ")
                        rest = [_MODE_RU.get(row["mode"], row["mode"]),
                                "облако" if row.get("used_cloud") else "локально"]
                        ui.html(f'<span class="ap-muted">'
                                f'<span class="ap-mono">{timestamp}</span> · '
                                f'{" · ".join(rest)}</span>')
                    ui.label(caption).classes(f"text-xs {color}")
                    ui.button("Подробно", icon="chevron_right",
                              on_click=lambda r=row: ui.navigate.to(
                                  f"/task/{r['task_id']}")).props("size=sm flat color=primary")

    search.on("update:model-value", lambda _: refresh())
    refresh()
    ui.button("Обновить", icon="refresh", on_click=refresh).classes("mt-3").props("outline")


@ui.page("/task/{task_id}")
def page_task_detail(task_id: str):
    layout("/history")
    platform = STATE.platform
    body = ui.column().classes("w-full gap-4")

    def render():
        body.clear()
        record = platform.blackboard.load_record(task_id)
        with body:
            if record is None:
                ui.label("Задача не найдена").classes("text-negative")
                return

            page_title("Детали задачи", record.task.prompt[:160])
            status_ru = {"done": "выполнено", "failed": "ошибка",
                         "running": "выполняется", "created": "создана"}
            with ui.row().classes("gap-2 flex-wrap"):
                ui.html(f'<span class="ap-chip ap-mono">#{task_id[:8]}</span>')
                ui.html(f'<span class="ap-chip">'
                        f'{status_ru.get(record.status, record.status)}</span>')
                ui.html(f'<span class="ap-chip">'
                        f'{_MODE_RU.get(record.task.mode.value, record.task.mode.value)}</span>')
                if record.routing:
                    ui.html(f'<span class="ap-chip">'
                            f'{record.routing.complexity.value}</span>')

            if record.final:
                with ui.column().classes("ap-card gap-2 w-full").style(
                        "border-color:#3fb98c"):
                    with ui.row().classes("items-center gap-2"):
                        _icon("task_alt").classes("text-positive")
                        ui.label("Итоговый ответ").classes("ap-h2")
                    ui.markdown(record.final.answer)
                    ui.label(
                        f"{_DECIDED_RU.get(record.final.decided_by, record.final.decided_by)}"
                        f" · модель {record.final.model}"
                        f" · {'облако' if record.final.used_cloud else 'локально'}"
                    ).classes("ap-muted")
                    if record.final.rationale:
                        with ui.expansion("Обоснование", icon="notes").classes(
                                "w-full ap-card-soft"):
                            ui.markdown(record.final.rationale)

            # --- план и шаги оркестратора -------------------------------
            if record.plan:
                log = _record_to_log(record)

                def rerun(step: dict) -> None:
                    """Повторить один шаг и пересобрать итог задачи."""
                    step_id = str(step.get("id"))
                    ui.notify(f"Повторяю шаг {step_id}...", type="ongoing")

                    def worker():
                        from workflows.main_graph import Workflow
                        try:
                            Workflow(platform).rerun_step(task_id, step_id)
                        except Exception as exc:  # noqa: BLE001
                            print(f"Повтор шага не удался: {exc}")

                    threading.Thread(target=worker, daemon=True).start()

                render_plan_panel(log, task_id=task_id,
                                  on_rerun=None if record.status == "running" else rerun)

            if record.step_results:
                with ui.column().classes("ap-card gap-1 w-full"):
                    ui.label("Результаты шагов").classes("ap-h2")
                    for result in record.step_results:
                        mark = "успех" if result.ok else "сбой"
                        warn = " · есть вопросы" if result.warnings else ""
                        with ui.expansion(
                            f"{result.step_id}. {result.title} — {result.assignee} · "
                            f"{mark}{warn} · {result.latency_ms} мс"
                        ).classes("w-full ap-card-soft"):
                            ui.label(f"модель: {result.model}").classes("ap-muted")
                            for warning in result.warnings:
                                ui.label(f"⚠ {warning}").classes("text-warning text-xs")
                            ui.markdown((result.answer or result.error or "—")[:6000])


            # --- сравнение ответов моделей (было в планах) ---------------
            if record.proposals:
                with ui.column().classes("ap-card gap-2 w-full"):
                    with ui.row().classes("items-center gap-2 w-full"):
                        _icon("compare_arrows").classes("text-accent")
                        ui.label("Сравнение исполнителей").classes("ap-h2")
                        ui.space()
                        if record.verdict:
                            agree = "консенсус" if record.verdict.consensus else "расхождение"
                            ui.html(f'<span class="ap-chip">{agree}</span>')
                    if record.verdict:
                        ui.label(record.verdict.reason).classes("ap-muted")
                    # Колонки рядом: видно, какая модель систематически
                    # расходится с остальными
                    with ui.row().classes("w-full gap-3 items-stretch"):
                        agreeing = set(record.verdict.agreeing if record.verdict else [])
                        for proposal in record.proposals:
                            border = ("#3fb98c" if proposal.proposer_id in agreeing
                                      else "#2f3548")
                            with ui.column().classes("ap-card-soft gap-1").style(
                                    f"flex:1; min-width:260px; border-color:{border}"):
                                with ui.row().classes("items-center gap-2"):
                                    _icon("check_circle" if proposal.ok else "cancel"
                                            ).classes(f"{_status_color(proposal.ok)} text-sm")
                                    ui.label(proposal.proposer_id).classes("text-sm font-bold")
                                ui.label(proposal.model).classes("ap-muted")
                                meta = [f"{proposal.latency_ms} мс",
                                        f"инструментов: {len(proposal.tool_results)}"]
                                if proposal.proposer_id in agreeing:
                                    meta.append("в согласии")
                                ui.label(" · ".join(meta)).classes("ap-muted")
                                ui.separator().style("background:#2f3548")
                                ui.markdown((proposal.answer or proposal.error or "—")[:1500]
                                            ).classes("text-xs")

            calls = platform.blackboard.get_tool_calls(task_id)
            if calls:
                with ui.column().classes("ap-card gap-1 w-full"):
                    ui.label("Вызовы инструментов").classes("ap-h2")
                    for call in calls:
                        with ui.row().classes("items-start gap-2"):
                            _icon("check_circle" if call["ok"] else "cancel").classes(
                                f"{_status_color(call['ok'])} text-sm mt-1")
                            ui.label(f"[{call['caller']}] {call['tool']}.{call['action']}"
                                     f" · {call['duration_ms']} мс · "
                                     f"{(call['summary'] or call['error'] or '')[:170]}"
                                     ).classes("text-xs ap-log")

            with ui.row().classes("gap-2"):
                ui.button("К истории", icon="arrow_back",
                          on_click=lambda: ui.navigate.to("/history")).props("outline")
                ui.button("Обновить", icon="refresh", on_click=render).props("flat")

    render()


def _bb_task_to_log(bb_task: dict) -> Any:
    """
    Собрать объект, совместимый с render_plan_panel и статус-шапкой, из
    "живой" задачи Blackboard (get_running_task) — т.е. запущенной, например,
    из CLI-процесса, события которого не попадают в RAM-лог этого UI.

    Статус (running/finished/error) берём из tasks.status, а не из пустого
    RunLog текущего процесса (см. комментарий в page_run.refresh) — иначе
    панель мгновенно и ошибочно показывает "Задача выполнена".

    Состояние КАЖДОГО ШАГА берём из поля ok/error результата, а НЕ из
    столбца plan_steps.status: тот не обновляется из-за отсутствующей
    колонки updated_at в таблице plan_steps (см. upsert_plan_step — UPDATE
    падает с "no such column: updated_at", ошибка молча гасится в
    main_graph.py) и потому всегда остаётся "waiting", даже когда шаг
    реально выполнен успешно (ok=1). Это отдельный, более глубокий баг —
    рекомендуется добавить миграцию `ALTER TABLE plan_steps ADD COLUMN
    updated_at TEXT`, но эта функция уже устойчива к нему.
    """
    from ui.state import RunLog

    status = str(bb_task.get("status") or "")
    log = RunLog(
        task_id=bb_task.get("task_id", "") or "",
        prompt=bb_task.get("prompt", "") or "",
        mode=bb_task.get("mode", "auto") or "auto",
        running=(status == "running"),
        finished=(status in ("done", "failed")),
        error=str(bb_task.get("error") or ""),
    )
    plan = bb_task.get("plan") or {}
    log.plan_model = plan.get("model", "") or ""
    log.plan_reasoning = plan.get("reasoning", "") or ""
    log.plan_error = plan.get("error") or ""
    log.placement = str(plan.get("placement") or "")
    results = {r.get("step_id"): r for r in (bb_task.get("steps") or [])}
    for step in plan.get("steps") or []:
        step_id = step.get("step_id")
        result = results.get(step_id)
        if result is None:
            state = "running" if status == "running" else "waiting"
        elif result.get("ok"):
            state = "done"
        else:
            state = "failed"
        log.plan_steps.append({
            "id": step_id, "title": step.get("title"),
            "assignee": step.get("assignee"), "tools": step.get("tools") or [],
            "depends_on": step.get("depends_on") or [],
            "model": (result or {}).get("model", "") or "",
            "state": state,
            "answer": ((result or {}).get("answer") or "")[:240],
            "error": (result or {}).get("error") or "",
            "ms": (result or {}).get("latency_ms") or 0,
            "warnings": (result or {}).get("warnings") or [],
        })
    return log


def _record_to_log(record) -> Any:
    """
    Собрать объект, совместимый с render_plan_panel, из записи в БД.

    Панель шагов писалась для живого RunLog; чтобы не дублировать разметку
    на странице деталей, переводим сохранённую запись в тот же вид.
    """
    from ui.state import RunLog

    log = RunLog(task_id=record.task.task_id, prompt=record.task.prompt,
                 mode=record.task.mode.value, finished=True)
    if record.plan:
        log.plan_model = record.plan.model
        log.plan_reasoning = record.plan.reasoning
        log.plan_error = record.plan.error or ""
        log.placement = record.plan.placement.value
        results = {r.step_id: r for r in record.step_results}
        for step in record.plan.steps:
            result = results.get(step.step_id)
            log.plan_steps.append({
                "id": step.step_id, "title": step.title,
                "assignee": step.assignee, "tools": step.tools,
                "depends_on": step.depends_on,
                "model": result.model if result else "",
                "state": ("done" if result and result.ok
                          else "failed" if result else "waiting"),
                "answer": (result.answer or "")[:240] if result else "",
                "error": (result.error or "") if result else "",
                "ms": result.latency_ms if result else 0,
                "warnings": result.warnings if result else [],
            })
    return log


# --------------------------------------------------------------------------
# Экран 5: Инструменты
# --------------------------------------------------------------------------


@ui.page("/knowledge")
def page_knowledge():
    """База знаний: список документов, поиск, удаление."""
    layout("/knowledge")
    page_title("База знаний", "Локальные знания платформы: документы и поиск")

    def get_kb():
        from core.knowledge import KnowledgeBase
        return KnowledgeBase("data/knowledge.sqlite3")

    with ui.expansion("➕ Добавить знание текстом", icon="add").classes(
            "w-full").style("border:1px solid #2f3548; border-radius:8px"):
        note_title = ui.input("Название").classes("w-full").props("outlined dense")
        note_text = ui.textarea("Текст знания").classes("w-full").props(
            "outlined dense")
        note_tags = ui.input("Теги (через запятую)").classes("w-full").props(
            "outlined dense")

        def do_add_text():
            text = (note_text.value or "").strip()
            if not text:
                ui.notify("Пустой текст", type="warning")
                return
            try:
                kb = get_kb()
                r = kb.add_document(f"note://{note_title.value or 'заметка'}",
                                    text,
                                    title=note_title.value or "заметка",
                                    tags=note_tags.value or "")
                kb.close()
            except Exception as exc:
                ui.notify(f"Ошибка: {exc}", type="negative")
                return
            if r.get("ok"):
                ui.notify(f"Добавлено ({r['chunks']} чанков)")
                note_title.value = note_text.value = note_tags.value = ""
                render_docs()
            else:
                ui.notify(r.get("error", "ошибка"), type="negative")

        ui.button("Добавить", icon="save", on_click=do_add_text).props(
            "color=primary dense")

    with ui.row().classes("w-full gap-2 items-center"):
        search_box = ui.input("Поиск по базе").classes("flex-grow").props(
            "outlined dense clearable")
        search_btn = ui.button(icon="search").props("flat round dense")
        ui.space()
        refresh_btn = ui.button(icon="refresh").props("flat round dense "
                                                      "tooltip=Обновить")

    results_col = ui.column().classes("w-full gap-2")
    docs_table = ui.column().classes("w-full gap-1")

    def render_docs():
        docs_table.clear()
        with docs_table:
            try:
                kb = get_kb()
                docs = kb.list_documents()
                kb.close()
            except Exception as exc:
                ui.label(f"Ошибка чтения базы: {exc}").classes("text-negative")
                return
            if not docs:
                ui.label("База пуста. Добавь знания через задачи или "
                         "scripts/load_folder_to_kb.py.").classes("ap-muted")
                return
            for d in docs:
                with ui.expansion(
                        f"{d['title']}  ·  {d['chunk_count']} чанков",
                        caption=f"{d['doc_type']} | теги: {d['tags'] or '—'} | "
                                f"{d['added_at'][:16]}",
                        icon="description").classes("w-full").style(
                        "border:1px solid #2f3548; border-radius:8px"):
                    with ui.row().classes("w-full justify-end gap-2"):
                        ui.button("Удалить", icon="delete", on_click=lambda
                                  did=d["doc_id"]: do_delete(did)).props(
                            "color=negative flat dense")

    def render_search(hits):
        results_col.clear()
        with results_col:
            if not hits:
                ui.label("Ничего не найдено.").classes("ap-muted")
            for i, h in enumerate(hits, 1):
                meta = " · ".join(x for x in (
                    h.get("doc_type") or "", h.get("tags") or "",
                    (h.get("source") or "")[:40]) if x)
                with ui.expansion(
                        f"{i}. [{h['via']} {h['score']:.2f}] {h['title']}",
                        caption=meta or f"чанк {h['chunk_no']}",
                        icon="search").classes("w-full").style(
                        "border:1px solid #2f3548; border-radius:8px"):
                    ui.markdown(h["text"]).classes("text-sm")

    def do_search():
        q = (search_box.value or "").strip()
        if not q:
            return
        try:
            kb = get_kb()
            hits = kb.search(q, top_k=5)
            kb.close()
        except Exception as exc:
            render_error = results_col.clear  # noqa
            results_col.clear()
            with results_col:
                ui.label(f"Ошибка поиска: {exc}").classes("text-negative")
            return
        render_docs()
        render_search(hits)

    def do_delete(doc_id: str) -> None:
        try:
            kb = get_kb()
            kb.delete_document(doc_id)
            kb.close()
        except Exception as exc:
            ui.notify(f"Ошибка удаления: {exc}", type="negative")
            return
        ui.notify("Удалено")
        render_docs()

    search_btn.on("click", do_search)
    refresh_btn.on("click", render_docs)
    search_box.on("keydown.enter", lambda _: do_search())
    render_docs()


@ui.page("/tools")
def page_tools():
    layout("/tools")
    page_title("Инструменты",
               "Каждый инструмент — отдельная папка в tools/. Чтобы добавить новый, "
               "скопируйте существующий и правьте manifest.yaml: код платформы "
               "менять не нужно.")
    platform = STATE.platform
    box = ui.column().classes("w-full gap-2")

    def toggle(entry, enabled: bool):
        """
        Вкл/выкл плагина — точечная правка строки enabled в manifest.yaml.

        Раньше здесь был safe_load + safe_dump, и перезапись СТИРАЛА все комментарии
        и форматирование файла (проверено на tools/qgis) — а в манифестах лежит
        документация для автора плагина. Правим только нужную строку.
        """
        import re

        import yaml

        manifest_path = entry.path / "manifest.yaml"
        text = manifest_path.read_text(encoding="utf-8")
        value = "true" if enabled else "false"
        patched, count = re.subn(r"^enabled:.*$", f"enabled: {value}",
                                 text, count=1, flags=re.MULTILINE)
        if count == 0:
            # Поля не было — добавляем, не трогая остальное
            patched = text.rstrip("\n") + f"\nenabled: {value}\n"
        # Проверяем, что не сломали YAML, и только потом пишем на диск
        try:
            check = yaml.safe_load(patched) or {}
            if bool(check.get("enabled")) is not enabled:
                raise ValueError("значение enabled не применилось")
        except Exception as exc:  # noqa: BLE001
            ui.notify(f"Не удалось изменить манифест: {exc}", type="negative")
            return
        manifest_path.write_text(patched, encoding="utf-8")
        platform.registry.discover()
        platform.refresh_tool_context()
        refresh()
        ui.notify(f"Инструмент {entry.name}: {'включён' if enabled else 'выключен'}")

    def refresh():
        box.clear()
        # discover() теперь атомарен (см. core/tool_registry.py), но всё равно нужен
        # refresh_tool_context() после него: иначе заново созданные инстансы
        # плагинов получат контекст только случайно, при следующем вызове
        # set_context() где-то ещё, а не гарантированно сразу.
        platform.registry.discover()
        platform.refresh_tool_context()
        with box:
            if not platform.registry.entries:
                with ui.column().classes("ap-card items-center gap-2 w-full py-8"):
                    _icon("extension_off").classes("text-4xl text-primary")
                    ui.label("Плагины не найдены в папке tools/").classes("ap-h2")
                return
            status_ru = {"ready": ("готов", "text-positive", "check_circle"),
                         "unavailable": ("окружение не готово", "text-warning", "warning"),
                         "disabled": ("выключен", "ap-muted", "toggle_off"),
                         "error": ("ошибка", "text-negative", "error")}
            for entry in platform.registry.entries.values():
                caption, color, icon = status_ru.get(
                    entry.status.value, (entry.status.value, "ap-muted", "help"))
                with ui.column().classes("ap-card gap-2 w-full"):
                    with ui.row().classes("items-center gap-3 w-full"):
                        _icon(icon).classes(f"{color} text-lg")
                        with ui.column().classes("gap-0 grow"):
                            with ui.row().classes("items-center gap-2"):
                                ui.label(entry.name).classes("ap-h2")
                                ui.html(f'<span class="ap-chip ap-mono">v{entry.manifest.version}</span>')
                                actions_count = (len(entry.instance.actions())
                                                 if entry.instance is not None else 0)
                                if actions_count:
                                    ui.html(f'<span class="ap-chip">'
                                            f'<span class="ap-mono">{actions_count}</span> действий</span>')
                                if _is_domain_tool(entry):
                                    ui.html('<span class="ap-chip ap-chip-domain">'
                                            'геодомен (QGIS)</span>')
                            ui.label(entry.manifest.description or "—").classes("ap-muted")
                            ui.label(f"{caption} — {entry.message}").classes(f"text-xs {color}")
                        ui.switch(value=entry.manifest.enabled,
                                  on_change=lambda e, en=entry: toggle(en, e.value))
                    if entry.instance is not None:
                        with ui.expansion("Что умеет", icon="list").classes(
                                "w-full ap-card-soft"):
                            for action in entry.instance.actions():
                                with ui.row().classes("items-start gap-2"):
                                    ui.label(action.name).classes(
                                        "text-xs font-bold").style("min-width:150px")
                                    ui.label(action.description).classes("ap-muted")
                    if entry.manifest.requires:
                        ui.label("Требования: " + ", ".join(entry.manifest.requires)
                                 ).classes("ap-muted")

    refresh()
    ui.button("Перечитать плагины", icon="refresh", on_click=refresh).classes("mt-3").props("outline")


# --------------------------------------------------------------------------
# Экран 6: Настройки
# --------------------------------------------------------------------------

_FLAG_RU = {
    "router": "Маршрутизатор (классификация задач)",
    "retrieval": "Поиск по локальной базе знаний",
    "tools": "Инструменты (плагины)",
    "proposers": "Исполнители (ансамбль моделей)",
    "verifier": "Проверяющий (голосование 2 из 3)",
    "judge": "Арбитр (итоговое решение)",
    "cloud_judge": "Разрешить арбитру уходить в облако",
    "judge_subagents": "Арбитр может переспрашивать локальные модели",
    "orchestrator": "Режим «Оркестратор» (планировщик делит задачу на шаги)",
    "lead_agent": "Lead Agent: один исполнитель делает задачу целиком (без планировщика)",
    "team_mode": "Команда: совещание моделей + взаимная проверка (для orchestrated)",
    "orchestrator_review": "Adversarial-ревью итога после выполнения",
    "observability": "Трейсинг в Langfuse",
}


@ui.page("/settings")
def page_settings():
    layout("/settings")
    page_title("Настройки", "Значения по умолчанию для новых задач и узлы графа")
    from config.loader import load_settings, save_settings

    settings = load_settings()
    flags = dict(settings.get("feature_flags") or {})
    resilience = dict(settings.get("resilience") or {})
    privacy = dict(settings.get("privacy") or {})

    with ui.column().classes("ap-card gap-2 w-full").style("max-width:900px"):
        ui.label("Режим работы").classes("ap-h2")
        mode_select = ui.select(
            {"auto": "Автоматический (локально + облако при доступности)",
             "local_only": "Строго локальный (данные не покидают компьютер)",
             "orchestrated": "Оркестратор (планировщик делит задачу на шаги)"},
            value=str(settings.get("mode", "auto")), label="Режим по умолчанию",
        ).classes("w-full")
        privacy_switch = ui.switch(
            "Автоматически переходить в локальный режим при признаках конфиденциальности",
            value=bool(privacy.get("force_local_for_sensitive", True)))

    with ui.column().classes("ap-card gap-2 w-full").style("max-width:900px"):
        ui.label("Режим «Оркестратор»").classes("ap-h2")
        ui.label("Планировщик делит задачу на шаги и поручает их малым локальным "
                 "моделям. Шаги выполняются ТОЛЬКО локально: содержимое файлов "
                 "не уходит в облако даже с облачным планировщиком.").classes("ap-muted")
        placement_select = ui.select(
            {"auto": "Автоматически (облако при доступности, иначе локально)",
             "cloud": "Облачный планировщик (сильнее планирует)",
             "local": "Локальный планировщик (ничего не уходит в сеть)"},
            value=str((settings.get("orchestrator") or {}).get("placement", "auto")),
            label="Где работает планировщик",
        ).classes("w-full")
        # Модель планировщика по умолчанию. На экране задачи её можно
        # переопределить для одного запуска.
        try:
            _cands = STATE.platform.gateway.orchestrator_candidates()
            _default = STATE.platform.gateway.orchestrator_spec().model
        except Exception:  # noqa: BLE001
            _cands, _default = [], ""
        planner_options = {"": f"По умолчанию — {_default or 'из models.yaml'}"}
        for _spec in _cands:
            planner_options[_spec.model] = str(_spec.extra.get("label") or _spec.model)
        _saved = str((settings.get("orchestrator") or {}).get("model", ""))
        planner_model_select = ui.select(
            planner_options,
            value=_saved if _saved in planner_options else "",
            label="Модель локального планировщика",
        ).classes("w-full")
        ui.label("Главное требование к планировщику — стабильный JSON и перенос "
                 "путей из задачи в шаги. Замеры по каждой модели — в "
                 "комментариях config/models.yaml.").classes("ap-muted")
        max_steps = ui.number(
            "Максимум шагов в плане",
            value=int((settings.get("limits") or {}).get("max_plan_steps", 6)),
            min=1, max=12, precision=0).classes("w-full")
        ui.label("Каждый шаг — отдельный вызов модели. На 8 ГБ видеопамяти больше "
                 "6 шагов дают заметное ожидание.").classes("ap-muted")

    with ui.column().classes("ap-card gap-2 w-full").style("max-width:900px"):
        ui.label("Узлы графа").classes("ap-h2")
        ui.label("Выключенный узел исключается из графа целиком — полезно для отладки "
                 "и ускорения.").classes("ap-muted")
        flag_switches: dict[str, Any] = {}
        for key, value in flags.items():
            flag_switches[key] = ui.switch(_FLAG_RU.get(key, key), value=bool(value))

    with ui.column().classes("ap-card gap-2 w-full").style("max-width:900px"):
        ui.label("Защита от сбоев облака").classes("ap-h2")
        fail_max = ui.number("Сбоев до отключения облака",
                             value=int(resilience.get("fail_max", 3)), min=1, max=20, precision=0)
        reset_timeout = ui.number("Пауза перед повторной попыткой, с",
                                  value=int(resilience.get("reset_timeout_sec", 30)),
                                  min=5, max=600, precision=0)
        health_timeout = ui.number("Таймаут проверки связи, с",
                                   value=int(resilience.get("health_timeout_sec", 5)),
                                   min=1, max=60, precision=0)

    with ui.column().classes("ap-card gap-2 w-full").style("max-width:900px"):
        ui.label("Модели по ролям").classes("ap-h2")
        ui.label("Список моделей задаётся в config/models.yaml. Добавление модели "
                 "не требует изменения кода — см. CONTRIBUTING_MODELS.md.").classes("ap-muted")
        platform = STATE.platform
        try:
            router_model = platform.gateway.spec("router").model
            verifier_model = platform.gateway.spec("verifier").model
            cloud_judge, local_judge = platform.gateway.judge_specs()
            lead = bool((platform.settings.get("feature_flags") or {}).get("lead_agent", False))
            ui.label(f"Маршрутизатор: {router_model}").classes("text-sm")
            if lead:
                ui.label("Режим: Lead Agent — задачу выполняет один сильный "
                         "исполнитель целиком (планировщик LLM не используется)").classes("text-sm")
                first = next(iter(platform.gateway.proposer_specs()), None)
                ui.label(f"Lead-исполнитель: {first.model if first else '—'}").classes("text-sm")
            else:
                ui.label(f"Планировщик (резервный многошаговый): "
                         f"{platform.gateway.orchestrator_spec().model}").classes("text-sm")
            ui.label("Исполнители: " + ", ".join(
                f"{s.id} → {s.model}" for s in platform.gateway.proposer_specs())).classes("text-sm")
            ui.label(f"Проверяющий (adversarial-ревью): {verifier_model}").classes("text-sm")
            ui.label(f"Арбитр: облако {cloud_judge.model if cloud_judge else '—'} → "
                     f"локально {local_judge.model}").classes("text-sm")
        except Exception as exc:  # noqa: BLE001
            ui.label(f"Ошибка чтения models.yaml: {exc}").classes("text-red-8")
        ui.link("Ввести ключи доступа", "/secrets").classes("text-sm")
        ui.label("Ключ облака вводится на экране «Ключи доступа» или переменной "
                 "OPENROUTER_API_KEY. Без ключа платформа работает полностью локально."
                 ).classes("ap-muted")

    def save():
        settings["mode"] = str(mode_select.value)
        settings.setdefault("privacy", {})["force_local_for_sensitive"] = bool(privacy_switch.value)
        settings["feature_flags"] = {k: bool(sw.value) for k, sw in flag_switches.items()}
        settings.setdefault("orchestrator", {})["placement"] = str(placement_select.value)
        settings["orchestrator"]["model"] = str(planner_model_select.value or "")
        settings.setdefault("limits", {})["max_plan_steps"] = int(max_steps.value or 6)
        settings.setdefault("resilience", {}).update({
            "fail_max": int(fail_max.value or 3),
            "reset_timeout_sec": int(reset_timeout.value or 30),
            "health_timeout_sec": int(health_timeout.value or 5),
        })
        save_settings(settings)
        STATE.reload_platform()
        ui.notify("Настройки сохранены, платформа перезагружена", type="positive")

    ui.button("Сохранить настройки", icon="save", on_click=save).classes("mt-3").props(
        "color=primary size=lg")

    # ---- обслуживание: перезагрузка и аварийное выключение -----------------
    # Изменения config/*.yaml, tools/, agents/ применяются платформой только
    # при пересборке. reload_platform() пересоздаёт Platform (реестр инструментов,
    # шлюз моделей, роутер) без перезапуска процесса — достаточно для конфигов.
    # Полный рестарт процесса нужен после правок кода (ui/app.py, агенты):
    # Python уже загрузил старые модули. Аварийная остановка прерывает
    # выполняющуюся задачу и закрывает приложение.

    def _running_task_active() -> bool:
        return STATE.snapshot().running

    with ui.column().classes("ap-card gap-2 w-full").style("max-width:900px"):
        ui.label("Обслуживание").classes("ap-h2")
        ui.label(
            "Перезагрузка компонентов применяет изменения конфигов, моделей и "
            "инструментов без закрытия панели. Полный рестарт нужен после правок "
            "кода (агентов, интерфейса): он перезапускает процесс целиком."
        ).classes("ap-muted")

        def do_reload():
            if _running_task_active():
                ui.notify("Задача выполняется — дождитесь завершения или используйте "
                          "аварийное выключение", type="warning")
                return
            STATE.reload_platform()
            STATE.platform  # форсируем пересборку ленивого синглтона
            ui.notify("Компоненты платформы перезагружены", type="positive")

        ui.button("Перезагрузить компоненты", icon="refresh", on_click=do_reload).props(
            "color=primary outline size=md").classes("mt-1")

        confirm = {"armed": False}

        def do_restart():
            """Полный рестарт процесса: sys.executable + те же аргументы."""
            if not confirm["armed"]:
                confirm["armed"] = True
                restart_btn.text = "Точно перезапустить? Нажмите ещё раз"
                ui.notify("Нажмите кнопку ещё раз для подтверждения", type="warning")
                return
            import os, subprocess, sys, time
            if _running_task_active():
                ui.notify("Задача ещё выполняется — она будет прервана", type="warning")
            python = sys.executable
            args = [python, *sys.argv]
            subprocess.Popen(args, cwd=str(ROOT),
                             env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            time.sleep(0.5)  # дать новому процессу стартовать
            os._exit(0)      # аварийно гасим текущий процесс без cleanup-ловушек

        restart_btn = ui.button("Перезапустить панель (полный рестарт)",
                                icon="restart_alt", on_click=do_restart).props(
            "color=warning outline size=md")

        def do_shutdown():
            if not confirm.setdefault("armed_off", False):
                confirm["armed_off"] = True
                shutdown_btn.text = "Остановить платформу? Нажмите ещё раз"
                ui.notify("Выполняющаяся задача будет прервана! "
                          "Нажмите ещё раз для подтверждения", type="negative")
                return
            import os
            try:
                STATE.platform.blackboard.close()
            except Exception:  # noqa: BLE001 — база могла быть уже закрыта
                pass
            os._exit(1)

        shutdown_btn = ui.button("Аварийное выключение", icon="power_settings_new",
                                 on_click=do_shutdown).props("color=negative outline size=md")
        ui.label("Аварийное выключение прерывает выполняющуюся задачу и закрывает "
                 "панель. Завершённые задачи сохранены в журнале.").classes("ap-muted")


# --------------------------------------------------------------------------
# Экран 7: Журнал изменений
# --------------------------------------------------------------------------


@ui.page("/changelog")
def page_changelog():
    layout("/changelog")
    page_title("Журнал изменений", "Что менялось в платформе и почему")
    changelog = ROOT / "CHANGELOG.md"
    if changelog.is_file():
        ui.markdown(changelog.read_text(encoding="utf-8")).classes("w-full max-w-4xl")
    else:
        ui.label("Файл CHANGELOG.md не найден").classes("opacity-70")


# --------------------------------------------------------------------------
# Запуск
# --------------------------------------------------------------------------


@ui.page("/secrets")
def page_secrets():
    """Экран ключей вынесен в ui/pages/ — образец для новых экранов."""
    layout("/secrets")
    from ui.pages.secrets_page import render
    render()


def run_ui(port: int = 8080, show: bool = True, native: bool = False) -> None:
    """
    Запустить панель управления.

    native=True открывает отдельное окно приложения (нужен pywebview),
    по умолчанию — вкладка браузера, что не требует лишних зависимостей.
    """
    # Ключи, вписанные в интерфейсе, должны быть доступны с первого запроса
    from config.secrets import load_secrets
    load_secrets()
    ui.run(title=APP_NAME, port=port, show=show, reload=False,
           native=native, favicon="🧭", language="ru")


if __name__ in {"__main__", "__mp_main__"}:
    run_ui()
