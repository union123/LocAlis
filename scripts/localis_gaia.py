# -*- coding: utf-8 -*-
"""
GAIA-адаптер для LocAlis: прогон GAIA validation Level 1 (text-only) через платформу.

Проще TB-адаптера: нет docker/tmux. Каждый вопрос -> main.py -> финальный ответ
-> сравнение с эталоном (нормализованный string match, официальный критерий GAIA).

Запуск (ночь):
  .venv/Scripts/python.exe scripts/localis_gaia.py --limit 42
Результат: data/gaia_runs/<ts>/gaia_results.json + консольная сводка.
"""

import argparse
import base64
import json
import os
import re
import string
import subprocess
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path

PLATFORM = Path(__file__).resolve().parent.parent
PYTHON = PLATFORM / ".venv" / "Scripts" / "python.exe"
RUNNER = PLATFORM / "main.py"

# --- официальный нормализатор ответов GAIA (упрощённая версия из scoring script) ---
def normalize_answer(a: str) -> str:
    a = str(a or "")
    # число: убрать пробелы/запятые/валиuta
    a = a.split("=")[-1]
    # первый пункт списка, если ответ - список через запятую/AND
    a = re.split(r"\band\b|,", a)[0] if a.count(",") == 0 and " and " in a else a
    a = a.replace(",", "").replace("$", "").replace("%", "")
    a = re.sub(r"(?<=\d)[ \u00a0\u202f](?=\d)", "", a)  # разделители тысяч
    a = unicodedata.normalize("NFKD", a)
    a = a.translate(str.maketrans("", "", string.punctuation))
    a = " ".join(a.lower().split())
    return a.strip()

def gaia_correct(expected: str, got: str) -> bool:
    e, g = normalize_answer(expected), normalize_answer(got)
    if not e:
        return False
    return e in g or g == e

# --- загрузка датасета ---
def load_tasks(level: str = "1", limit: int | None = None) -> list[dict]:
    from huggingface_hub import hf_hub_download
    import pyarrow.parquet as pq
    p = hf_hub_download("sayan1101/gaia_filtered_text_only",
                        "data/validation-00000-of-00001.parquet", repo_type="dataset")
    rows = pq.read_table(p).to_pylist()
    tasks = [r for r in rows if str(r["Level"]) == level and not r["file_name"]]
    if limit:
        tasks = tasks[:limit]
    return tasks

# --- вызов платформы (как в TB-адаптере) ---
def ask_platform(question: str, timeout_sec: int = 1200,
                 mode: str = "lead") -> tuple[int, str, str]:
    q = (question +
         "\n\n[ФОРМАТ ОТВЕТА] Последней строкой выведи ровно одну строку вида: "
         "ФИНАЛЬНЫЙ ОТВЕТ: <короткий ответ>. Без пояснений и markdown, только "
         "сам ответ (число/имя/название). Отвечай на языке вопроса.")
    meta = json.dumps({"meeting_rounds": 3}) if mode == "team" else "{}"
    cmd = [str(PYTHON), str(RUNNER), "--mode", "orchestrated", "--quiet",
           "--meta", meta, "--task", q]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout_sec, cwd=str(PLATFORM),
                              encoding="utf-8", errors="replace", env=env)
        return proc.returncode, proc.stdout or "", proc.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout {timeout_sec}s"

def extract_answer(stdout: str) -> str:
    """Финальный ответ: строка после маркера ФИНАЛЬНЫЙ ОТВЕТ: (иначе последняя
    короткая строка блока). Провалы 04.09: ответ тонул в markdown-процессе."""
    m = None
    for m in re.finditer(r"ФИНАЛЬНЫЙ ОТВЕТ\s*[:—-]\s*(.+)", stdout):
        pass  # берём ПОСЛЕДНЕЕ вхождение
    if m:
        ans = m.group(1).strip().strip("*#").strip()
        if ans:
            return ans
    parts = [p.strip() for p in stdout.split("=" * 70) if p.strip()]
    for block in reversed(parts):
        low = block.lower()
        if any(k in low for k in ("решение принято", "подробности:", "обоснование")):
            continue
        lines = [ln.strip().strip("*#").strip() for ln in block.splitlines()
                 if ln.strip() and not ln.strip().startswith(("#", "-", "*", "|"))]
        if lines:
            return lines[-1]
    return ""

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", default="1", choices=["1", "2"])
    ap.add_argument("--limit", type=int, default=None,
                    help="Сколько задач (по умолчанию все Level-1)")
    ap.add_argument("--timeout", type=int, default=1200,
                    help="Сек на задачу")
    ap.add_argument("--mode", default="lead", choices=["lead", "team"],
                    help="lead = один исполнитель; team = совещание 3 раунда")
    ap.add_argument("--out", default=None, help="Папка результата")
    args = ap.parse_args()

    tasks = load_tasks(args.level, args.limit)
    out_dir = Path(args.out) if args.out else (
        PLATFORM / "data" / "gaia_runs" /
        (datetime.now().strftime("%Y-%m-%d__%H-%M-%S") + f"-{args.mode}"))
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"GAIA level-{args.level}: {len(tasks)} задач -> {out_dir}")
    results, correct = [], 0
    t0 = time.time()
    for i, task in enumerate(tasks, 1):
        q, expected = task["Question"], str(task["Final answer"])
        print(f"\n[{i}/{len(tasks)}] {task['task_id'][:8]}: {q[:70]}...")
        rc, stdout, stderr = ask_platform(q, args.timeout, args.mode)
        answer = extract_answer(stdout)
        ok = gaia_correct(expected, answer)
        correct += ok
        results.append({"task_id": task["task_id"], "level": args.level,
                        "question": q, "expected": expected, "answer": answer[:2000],
                        "correct": ok, "rc": rc, "stderr_tail": stderr[-300:]})
        print(f"    expected={expected!r} got={answer[:60]!r} -> {'PASS' if ok else 'FAIL'}")

        # промежуточное сохранение после каждой задачи (ночь может прерваться)
        (out_dir / "gaia_results.json").write_text(json.dumps(
            {"results": results, "correct": correct, "total": len(results),
             "accuracy": correct / len(results)}, ensure_ascii=False, indent=1),
            encoding="utf-8")

    mins = (time.time() - t0) / 60
    print(f"\n=== GAIA L{args.level}: {correct}/{len(results)} = "
          f"{100*correct/len(results):.0f}% за {mins:.0f} мин ===")
    print(f"Результат: {out_dir / 'gaia_results.json'}")
    return 0

if __name__ == "__main__":
    sys.exit(main())
