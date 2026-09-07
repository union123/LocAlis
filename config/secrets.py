"""
Хранилище ключей доступа (API-ключей).

Зачем отдельный слой: раньше ключи брались только из переменных окружения,
и пользователю приходилось открывать терминал (setx). Теперь ключи можно
вписать в интерфейсе — они сохраняются в data/secrets.json и подгружаются
в окружение процесса при старте.

Приоритет источников (от высшего к низшему):
  1. переменная окружения, заданная вручную в системе;
  2. файл data/secrets.json (то, что вписано в интерфейсе).

Так системная настройка администратора не переопределяется молча из UI.

БЕЗОПАСНОСТЬ: файл лежит локально, в .gitignore, с правами только для
владельца (насколько это позволяет ОС). Это НЕ шифрованное хранилище —
для защиты от другого пользователя того же компьютера используйте
системные переменные окружения.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

SECRETS_VERSION = "1.0"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SECRETS_PATH = PROJECT_ROOT / "data" / "secrets.json"


class SecretDefinition:
    """Описание одного ключа: что это, зачем и обязателен ли."""

    def __init__(self, env: str, title: str, description: str,
                 required: bool = False, placeholder: str = "",
                 url: str = "", group: str = "Прочее"):
        self.env = env                  # имя переменной окружения
        self.title = title              # подпись в интерфейсе (по-русски)
        self.description = description  # пояснение для пользователя
        self.required = required        # обязателен для работы облака
        self.placeholder = placeholder
        self.url = url                  # где взять ключ
        self.group = group


# Реестр ключей. Добавить новый ключ = добавить запись здесь;
# экран «Ключи доступа» построится автоматически, код UI не меняется.
KNOWN_SECRETS: list[SecretDefinition] = [
    SecretDefinition(
        env="OPENROUTER_API_KEY",
        title="Ключ OpenRouter",
        description=(
            "Нужен только для облачного усиления (арбитр и облачные исполнители). "
            "Без него платформа работает полностью локально."
        ),
        required=False,
        placeholder="sk-or-v1-...",
        url="https://openrouter.ai/keys",
        group="Облачные модели",
    ),
    SecretDefinition(
        env="OPENAI_API_KEY",
        title="Ключ OpenAI (необязательно)",
        description="Если хотите использовать модели OpenAI напрямую, минуя OpenRouter.",
        placeholder="sk-...",
        url="https://platform.openai.com/api-keys",
        group="Облачные модели",
    ),
    SecretDefinition(
        env="LANGFUSE_PUBLIC_KEY",
        title="Langfuse: публичный ключ",
        description="Для трейсинга вызовов. Берётся в настройках проекта Langfuse.",
        placeholder="pk-lf-...",
        url="http://localhost:3000",
        group="Наблюдаемость (Langfuse)",
    ),
    SecretDefinition(
        env="LANGFUSE_SECRET_KEY",
        title="Langfuse: секретный ключ",
        description="Парный секретный ключ того же проекта Langfuse.",
        placeholder="sk-lf-...",
        url="http://localhost:3000",
        group="Наблюдаемость (Langfuse)",
    ),
    SecretDefinition(
        env="LANGFUSE_HOST",
        title="Langfuse: адрес сервера",
        description="Обычно http://localhost:3000 при запуске через Docker Compose.",
        placeholder="http://localhost:3000",
        group="Наблюдаемость (Langfuse)",
    ),
]

SECRETS_BY_ENV: dict[str, SecretDefinition] = {s.env: s for s in KNOWN_SECRETS}


class SecretStore:
    """Чтение и запись ключей в локальный файл."""

    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else SECRETS_PATH

    # ---- низкоуровневое --------------------------------------------------

    def _read_raw(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"version": SECRETS_VERSION, "secrets": {}}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            # Повреждённый файл не должен ронять приложение
            return {"version": SECRETS_VERSION, "secrets": {}}
        if not isinstance(data, dict):
            return {"version": SECRETS_VERSION, "secrets": {}}
        data.setdefault("version", SECRETS_VERSION)
        if not isinstance(data.get("secrets"), dict):
            data["secrets"] = {}
        return data

    def _write_raw(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        # Ограничиваем права: только владелец (на Windows влияет частично)
        try:
            self.path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass

    # ---- публичный API ---------------------------------------------------

    def all_stored(self) -> dict[str, str]:
        """Ключи, сохранённые через интерфейс (без переменных окружения)."""
        return {k: str(v) for k, v in self._read_raw()["secrets"].items() if str(v).strip()}

    def get(self, env: str) -> str:
        """Значение ключа с учётом приоритета: окружение выше файла."""
        from_env = os.environ.get(env, "").strip()
        if from_env:
            return from_env
        return str(self.all_stored().get(env, "")).strip()

    def source(self, env: str) -> str:
        """Откуда взят ключ: 'окружение' | 'файл' | '' (нет)."""
        if os.environ.get(env, "").strip():
            # Если значение совпадает с файлом, значит его подгрузили мы
            stored = self.all_stored().get(env, "")
            if stored and stored == os.environ[env].strip():
                return "файл"
            return "окружение"
        return "файл" if self.all_stored().get(env) else ""

    def set(self, env: str, value: str) -> None:
        """Сохранить ключ и сразу применить его к текущему процессу."""
        value = (value or "").strip()
        data = self._read_raw()
        if value:
            data["secrets"][env] = value
            os.environ[env] = value
        else:
            data["secrets"].pop(env, None)
            # Убираем из окружения только если сами его туда положили
            os.environ.pop(env, None)
        self._write_raw(data)

    def set_many(self, values: dict[str, str]) -> None:
        for env, value in values.items():
            self.set(env, value)

    def delete(self, env: str) -> None:
        self.set(env, "")

    def load_into_env(self, override: bool = False) -> list[str]:
        """
        Подгрузить сохранённые ключи в переменные окружения процесса.

        override=False (по умолчанию): системная переменная имеет приоритет.
        Возвращает список применённых имён — удобно для логов и тестов.
        """
        applied: list[str] = []
        for env, value in self.all_stored().items():
            if override or not os.environ.get(env, "").strip():
                os.environ[env] = value
                applied.append(env)
        return applied


# Единый экземпляр для приложения
STORE = SecretStore()


def mask(value: str) -> str:
    """Замаскировать ключ для показа в интерфейсе и логах."""
    value = (value or "").strip()
    if not value:
        return ""
    if len(value) <= 10:
        return "•" * len(value)
    return f"{value[:6]}…{value[-4:]}"


def load_secrets(override: bool = False) -> list[str]:
    """Вызывается при старте приложения (UI и CLI) до создания платформы."""
    return STORE.load_into_env(override=override)


def secret_status() -> list[dict[str, Any]]:
    """Состояние всех известных ключей — данные для экрана «Ключи доступа»."""
    result: list[dict[str, Any]] = []
    for definition in KNOWN_SECRETS:
        value = STORE.get(definition.env)
        result.append({
            "env": definition.env,
            "title": definition.title,
            "description": definition.description,
            "required": definition.required,
            "placeholder": definition.placeholder,
            "url": definition.url,
            "group": definition.group,
            "filled": bool(value),
            "masked": mask(value),
            "source": STORE.source(definition.env),
        })
    return result
