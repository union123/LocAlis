# LocAlis

> **Локальная мультиагентная платформа: три модели спорят над планом, исполняют его инструментами и проверяют друг друга — всё на вашем ноутбуке, без облака.**

![local](https://img.shields.io/badge/100%25-local-no_cloud_data_sent-success) ![multi-model](https://img.shields.io/badge/multi--model-3%2B%20proposers-blue) ![tools](https://img.shields.io/badge/tools-11%2B%20built--in-orange) ![license](https://img.shields.io/badge/license-MIT-blue)

```
Задача → Router → Secretary (контекст)
            ↓
   ┌── proposer_a ──┐
   ├── proposer_b ──┤  → Арбитр → консенсус-план
   └── proposer_c ──┘
            ↓
     Lead Executor (tool-calling: файлы, QGIS, браузер, контейнер…)
            ↓
   Cross-review (по фактам на диске) → Adversarial-ревью
            ↓
   [auto-fix] → Итог + полный журнал в SQLite
```

**LocAlis** превращает несколько локальных LLM (Ollama + llama.cpp) в слаженную
команду: модели независимо предлагают план, арбитр сводит их в консенсус,
исполнитель работает через 11+ инструментов (файлы, CSV/Excel, QGIS, браузер,
shell в контейнере), а результат до выдачи проходит кросс-ревью и
adversarial-проверку. Всё — на consumer-железе: ансамбль 30B-моделей уживается
в 8 ГБ VRAM + 32 ГБ RAM thanks to VRAM-арбитру (тяжёлые модели выгружают
экспертов в RAM).

Конкурентоспособно с облачными агентами на публичных бенчмарках
(Terminal-Bench, GAIA — см. таблицу ниже), при этом бесплатно и приватно.

## Возможности

- **Маршрутизация**: простые задачи решаются одной моделью сразу, сложные — ансамблем
- **Team-режим**: N раундов совещания → консенсус арбитра → исполнение → кросс-ревью → auto-fix
- **Adversarial-ревью**: отдельная модель пытается опровергнуть результат перед выдачей
- **11+ инструментов**: файлы, CSV/Excel (с сохранением порядка колонок), QGIS, графики, headless-браузер (Playwright), desktop-автоматизация, гибридная база знаний (FTS5 + эмбеддинги), контейнерный shell
- **VRAM-арбитр**: 30B+ модели получают эксклюзивный слот GPU, остальные выгружаются — ансамбль уживается в 8 ГБ VRAM
- **Live-телеметрия**: токены/сек и латентность каждого вызова модели в SQLite

## Бенчмарки

| Бенчмарк | Lead | Team-3R |
|---|---|---|
| Terminal-Bench core (срез 20 задач) | 2/20 (10%) | **4/20 (20%)** |
| GAIA validation L1 (42 задачи, text-only) | 13/42 (31%) | 2/мод. прогон, см. сноску |

Первые две строки — одна ночная абляция (06.09), один адаптер v6.3,
равные условия. Team-победы проверены вручную: `heterogeneous-dates`
решён с точным эталоном 11.428571 (совещание поймало off-by-one),
`fix-permissions` — 1/1 exec-теста в контейнере. Ниша Team — локальные
файловые/shell-задачи; ниша Lead — web-исследования (быстрее, меньше
таймаутов). **Сноска**: в том прогоне llama-server Ornith-модели был
недоступен — Team шёл на двух моделях из трёх; полная 3-модельная
абляция ещё не проводилась. GAIA Lead шёл на старом извлечении ответа
(с «ФИНАЛЬНЫЙ ОТВЕТ:» маркером прогноз 50%+); GAIA-Team результат
не сопоставим напрямую. LocAlis-Bench v1 (12 задач) — регрессионный набор.

Скрипты: `scripts/localis_gaia.py`, `scripts/localis_bench.py`, `scripts/tb20_subset.sh`.
Адаптер Terminal-Bench: `scripts/localis_tb_agent.py` (требует
[terminal-bench](https://github.com/laude-institute/terminal-bench) и Docker).

## Установка

Требования: Windows (проверено) или Linux, Python 3.10+, [Ollama](https://ollama.com);
для контейнерного инструмента — Docker.

### Модели

**Обязательные** (маленькие, нужны для старта любой задачи):

```bash
ollama pull qwen2.5:3b-instruct   # роутер + secretary (~2 ГБ)
ollama pull bge-m3:latest          # эмбеддинги базы знаний (~1.2 ГБ)
```

**Опциональные** (ансамбль для orchestrated/team режимов — выберите под своё
железо, полные аналоги в `config/models.example.yaml`):

```bash
ollama pull glm-4.7-flash          # proposer/executor
ollama pull nemotron-3.5-lightning:30b-a3b-q4_K_M
# 35B+ модели — через llama.cpp (llama-server), см. notes в models.example.yaml
```

Минимальный старт: только две обязательные модели — платформа работает в
simple-режиме и с базой знаний. VRAM-арбитр сам разрулит, кто когда в GPU.

Windows-пользователям: `install_models.bat` в корне репозитория скачает всё
необходимое одной командой (double-click).

### Установка

```bash
git clone <repo-url> localis
cd localis
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux: .venv/bin/pip

# настроить конфиг:
copy config\models.example.yaml config\models.yaml   # Linux: cp ...
# отредактируйте пути/имена моделей под себя

# запустить панель:
.venv/Scripts/python.exe main.py --ui
# → http://127.0.0.1:8080
```

Ключи облачных провайдеров (опционально — платформа полноценно работает и без
них): `python main.py --set-key OPENROUTER_API_KEY=sk-or-v1-...` или переменная
окружения. Ключи хранятся в `data/secrets.json` (в git не попадает).

## Использование

- **Панель** (`main.py --ui`) — ввод задачи, живой журнал инструментов, токены/сек каждого вызова модели, статус совещаний и ревью
- **CLI**: `python main.py --mode orchestrated --task "ваша задача"`
- **Team-режим**: флаг в панели или `--meta '{"meeting_rounds":3}'`
- **Бенчмарки**: `python scripts/localis_bench.py --mode lead` / `python scripts/localis_gaia.py --level 1 --mode team`

## Архитектура (кратко)

```
Router → Secretary → [Team meeting] → Lead executor (tools)
     → Cross-review (disk facts) → Adversarial review → [auto-fix]
```

Ключевые компоненты: `core/llm_gateway.py` (единый вызов всех провайдеров +
VRAM-арбитр + телеметрия), `core/blackboard.py` (SQLite-журнал решений),
`tools/` (плагины с манифестами), `workflows/strategies.py` (режимы исполнения).

## Статус

Research preview. Работает на машине автора (RTX 3070 8GB VRAM, 40 GB RAM, Windows); установка
на других конфигурациях требует правки `config/models.yaml` под своё железо.
Issues приветствуются.
