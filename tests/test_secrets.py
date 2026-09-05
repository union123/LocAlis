"""
Тесты хранилища ключей доступа.

Главное, что проверяем: ключ, введённый в интерфейсе, доходит до
Mode Controller и шлюза без переменных окружения (раньше требовался setx),
а неверный ключ честно отклоняется, а не считается «облако доступно».
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.resilience import ModeController  # noqa: E402
from config.secrets import (  # noqa: E402
    KNOWN_SECRETS,
    SECRETS_BY_ENV,
    SecretStore,
    mask,
    secret_status,
)

ENV = "OPENROUTER_API_KEY"


@pytest.fixture()
def store(tmp_path: Path, monkeypatch) -> SecretStore:
    """Изолированное хранилище: не трогаем реальный data/secrets.json."""
    monkeypatch.delenv(ENV, raising=False)
    instance = SecretStore(tmp_path / "secrets.json")
    # Подменяем глобальный STORE, которым пользуется остальной код
    import config.secrets as module
    monkeypatch.setattr(module, "STORE", instance)
    return instance


# ---- базовое поведение ---------------------------------------------------


def test_missing_key_returns_empty(store: SecretStore):
    assert store.get(ENV) == ""
    assert store.source(ENV) == ""


def test_set_and_get_without_env_vars(store: SecretStore, monkeypatch):
    """Ключ вписан в интерфейсе — переменная окружения не нужна."""
    monkeypatch.delenv(ENV, raising=False)
    store.set(ENV, "sk-or-v1-abc")
    assert store.get(ENV) == "sk-or-v1-abc"
    assert store.source(ENV) == "файл"


def test_value_persists_between_processes(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    path = tmp_path / "secrets.json"
    SecretStore(path).set(ENV, "sk-or-v1-persist")
    monkeypatch.delenv(ENV, raising=False)   # имитируем новый процесс
    assert SecretStore(path).get(ENV) == "sk-or-v1-persist"


def test_whitespace_is_trimmed(store: SecretStore):
    store.set(ENV, "  sk-or-v1-pad  ")
    assert store.get(ENV) == "sk-or-v1-pad"


def test_delete_removes_key(store: SecretStore):
    store.set(ENV, "sk-or-v1-tmp")
    store.delete(ENV)
    assert store.get(ENV) == ""
    assert ENV not in json.loads(store.path.read_text(encoding="utf-8"))["secrets"]


def test_empty_value_deletes(store: SecretStore):
    store.set(ENV, "sk-or-v1-x")
    store.set(ENV, "   ")
    assert store.get(ENV) == ""


def test_set_many(store: SecretStore):
    store.set_many({ENV: "a-key", "LANGFUSE_HOST": "http://localhost:3000"})
    assert store.get(ENV) == "a-key"
    assert store.get("LANGFUSE_HOST") == "http://localhost:3000"


# ---- приоритет источников ------------------------------------------------


def test_system_env_wins_over_file(store: SecretStore, monkeypatch):
    """Настройка администратора не должна молча переопределяться из UI."""
    store.set(ENV, "из-файла")
    monkeypatch.setenv(ENV, "из-системы")
    assert store.get(ENV) == "из-системы"
    assert store.source(ENV) == "окружение"


def test_load_into_env_does_not_override_by_default(store: SecretStore, monkeypatch):
    store.set(ENV, "из-файла")
    monkeypatch.setenv(ENV, "из-системы")
    store.load_into_env()
    import os
    assert os.environ[ENV] == "из-системы"


def test_load_into_env_can_force_override(store: SecretStore, monkeypatch):
    store.set(ENV, "из-файла")
    monkeypatch.setenv(ENV, "из-системы")
    applied = store.load_into_env(override=True)
    import os
    assert ENV in applied and os.environ[ENV] == "из-файла"


# ---- устойчивость --------------------------------------------------------


def test_corrupted_file_does_not_crash(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    path = tmp_path / "secrets.json"
    path.write_text("{это не json", encoding="utf-8")
    assert SecretStore(path).get(ENV) == ""


def test_non_dict_file_is_ignored(tmp_path: Path, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    path = tmp_path / "secrets.json"
    path.write_text('["список вместо объекта"]', encoding="utf-8")
    assert SecretStore(path).all_stored() == {}


# ---- маскирование --------------------------------------------------------


@pytest.mark.parametrize("value", ["sk-or-v1-верысекретныйключ1234", "sk-or-v1-abcdefghij"])
def test_mask_hides_middle(value: str):
    masked = mask(value)
    assert value not in masked
    assert masked.startswith(value[:6]) and value[-4:] in masked


def test_mask_short_value_fully_hidden():
    assert set(mask("abc123")) == {"•"}


def test_mask_empty():
    assert mask("") == ""


# ---- интеграция с Mode Controller ---------------------------------------


def test_mode_controller_sees_key_from_store(store: SecretStore, monkeypatch):
    """Ключ из интерфейса открывает облако без переменных окружения."""
    monkeypatch.delenv(ENV, raising=False)
    controller = ModeController(mode="auto", health_probe=lambda: True)
    assert controller.status().allowed is False       # ключа ещё нет

    store.set(ENV, "sk-or-v1-from-ui")
    monkeypatch.delenv(ENV, raising=False)           # только файл, без окружения
    assert controller.has_api_key() is True
    assert controller.status().allowed is True


def test_rejected_key_blocks_cloud(store: SecretStore, monkeypatch):
    """
    Регрессия: /models у OpenRouter отвечает 200 без ключа, поэтому
    неверный ключ считался рабочим. Теперь 401 означает отказ.
    """
    monkeypatch.delenv(ENV, raising=False)
    store.set(ENV, "sk-or-v1-неверный")
    monkeypatch.delenv(ENV, raising=False)

    class FakeResponse:
        status_code = 401

    class FakeHttpx:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    controller = ModeController(mode="auto")
    status = controller.status()
    assert status.allowed is False
    assert "ключ отклонён" in status.reason


def test_server_error_blocks_cloud(store: SecretStore, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    store.set(ENV, "sk-or-v1-ok")
    monkeypatch.delenv(ENV, raising=False)

    class FakeResponse:
        status_code = 503

    class FakeHttpx:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    status = ModeController(mode="auto").status()
    assert status.allowed is False and "503" in status.reason


def test_valid_key_allows_cloud(store: SecretStore, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    store.set(ENV, "sk-or-v1-good")
    monkeypatch.delenv(ENV, raising=False)

    class FakeResponse:
        status_code = 200

    class FakeHttpx:
        @staticmethod
        def get(*_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setitem(sys.modules, "httpx", FakeHttpx)
    assert ModeController(mode="auto").status().allowed is True


def test_local_only_ignores_key_entirely(store: SecretStore, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    store.set(ENV, "sk-or-v1-good")
    controller = ModeController(mode="local_only", health_probe=lambda: True)
    assert controller.status().allowed is False


# ---- реестр ключей для интерфейса ---------------------------------------


def test_registry_contains_openrouter():
    assert ENV in SECRETS_BY_ENV
    assert SECRETS_BY_ENV[ENV].url.startswith("https://")


def test_all_definitions_have_russian_titles():
    for definition in KNOWN_SECRETS:
        assert definition.title and definition.description
        assert definition.group


def test_secret_status_shape(store: SecretStore, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    store.set(ENV, "sk-or-v1-status")
    monkeypatch.delenv(ENV, raising=False)
    statuses = {item["env"]: item for item in secret_status()}
    assert statuses[ENV]["filled"] is True
    # В интерфейс уходит только маска, не сам ключ
    assert "sk-or-v1-status" not in json.dumps(statuses, ensure_ascii=False)


def test_gateway_error_mentions_ui(store: SecretStore, monkeypatch):
    """Сообщение об ошибке должно подсказывать экран, а не команду setx."""
    monkeypatch.delenv(ENV, raising=False)
    from config.loader import load_models
    from core.llm_gateway import LLMGateway, ModelSpec

    gateway = LLMGateway(load_models(), ModeController(mode="auto",
                                                      health_probe=lambda: True))
    spec = ModelSpec(id="c", provider="openrouter", model="any/model")
    response = gateway.chat(spec, [{"role": "user", "content": "привет"}])
    assert response.ok is False
    assert "Ключи доступа" in response.error or "ключ облака" in response.error.lower()
