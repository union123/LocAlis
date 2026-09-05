# -*- coding: utf-8 -*-
"""
LocAlis-Bench v1 — свой бенчмарк платформы на реальных задачах.

12 задач с машиночитаемыми критериями успеха:
  - 4 гео-данных (CSV/GeoJSON/отчёты)
  - 4 код/файлы (скрипты, структура)
  - 2 knowledge (поиск по базе знаний)
  - 2 веб (поиск + сводка)

Использование:
  python scripts/localis_bench.py                # все задачи, режим lead
  python scripts/localis_bench.py --mode team    # режим команды
  python scripts/localis_bench.py --only geo_1,code_2
  python scripts/localis_bench.py --mode simple --only geo_1

Результат: data/bench_runs/bench_<дата>_<режим>.json + HTML-отчёт.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SANDBOX = BASE / "data" / "bench_runs" / "sandbox"
PLATFORM_RUNNER = BASE / "main.py"

# ---------------------------------------------------------------- задачи

def _task(id_, category, prompt, checks, description):
    """checks: список (name, callable(workdir)->bool)."""
    return {
        "id": id_, "category": category, "prompt": prompt,
        "checks": checks, "description": description,
    }


def check_file_exists(workdir, *names):
    return all((workdir / n).exists() for n in names)


def check_csv_header(workdir, name, expected_cols):
    """CSV-заголовок == expected (порядок важен), без BOM."""
    import csv
    p = workdir / name
    if not p.exists():
        return False
    with open(p, encoding="utf-8-sig") as f:
        row = next(csv.reader(f), [])
    return [c.strip() for c in row] == expected_cols


def check_csv_rows(workdir, name, min_rows):
    import csv
    p = workdir / name
    if not p.exists():
        return False
    with open(p, encoding="utf-8-sig") as f:
        return sum(1 for _ in csv.reader(f)) - 1 >= min_rows


def check_python_compiles(workdir, name):
    p = workdir / name
    if not p.exists():
        return False
    import py_compile, tempfile
    try:
        py_compile.compile(str(p), cfile=tempfile.mktemp(), doraise=True)
        return True
    except py_compile.PyCompileError:
        return False


def check_contains(workdir, name, *needles):
    p = workdir / name
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8", errors="replace").lower()
    return all(n.lower() in text for n in needles)


def check_not_contains(workdir, name, *needles):
    p = workdir / name
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8", errors="replace").lower()
    return not any(n.lower() in text for n in needles)


def check_json_valid(workdir, name, required_keys):
    p = workdir / name
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return all(k in d for k in required_keys)
    except Exception:
        return False


def check_png_big(workdir, name, min_kb=10):
    p = workdir / name
    return p.exists() and p.stat().st_size > min_kb * 1024


def check_knowledge_hit(workdir, marker):
    """Задача knowledge: платформа должна была записать результат в БЗ.
    Проверяем через sqlite напрямую."""
    import sqlite3
    dbp = BASE / "data" / "knowledge.sqlite3"
    if not dbp.exists():
        return False
    db = sqlite3.connect(str(dbp))
    try:
        row = db.execute(
            "SELECT COUNT(*) FROM chunks WHERE content LIKE ?",
            (f"%{marker}%",)).fetchone()
        return row[0] > 0
    finally:
        db.close()


TASKS = [
    # ---------- ГЕО-ДАННЫЕ ----------
    _task(
        "geo_1", "geo",
        "Создай файл {wd}/samples.csv (формат csv) со строгим порядком столбцов: "
        "проба,Au_г_т,Ag_г_т,интервал_м. Данные: 101,1.2,8,100-105 / 102,0.3,5,105-110 / "
        "103,2.8,12,110-115 / 104,0.7,6,115-120 / 105,3.1,15,120-125.",
        [("csv_exists", lambda w: check_file_exists(w, "samples.csv")),
         ("csv_header", lambda w: check_csv_header(w, "samples.csv",
              ["проба", "Au_г_т", "Ag_г_т", "интервал_м"])),
         ("csv_rows", lambda w: check_csv_rows(w, "samples.csv", 5))],
        "CSV со строгим порядком столбцов (баг 28fde479)",
    ),
    _task(
        "geo_2", "geo",
        "Дан файл {wd}/input_points.json (массив объектов с полями id, lat, lon, au). "
        "Напиши скрипт {wd}/filter_au.py на Python, который читает json и записывает "
        "{wd}/high_au.json только пробы с au > 1.0, отсортированные по убыванию au. "
        "Запусти скрипт и убедись, что выходной файл создан.",
        [("script_exists", lambda w: check_file_exists(w, "filter_au.py")),
         ("script_compiles", lambda w: check_python_compiles(w, "filter_au.py")),
         ("output_exists", lambda w: check_file_exists(w, "high_au.json")),
         ("output_json", lambda w: check_json_valid(w, "high_au.json", ["id", "au"]))],
        "Обработка GeoJSON/JSON точек: фильтр + сортировка",
    ),
    _task(
        "geo_3", "geo",
        "Прочитай {wd}/assay.csv (результаты опробования). Найди пробу с максимальным "
        "Au и запиши отчёт {wd}/report.md: заголовок '# Отчёт по опробованию', "
        "строку 'Лучшая проба: <номер>' и 'Au: <значение> г/т'.",
        [("report_exists", lambda w: check_file_exists(w, "report.md")),
         ("report_header", lambda w: check_contains(w, "report.md", "# отчёт по опробованию")),
         ("report_best", lambda w: check_contains(w, "report.md", "лучшая проба")),
         ("no_placeholder", lambda w: check_not_contains(w, "report.md", "xxx", "todo", "<номер>"))],
        "Чтение CSV + осмысленный отчёт (нет плейсхолдеров)",
    ),
    _task(
        "geo_4", "geo",
        "Создай файл {wd}/strat.json (json) со стратиграфией разреза: ключи "
        "'period' (значение 'мел'), 'stage' ('сеноман'), 'thickness_m' (45), "
        "'lithology' ('известняк').",
        [("json_exists", lambda w: check_file_exists(w, "strat.json")),
         ("json_ok", lambda w: check_json_valid(w, "strat.json",
              ["period", "stage", "thickness_m", "lithology"]))],
        "Структурированные стратиграфические данные",
    ),
    # ---------- КОД / ФАЙЛЫ ----------
    _task(
        "code_1", "code",
        "Напиши {wd}/primes.py — модуль с функцией is_prime(n) и primes_up_to(n). "
        "Добавь блок if __name__ == '__main__': с выводом простых чисел до 30. "
        "Убедись, что файл компилируется без ошибок.",
        [("exists", lambda w: check_file_exists(w, "primes.py")),
         ("compiles", lambda w: check_python_compiles(w, "primes.py")),
         ("has_main", lambda w: check_contains(w, "primes.py", "__main__")),
         ("has_funcs", lambda w: check_contains(w, "primes.py", "is_prime", "primes_up_to"))],
        "Классическая задача на код со структурой",
    ),
    _task(
        "code_2", "code",
        "В {wd}/ есть испорченный скрипт buggy_calc.py. Исправь ошибки так, чтобы "
        "скрипт при запуске не падал и выводил корректную сумму чисел от 1 до 10 (55). "
        "Сохрани исправленную версию в тот же файл и запусти для проверки.",
        [("exists", lambda w: check_file_exists(w, "buggy_calc.py")),
         ("compiles", lambda w: check_python_compiles(w, "buggy_calc.py")),
         ("runs_ok", lambda w: _runs_and_prints(w, "buggy_calc.py", "55"))],
        "Отладка испорченного скрипта",
    ),
    _task(
        "code_3", "code",
        "Создай {wd}/data_catalog.json — каталог файлов: ключ 'files' — массив "
        "объектов {'name': <имя файла в папке>, 'size_bytes': <размер>, 'type': "
        "<'py'|'json'|'csv'|'other'>} для каждого файла в {wd} (без подпапок).",
        [("json_exists", lambda w: check_file_exists(w, "data_catalog.json")),
         ("json_ok", lambda w: check_json_valid(w, "data_catalog.json", ["files"]))],
        "Инспекция файловой системы + структурированный вывод",
    ),
    _task(
        "code_4", "code",
        "Напиши {wd}/gpx_to_csv.md — ИНСТРУКЦИЮ (не код): пошаговый алгоритм "
        "конвертации GPX-треков в CSV для QGIS, минимум 5 шагов, упомяни "
        "проекции WGS84 и UTM.",
        [("exists", lambda w: check_file_exists(w, "gpx_to_csv.md")),
         ("has_steps", lambda w: check_contains(w, "gpx_to_csv.md", "gpx", "csv")),
         ("has_crs", lambda w: check_contains(w, "gpx_to_csv.md", "wgs84", "utm")),
         ("not_code", lambda w: check_not_contains(w, "gpx_to_csv.md", "import os"))],
        "Текстовая инструкция (не код!) — проверка следования форматам",
    ),
    # ---------- KNOWLEDGE ----------
    _task(
        "know_1", "knowledge",
        "Найди в базе знаний информацию о маршруте Голубинка (дневники). Запиши "
        "краткую выжимку в {wd}/golubinka.md: что искали, что нашли (2-3 факта), "
        "затем сохрани факт о находке в базу знаний тегом 'bench,know_1'.",
        [("md_exists", lambda w: check_file_exists(w, "golubinka.md")),
         ("md_content", lambda w: check_contains(w, "golubinka.md", "голубин")),
         ("kb_written", lambda w: check_knowledge_hit(w, "know_1"))],
        "Гибридный поиск по БЗ + запись результата",
    ),
    _task(
        "know_2", "knowledge",
        "Найди в базе знаний всё про известняки Таврической гряды. Сохрани в "
        "{wd}/tavrida.md краткую сводку (5+ фактов) со ссылкой на источники "
        "(имена файлов-дневников).",
        [("md_exists", lambda w: check_file_exists(w, "tavrida.md")),
         ("facts", lambda w: check_contains(w, "tavrida.md", "известняк", "таврич"))],
        "Тематический поиск по 558 чанкам дневников",
    ),
    # ---------- ВЕБ ----------
    _task(
        "web_1", "web",
        "Найди в интернете высоту горы Ай-Петри в Крыму и запиши в "
        "{wd}/ai_petri.md: название, высоту в метрах, источник (URL).",
        [("md_exists", lambda w: check_file_exists(w, "ai_petri.md")),
         ("has_height", lambda w: check_contains(w, "ai_petri.md", "1234", "м")),
         ("has_url", lambda w: check_contains(w, "ai_petri.md", "http"))],
        "Веб-поиск факта + источник",
    ),
    _task(
        "web_2", "web",
        "Найди в интернете, что такое GPR (георадар) и запиши в {wd}/gpr.md: "
        "расшифровку, принцип работы (2-3 предложения), один реальный прибор "
        "с производителем.",
        [("md_exists", lambda w: check_file_exists(w, "gpr.md")),
         ("has_decoding", lambda w: check_contains(w, "gpr.md", "георадиолокаци", "ground penetrating")),
         ("has_device", lambda w: check_contains(w, "gpr.md", "гпп", "гэотек", "geoscanners", "gssi", "датчик"))],
        "Веб-поиск технического термина",
    ),
]


def _runs_and_prints(workdir, script, expected):
    """Скрипт запускается и печатает expected."""
    import subprocess as sp
    p = workdir / script
    if not p.exists():
        return False
    try:
        r = sp.run([sys.executable, str(p)], capture_output=True,
                   text=True, timeout=30, cwd=str(workdir))
        return expected in (r.stdout or "")
    except Exception:
        return False


# ---------------------------------------------------------------- раннер

def seed_task_workdir(t, wd: Path):
    """Создаём входные файлы для задач, которым нужны данные."""
    wd.mkdir(parents=True, exist_ok=True)

    if t["id"] == "geo_2":
        pts = [
            {"id": 201, "lat": 44.75, "lon": 33.85, "au": 0.4},
            {"id": 202, "lat": 44.76, "lon": 33.86, "au": 1.7},
            {"id": 203, "lat": 44.77, "lon": 33.87, "au": 3.2},
            {"id": 204, "lat": 44.78, "lon": 33.88, "au": 0.9},
            {"id": 205, "lat": 44.79, "lon": 33.89, "au": 2.1},
        ]
        (wd / "input_points.json").write_text(
            json.dumps(pts, ensure_ascii=False, indent=1), encoding="utf-8")

    if t["id"] == "geo_3":
        rows = "проба,Au_г_т,Ag_г_т\n401,0.5,3\n402,4.6,11\n403,1.1,7\n404,0.2,2\n"
        (wd / "assay.csv").write_text(rows, encoding="utf-8")

    if t["id"] == "code_2":
        (wd / "buggy_calc.py").write_text(
            "# Испорченный скрипт: сумма 1..10\n"
            "total = 0\n"
            "for i in range(1, 10):  # BUG: должна быть range(1, 11)\n"
            "    total += i\n"
            "print('Summa:', totl)  # BUG: опечатка totl\n",
            encoding="utf-8")


def run_task(t, mode: str, timeout_min: int = 25):
    wd = SANDBOX / t["id"]
    if wd.exists():
        for p in sorted(wd.rglob("*"), reverse=True):
            p.unlink() if p.is_file() else None
    seed_task_workdir(t, wd)

    prompt = t["prompt"].format(wd=str(wd))
    log = wd / ".." / f"{t['id']}_{mode}.log"

    meta = json.dumps({"allowed_roots": [str(wd)],
                       **({"meeting_rounds": 3} if mode == "team" else {})})
    cmd = [sys.executable, str(PLATFORM_RUNNER),
           "--mode", "orchestrated", "--task", prompt, "--meta", meta]
    # Команда передаётся через metadata.meeting_rounds (см. main_graph node_orchestrator);
    # simple/lead идут штатным путём orchestrated без meeting_rounds.
    env = dict(os.environ, PYTHONIOENCODING="utf-8",
               LOCALIS_BENCH_TASK=t["id"])

    t0 = time.time()
    with open(log, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, env=env,
                                cwd=str(BASE))
        try:
            proc.wait(timeout=timeout_min * 60)
        except subprocess.TimeoutExpired:
            proc.kill()
    dt = time.time() - t0

    results = {}
    for name, fn in t["checks"]:
        try:
            results[name] = bool(fn(wd))
        except Exception as e:
            results[name] = False
    passed = sum(results.values())
    return {
        "id": t["id"], "category": t["category"],
        "description": t["description"],
        "passed": passed, "total": len(results),
        "ok": passed == len(results),
        "checks": results, "seconds": round(dt, 1),
        "timeout": dt > timeout_min * 60 - 1,
    }


def render_html(all_runs, stamp, mode):
    rows = ""
    for r in all_runs:
        color = "#5fd08a" if r["ok"] else "#ff6b81"
        checks = " ".join(
            f"<span style='color:{'#5fd08a' if v else '#ff6b81'}'>{'✓' if v else '✗'}</span>"
            for v in r["checks"].values())
        rows += (f"<tr><td>{r['id']}</td><td>{r['category']}</td>"
                 f"<td>{r['description']}</td>"
                 f"<td style='color:{color};font-weight:700'>{r['passed']}/{r['total']}</td>"
                 f"<td>{checks}</td><td class='num'>{r['seconds']}s</td></tr>")
    total_p = sum(r["passed"] for r in all_runs)
    total_t = sum(r["total"] for r in all_runs)
    ok_n = sum(1 for r in all_runs if r["ok"])
    return f"""<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8">
