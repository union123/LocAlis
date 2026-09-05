"""
Разрешение путей: перевод человеческих и «угаданных» путей в реальные.

Зачем понадобилось (жалоба 05.08: «моделям трудновато находить файлы»):
у этого пользователя папка Desktop физически отсутствует — рабочий стол
перенаправлен в OneDrive («C:/Users/<имя>/OneDrive/Рабочий стол»). Модель,
обученная на стандартном Windows, честно строит C:/Users/<имя>/Desktop и
получает «путь не найден». То же с «Документы»/Documents.

Модуль домен-независимый: спрашивает у ОС реальные расположения известных
папок, а дальше умеет искать файл по имени, если путь всё равно не совпал.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import unquote

# Синонимы известных папок: как модель может их назвать -> внутренний ключ
_ALIASES: dict[str, str] = {
    "desktop": "desktop", "рабочийстол": "desktop", "рабочий_стол": "desktop",
    "documents": "documents", "мои документы": "documents",
    "документы": "documents", "docs": "documents",
    "downloads": "downloads", "загрузки": "downloads", "скачанное": "downloads",
    "pictures": "pictures", "изображения": "pictures", "картинки": "pictures",
    "music": "music", "музыка": "music",
    "videos": "videos", "видео": "videos",
    "home": "home", "домашняя": "home", "профиль": "home",
    "onedrive": "onedrive", "ванедрайв": "onedrive",
    "temp": "temp", "врем": "temp",
}

# Английские имена системных папок и их русские варианты в OneDrive
_CANDIDATES: dict[str, tuple[str, ...]] = {
    "desktop": ("Desktop", "Рабочий стол"),
    "documents": ("Documents", "Документы", "Мои документы"),
    "downloads": ("Downloads", "Загрузки"),
    "pictures": ("Pictures", "Изображения"),
    "music": ("Music", "Музыка"),
    "videos": ("Videos", "Видео"),
}


def _profile() -> Path:
    return Path(os.environ.get("USERPROFILE") or Path.home())


def _onedrive() -> Path | None:
    for var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        value = os.environ.get(var)
        if value and Path(value).is_dir():
            return Path(value)
    guess = _profile() / "OneDrive"
    return guess if guess.is_dir() else None


def known_folders() -> dict[str, Path]:
    """
    Реальные расположения известных папок ЭТОГО компьютера.

    Порядок поиска важен: сначала профиль, потом OneDrive. Если в профиле
    папки нет (перенаправление в OneDrive), берётся вариант из OneDrive —
    именно этот случай ломал модели.
    """
    found: dict[str, Path] = {"home": _profile()}
    one = _onedrive()
    if one:
        found["onedrive"] = one
    roots = [_profile()] + ([one] if one else [])
    for key, names in _CANDIDATES.items():
        for root in roots:
            for name in names:
                candidate = root / name
                if candidate.is_dir():
                    found[key] = candidate
                    break
            if key in found:
                break
    temp = os.environ.get("TEMP") or os.environ.get("TMP")
    if temp and Path(temp).is_dir():
        found["temp"] = Path(temp)
    return found


def describe_known_folders() -> str:
    """Однострочная подсказка для системного промпта и описаний действий."""
    pairs = [f"{k} = {v}" for k, v in known_folders().items()]
    return "; ".join(pairs)


def _strip_wrappers(text: str) -> str:
    text = text.strip().strip("\"'`«»")
    if text.lower().startswith("file:///"):
        text = text[8:]
    if "%" in text and re.search(r"%[0-9A-Fa-f]{2}", text):
        text = unquote(text)
    return text


def normalize(raw: object) -> str:
    """
    Привести путь от модели к виду, понятному ОС, и подставить известные папки.

    Обрабатываются реальные искажения:
      "C:\path\file.txt"      — кавычки
      ~/Desktop/a.txt           — тильда
      %USERPROFILE%\a.txt      — переменные окружения
      Рабочий стол/a.txt        — человеческое имя папки без пути
      C:/Users/<имя>/Desktop/…  — несуществующее перенаправление -> OneDrive
      /Users/<имя>/a.txt        — POSIX-стиль, потерянная буква диска
    """
    text = _strip_wrappers(str(raw or ""))
    if not text:
        return ""
    text = os.path.expandvars(text)
    if text.startswith("~"):
        text = str(_profile()) + text[1:]
    text = text.replace("/", os.sep).replace("\\", os.sep)

    folders = known_folders()
    # Ведущее человеческое имя папки: «Рабочий стол\a.txt», «Documents/a.txt»
    head, _, tail = text.partition(os.sep)
    key = _ALIASES.get(head.strip().lower().replace(" ", ""))
    if key and key in folders and not re.match(r"^[A-Za-z]:", head):
        text = str(folders[key]) + (os.sep + tail if tail else "")

    # Потерянная буква диска
    if re.match(rf"^{re.escape(os.sep)}(Users|ProgramData|Windows)", text, re.IGNORECASE):
        text = "C:" + text

    return _redirect_known(text, folders)


def _redirect_known(text: str, folders: dict[str, Path]) -> str:
    """
    Если путь ведёт в несуществующую системную папку профиля, а её реальное
    расположение известно (OneDrive) — переписать начало пути.
    """
    if Path(text).exists():
        return text
    profile = str(_profile())
    for key, names in _CANDIDATES.items():
        real = folders.get(key)
        if real is None:
            continue
        for name in names:
            guessed = str(Path(profile) / name)
            if text.lower().startswith(guessed.lower()) and not Path(guessed).is_dir():
                return str(real) + text[len(guessed):]
    return text
