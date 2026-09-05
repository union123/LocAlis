"""
Загрузка и версионирование конфигурации.

Принцип дорабатываемости: конфиг имеет поле version. При повышении версии
добавляется функция-миграция в MIGRATIONS, старые файлы читаются без правки
пользователем (обратная совместимость).
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Callable

import yaml

CONFIG_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CONFIG_DIR.parent

# Актуальные версии схем конфигов
CURRENT_SETTINGS_VERSION = "1.0"
CURRENT_MODELS_VERSION = "1.0"
CURRENT_ROUTING_VERSION = "1.0"


class ConfigError(RuntimeError):
    """Ошибка конфигурации — намеренно шумная, чтобы не работать втихую неверно."""


# --------------------------------------------------------------------------
# Миграции
# --------------------------------------------------------------------------
# Ключ — версия ИЗ файла, значение — функция, приводящая к следующей версии.
# Пример на будущее:
#   def _settings_1_0_to_1_1(data): data.setdefault("new_block", {}); return data
#   MIGRATIONS_SETTINGS = {"1.0": ("1.1", _settings_1_0_to_1_1)}
MigrationStep = tuple[str, Callable[[dict[str, Any]], dict[str, Any]]]

MIGRATIONS_SETTINGS: dict[str, MigrationStep] = {}
MIGRATIONS_MODELS: dict[str, MigrationStep] = {}
MIGRATIONS_ROUTING: dict[str, MigrationStep] = {}


def _apply_migrations(
    data: dict[str, Any],
    migrations: dict[str, MigrationStep],
    target_version: str,
    what: str,
) -> dict[str, Any]:
    """Последовательно применить миграции до целевой версии."""
    data = copy.deepcopy(data)
    version = str(data.get("version", "0.0"))
    seen: set[str] = set()
    while version != target_version:
        if version in seen:
            raise ConfigError(f"Цикл миграций {what} на версии {version}")
        seen.add(version)
        step = migrations.get(version)
        if step is None:
            raise ConfigError(
                f"Не могу привести {what} с версии {version} к {target_version}: "
                f"нет миграции. Обновите файл вручную."
            )
        next_version, func = step
        data = func(data)
        data["version"] = next_version
        version = next_version
    return data


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise ConfigError(f"Не найден файл конфигурации: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"Ошибка YAML в {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path.name}: ожидался словарь на верхнем уровне")
    return data


# --------------------------------------------------------------------------
# Публичный API
# --------------------------------------------------------------------------


def load_settings(path: Path | str | None = None) -> dict[str, Any]:
    """Прочитать config/settings.yaml с миграциями."""
    data = _read_yaml(Path(path) if path else CONFIG_DIR / "settings.yaml")
    data = _apply_migrations(data, MIGRATIONS_SETTINGS, CURRENT_SETTINGS_VERSION, "settings.yaml")
    # Переменная окружения перебивает файл — удобно для запуска "строго офлайн"
    env_mode = os.environ.get("AGENT_PLATFORM_MODE")
    if env_mode in ("auto", "local_only"):
        data["mode"] = env_mode
    return data


def load_models(path: Path | str | None = None) -> dict[str, Any]:
    """Прочитать config/models.yaml с миграциями."""
    data = _read_yaml(Path(path) if path else CONFIG_DIR / "models.yaml")
    return _apply_migrations(data, MIGRATIONS_MODELS, CURRENT_MODELS_VERSION, "models.yaml")


def load_routing_rules(path: Path | str | None = None) -> dict[str, Any]:
    """Прочитать config/routing_rules.yaml с миграциями."""
    data = _read_yaml(Path(path) if path else CONFIG_DIR / "routing_rules.yaml")
    return _apply_migrations(data, MIGRATIONS_ROUTING, CURRENT_ROUTING_VERSION, "routing_rules.yaml")


def save_settings(data: dict[str, Any], path: Path | str | None = None) -> None:
    """Сохранить настройки (используется экраном «Настройки» в UI)."""
    target = Path(path) if path else CONFIG_DIR / "settings.yaml"
    target.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
        encoding="utf-8",
    )


def resolve_path(relative: str) -> Path:
    """Путь из конфига -> абсолютный путь относительно корня проекта."""
    candidate = Path(relative)
    if candidate.is_absolute():
        return candidate
    return (PROJECT_ROOT / candidate).resolve()


def feature_enabled(settings: dict[str, Any], flag: str, default: bool = True) -> bool:
    """Проверка фича-флага. Незнакомый флаг => default (не ломаем старые конфиги)."""
    flags = settings.get("feature_flags") or {}
    return bool(flags.get(flag, default))
