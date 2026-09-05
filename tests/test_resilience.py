"""
Тесты Mode Controller и Circuit Breaker.

Проверяем именно то, что раньше ломалось: облако не должно «тихо»
использоваться в local_only и не должно блокировать работу при сбоях.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.loader import (  # noqa: E402
    ConfigError,
    feature_enabled,
    load_models,
    load_routing_rules,
    load_settings,
    save_settings,
)
from config.resilience import CloudUnavailable, ModeController  # noqa: E402


@pytest.fixture()
def mc(monkeypatch) -> ModeController:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return ModeController(mode="auto", fail_max=3, reset_timeout_sec=1,
                          health_probe=lambda: True)


# ---- конфигурация --------------------------------------------------------


def test_configs_load_with_versions():
    settings = load_settings()
    models = load_models()
    rules = load_routing_rules()
    assert settings["version"] == "1.0"
    assert models["version"] == "1.0"
    assert rules["version"] == "1.0"


# ---- разнообразие Proposers ---------------------------------------------

# Отображение «модель -> разработчик». Считать разнообразие по названию модели
# недостаточно: qwen3 и qwen2.5-coder — разные архитектуры, но ОДИН
# разработчик (Alibaba), значит общий корпус обучения и общие слепые зоны.
# Именно из-за этого голосование 2 из 3 может подтвердить неверный ответ.
MODEL_VENDORS = {
    "qwen": "Alibaba",
    "ministral": "Mistral AI",
    "mistral": "Mistral AI",
    "gpt-oss": "OpenAI",
    "llama": "Meta",
    "gemma": "Google",
    "granite": "IBM",
    "nemotron": "NVIDIA",
    "glm": "Zhipu",
    "deepseek": "DeepSeek",
    "phi": "Microsoft",
}


def _vendor(model: str) -> str:
    """Разработчик модели по её имени; неизвестное имя считаем уникальным."""
    name = model.split("/")[-1].lower()
    for prefix, vendor in MODEL_VENDORS.items():
        if prefix in name:
            return vendor
    return f"неизвестный:{name}"


def test_proposer_count_is_odd():
    """Нечётное число Proposers: иначе при голосовании возможна ничья."""
    active = [p for p in load_models()["proposers"] if p.get("enabled", True)]
    assert len(active) >= 3, "Нужно минимум 3 активных Proposers"
    assert len(active) % 2 == 1, f"Число Proposers должно быть нечётным, сейчас {len(active)}"


def test_proposer_models_are_distinct():
    """Одна и та же модель дважды — это не ансамбль, а дубль голоса."""
    active = [p for p in load_models()["proposers"] if p.get("enabled", True)]
    models = [p["model"] for p in active]
    assert len(set(models)) == len(models), f"Модели дублируются: {models}"


def test_proposers_come_from_different_vendors():
    """
    Ключевое требование ансамбля: разные РАЗРАБОТЧИКИ, а не просто разные
    названия. Три модели одного вендора ошибаются одинаково, и консенсус
    2 из 3 подтвердит неверный ответ вместо того, чтобы поймать ошибку.
    """
    active = [p for p in load_models()["proposers"] if p.get("enabled", True)]
    vendors = [_vendor(p["model"]) for p in active]
    unique = set(vendors)
    detail = ", ".join(f"{p['model']} ({v})" for p, v in zip(active, vendors))
    # Минимум 2 разработчика обязательно; при 3 моделях стремимся к 3
    assert len(unique) >= 2, f"Все Proposers от одного разработчика: {detail}"
    assert len(unique) == len(active), (
        "Proposers должны быть от РАЗНЫХ разработчиков, иначе ошибки "
        f"коррелируют и голосование теряет смысл. Сейчас: {detail}"
    )


def test_verifier_is_not_used_as_proposer():
    """
    Регрессия: deepseek-v2:16b НЕ поддерживает tool calling (проверено —
    Ollama отвечает 'does not support tools'). В роли Verifier это допустимо,
    он лишь сверяет тексты, но в Proposers такая модель сломает вызовы.
    """
    models = load_models()
    no_tools = {"deepseek-v2"}
    for proposer in models["proposers"]:
        if not proposer.get("enabled", True):
            continue
        name = proposer["model"].lower()
        assert not any(bad in name for bad in no_tools), (
            f"{proposer['model']} не поддерживает tools — нельзя в Proposers"
        )
        assert proposer.get("supports_tools", True) is True


def test_cloud_judge_chain_has_fallback():
    """
    Резервный облачный арбитр обязателен: бесплатные тиры отдают 429 при
    перегрузке провайдера, а модели исчезают из каталога (deepseek-v3-0324:free
    был в конфиге, но на OpenRouter его больше нет).
    """
    judge = load_models()["judge"]
    assert judge.get("cloud"), "Не задан основной облачный арбитр"
    fallback = judge.get("cloud_fallbacks") or judge.get("cloud_fallback")
    assert fallback, "Не задан резервный облачный арбитр (judge.cloud_fallback)"
    assert judge.get("local_fallback"), "Не задан локальный арбитр"


def test_cloud_judges_are_from_different_vendors():
    """Резерв того же вендора упадёт вместе с основным — смысла в нём нет."""
    judge = load_models()["judge"]
    primary = judge["cloud"]["model"]
    fallback = judge.get("cloud_fallbacks") or judge.get("cloud_fallback")
    if isinstance(fallback, dict):
        fallback = [fallback]
    for item in fallback or []:
        assert _vendor(item["model"]) != _vendor(primary), (
            f"Резервный арбитр {item['model']} того же вендора, что основной {primary}"
        )


def test_unknown_config_version_raises(tmp_path: Path):
    bad = tmp_path / "settings.yaml"
    bad.write_text("version: '99.9'\nmode: auto\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_settings(bad)


def test_env_mode_overrides_file(monkeypatch):
    monkeypatch.setenv("AGENT_PLATFORM_MODE", "local_only")
    assert load_settings()["mode"] == "local_only"


def test_save_and_reload_settings(tmp_path: Path):
    settings = load_settings()
    settings["feature_flags"]["retrieval"] = True
    target = tmp_path / "settings.yaml"
    save_settings(settings, target)
    assert feature_enabled(load_settings(target), "retrieval") is True


# ---- режимы --------------------------------------------------------------


def test_local_only_blocks_cloud(mc: ModeController):
    mc.set_mode("local_only")
    status = mc.status()
    assert status.allowed is False
    assert "local_only" in status.reason
    with pytest.raises(CloudUnavailable):
        mc.call_cloud(lambda: "не должно выполниться")


def test_task_mode_can_only_tighten(mc: ModeController):
    assert mc.effective_mode("local_only") == "local_only"
    mc.set_mode("local_only")
    # Задача не может ослабить глобальный запрет
    assert mc.effective_mode("auto") == "local_only"


def test_invalid_mode_rejected(mc: ModeController):
    with pytest.raises(ValueError):
        mc.set_mode("cloud_only")


def test_missing_api_key_blocks_cloud(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Изолируемся от реального data/secrets.json пользователя
    import config.secrets as secrets_module
    monkeypatch.setattr(secrets_module, "STORE",
                        secrets_module.SecretStore(tmp_path / "secrets.json"))
    controller = ModeController(mode="auto", health_probe=lambda: True)
    assert controller.status().allowed is False
    assert "ключ" in controller.status().reason.lower()


def test_failed_health_check_blocks_cloud(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    controller = ModeController(mode="auto", health_timeout_sec=5, health_probe=lambda: False)
    status = controller.status()
    assert status.allowed is False and "Проверка связи" in status.reason


def test_health_result_is_cached(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    calls = {"n": 0}

    def probe() -> bool:
        calls["n"] += 1
        return True

    controller = ModeController(mode="auto", health_cache_sec=60, health_probe=probe)
    controller.health_check()
    controller.health_check()
    assert calls["n"] == 1, "Повторный health-check не должен ходить в сеть"
    controller.health_check(force=True)
    assert calls["n"] == 2


# ---- circuit breaker -----------------------------------------------------


def test_breaker_opens_after_fail_max(mc: ModeController):
    def boom():
        raise RuntimeError("сеть упала")

    for _ in range(mc.fail_max):
        with pytest.raises(CloudUnavailable):
            mc.call_cloud(boom)
    status = mc.status(check_health=False)
    assert status.breaker_state == "open"
    assert status.allowed is False and "Circuit Breaker" in status.reason


def test_breaker_recovers_after_reset_timeout(mc: ModeController):
    def boom():
        raise RuntimeError("сбой")

    for _ in range(mc.fail_max):
        with pytest.raises(CloudUnavailable):
            mc.call_cloud(boom)
    time.sleep(mc.reset_timeout_sec + 0.3)
    # half-open: пробный вызов проходит и закрывает breaker
    assert mc.call_cloud(lambda: "ок") == "ок"
    assert mc.status(check_health=False).breaker_state == "closed"


def test_manual_reset_and_open(mc: ModeController):
    mc.open_breaker()
    assert mc.status(check_health=False).allowed is False
    mc.reset_breaker()
    assert mc.status().allowed is True


def test_successful_call_passes_result_through(mc: ModeController):
    assert mc.call_cloud(lambda: {"answer": 42}) == {"answer": 42}


def test_status_is_serializable_for_ui(mc: ModeController):
    data = mc.status().as_dict()
    assert set(data) >= {"allowed", "reason", "breaker_state", "mode", "has_api_key"}