<title>LocAlis-Bench — {mode} — {stamp}</title><style>
body{{background:#12151f;color:#dfe3ee;font-family:'Segoe UI',sans-serif;margin:24px}}
h1{{color:#7aa2f7}} table{{border-collapse:collapse;width:100%}}
th,td{{padding:8px 12px;border-bottom:1px solid #2f3548;text-align:left}}
td.num{{text-align:right;font-variant-numeric:tabular-nums}}
.sum{{font-size:1.4em;margin:16px 0}} .sum b{{color:#7aa2f7}}
</style></head><body>
<h1>🏔 LocAlis-Bench v1</h1>
<p>Режим: <b>{mode}</b> · дата: {stamp}</p>
<div class="sum">Задач закрыто полностью: <b>{ok_n}/{len(all_runs)}</b> ·
чеков пройдено: <b>{total_p}/{total_t}</b></div>
<table><tr><th>Задача</th><th>Категория</th><th>Описание</th>
<th>Счёт</th><th>Чеки</th><th>Время</th></tr>{rows}</table>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="lead",
                    choices=["simple", "lead", "team"],
                    help="режим стратегии платформы")
    ap.add_argument("--only", default="",
                    help="запятая-разделённые id задач (пусто = все)")
    ap.add_argument("--timeout", type=int, default=25,
                    help="минут на задачу")
    args = ap.parse_args()

    only = {x.strip() for x in args.only.split(",") if x.strip()}
    tasks = [t for t in TASKS if not only or t["id"] in only]
    if not tasks:
        print("Нет задач для прогона"); sys.exit(1)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n🏔 LocAlis-Bench v1: {len(tasks)} задач, режим {args.mode}\n")

    all_runs = []
    for t in tasks:
        print(f"▶ {t['id']} [{t['category']}] {t['description']}")
        r = run_task(t, args.mode, args.timeout)
        all_runs.append(r)
        mark = "✅" if r["ok"] else "❌"
        print(f"  {mark} {r['passed']}/{r['total']} чеков, {r['seconds']}s")
        for k, v in r["checks"].items():
            if not v:
                print(f"      ✗ {k}")
        print()

    out = BASE / "data" / "bench_runs"
    out.mkdir(parents=True, exist_ok=True)
    jpath = out / f"bench_{stamp}_{args.mode}.json"
    jpath.write_text(json.dumps(
        {"date": stamp, "mode": args.mode, "runs": all_runs},
        ensure_ascii=False, indent=1), encoding="utf-8")
    hpath = out / f"bench_{stamp}_{args.mode}.html"
    hpath.write_text(render_html(all_runs, stamp, args.mode), encoding="utf-8")

    ok_n = sum(1 for r in all_runs if r["ok"])
    tp = sum(r["passed"] for r in all_runs)
    tt = sum(r["total"] for r in all_runs)
    print("=" * 50)
    print(f"ИТОГ: {ok_n}/{len(all_runs)} задач полностью, {tp}/{tt} чеков")
    print(f"JSON: {jpath}")
    print(f"HTML: {hpath}")
    try:
        os.startfile(hpath)
    except Exception:
        pass


if __name__ == "__main__":
    main()
