"""
Mode Controller + Circuit Breaker (слой 0).

Единственное место, которое решает вопрос «можно ли сейчас в облако?».
Все остальные компоненты обязаны спрашивать здесь, а не проверять сеть сами.

Три независимых причины отказа от облака:
  1. mode = local_only (политика конфиденциальности) — жёсткий запрет;
  2. нет API-ключа провайдера;
  3. Circuit Breaker открыт после fail_max сбоев ИЛИ health-check не прошёл.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import pybreaker

from config.loader import load_models, load_settings

RESILIENCE_VERSION = "1.0"


class CloudUnavailable(RuntimeError):
    """Облако недоступно. Ловится вызывающим кодом для перехода на локальный путь."""


@dataclass
class CloudStatus:
    """Снимок состояния облака — то, что показывает UI на «Панели управления»."""

    allowed: bool
    reason: str
    breaker_state: str = "closed"
    fail_counter: int = 0
    mode: str = "auto"
    has_api_key: bool = False
    last_health_ok: bool | None = None
    last_health_at: float = 0.0
    last_error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "breaker_state": self.breaker_state,
            "fail_counter": self.fail_counter,
            "mode": self.mode,
            "has_api_key": self.has_api_key,
            "last_health_ok": self.last_health_ok,
            "last_error": self.last_error,
        }


class _BreakerListener(pybreaker.CircuitBreakerListener):
    """Слушатель breaker'а: сохраняет последнюю ошибку и переходы состояний."""

    def __init__(self) -> None:
        self.last_error: str | None = None
        self.transitions: list[tuple[float, str, str]] = []
        self.opened_at: float | None = None

    def failure(self, cb, exc):  # noqa: ANN001, D102
        self.last_error = f"{type(exc).__name__}: {exc}"

    def state_change(self, cb, old, new):  # noqa: ANN001, D102
        old_name = getattr(old, "name", str(old))
        new_name = getattr(new, "name", str(new))
        self.transitions.append((time.time(), old_name, new_name))
        # Момент открытия нужен, чтобы понимать, когда пора пробовать снова
        self.opened_at = time.time() if new_name == "open" else None


@dataclass
class ModeController:
    """
    Контроллер режима работы.

    Использование:
        mc = ModeController.from_config()
        if mc.cloud_available():
            result = mc.call_cloud(lambda: client.chat(...))
        else:
            result = local_fallback()
    """

    mode: str = "auto"
    fail_max: int = 3
    reset_timeout_sec: int = 30
    health_timeout_sec: int = 5
    health_cache_sec: int = 20
    health_url: str = "https://openrouter.ai/api/v1/models"
    api_key_env: str = "OPENROUTER_API_KEY"
    # Инъекция для тестов: функция health-check, возвращающая bool
    health_probe: Callable[[], bool] | None = None

    _breaker: pybreaker.CircuitBreaker = field(init=False, repr=False)
    _listener: _BreakerListener = field(init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _health_ok: bool | None = field(default=None, init=False)
    _health_at: float = field(default=0.0, init=False)
    # Пояснение причины провала health-check — показывается в интерфейсе
    _health_detail: str = field(default="", init=False)

    def __post_init__(self) -> None:
        self._listener = _BreakerListener()
        self._breaker = pybreaker.CircuitBreaker(
            fail_max=self.fail_max,
            reset_timeout=self.reset_timeout_sec,
            listeners=[self._listener],
            name="cloud",
        )

    # ---- создание из конфига --------------------------------------------

    @classmethod
    def from_config(
        cls,
        settings: dict[str, Any] | None = None,
        models: dict[str, Any] | None = None,
        health_probe: Callable[[], bool] | None = None,
    ) -> "ModeController":
        settings = settings if settings is not None else load_settings()
        models = models if models is not None else load_models()
        res = settings.get("resilience") or {}
        openrouter = ((models.get("providers") or {}).get("openrouter") or {})
        return cls(
            mode=str(settings.get("mode", "auto")),
            fail_max=int(res.get("fail_max", 3)),
            reset_timeout_sec=int(res.get("reset_timeout_sec", 30)),
            health_timeout_sec=int(res.get("health_timeout_sec", 5)),
            health_cache_sec=int(res.get("health_cache_sec", 20)),
            health_url=str(res.get("health_url") or f"{openrouter.get('base_url', '')}/models"),
            api_key_env=str(openrouter.get("api_key_env", "OPENROUTER_API_KEY")),
            health_probe=health_probe,
        )

    # ---- режим ----------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Переключение режима из UI. local_only = жёсткий офлайн."""
        if mode not in ("auto", "local_only"):
            raise ValueError(f"Неизвестный режим: {mode}")
        with self._lock:
            self.mode = mode

    @property
    def local_only(self) -> bool:
        return self.mode == "local_only"

    def effective_mode(self, task_mode: str | None = None) -> str:
        """
        Итоговый режим задачи: побеждает БОЛЕЕ строгий.
        Пользователь может ужесточить режим для конкретной задачи,
        но не может ослабить глобальный local_only.
        """
        if self.local_only or task_mode == "local_only":
            return "local_only"
        return "auto"

    # ---- ключ и health --------------------------------------------------

    def has_api_key(self) -> bool:
        return bool(self._api_key())

    def _api_key(self) -> str:
        """
        Ключ облака: сначала переменная окружения, затем ключ, вписанный
        в интерфейсе (data/secrets.json). Терминал больше не обязателен.
        """
        from config.secrets import STORE
        return STORE.get(self.api_key_env)

    def _default_probe(self) -> bool:
        """Health-check с таймаутом. Без httpx — считаем облако недоступным."""
        try:
            import httpx
        except ImportError:
            return False
        headers = {}
        key = self._api_key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        try:
            response = httpx.get(self.health_url, timeout=self.health_timeout_sec, headers=headers)
        except Exception as exc:  # noqa: BLE001 — любая сетевая ошибка = облака нет
            self._health_detail = f"сеть недоступна ({type(exc).__name__})"
            return False
        # ВАЖНО: 401/403 — это неверный ключ, а не «облако живо».
        # Проверено: эндпоинт /models отвечает 200 вообще без ключа,
        # поэтому для валидации нужен /key.
        if response.status_code in (401, 403):
            self._health_detail = "ключ отклонён сервисом (проверьте правильность)"
            return False
        if response.status_code >= 500:
            self._health_detail = f"сервис ответил {response.status_code}"
            return False
        self._health_detail = ""
        return True

    def health_check(self, force: bool = False) -> bool:
        """Проверка живости облака с кэшем на health_cache_sec."""
        now = time.time()
        with self._lock:
            fresh = self._health_ok is not None and (now - self._health_at) < self.health_cache_sec
            if fresh and not force:
                return bool(self._health_ok)
        probe = self.health_probe or self._default_probe
        ok = bool(probe())
        with self._lock:
            self._health_ok = ok
            self._health_at = time.time()
        return ok

    # ---- главный вопрос -------------------------------------------------

    def cloud_available(self, task_mode: str | None = None, check_health: bool = True) -> bool:
        return self.status(task_mode=task_mode, check_health=check_health).allowed

    def status(self, task_mode: str | None = None, check_health: bool = True) -> CloudStatus:
        """Полный ответ «почему можно/нельзя» — для UI и логов."""
        mode = self.effective_mode(task_mode)
        base = CloudStatus(
            allowed=False,
            reason="",
            breaker_state=self._breaker.current_state,
            fail_counter=self._breaker.fail_counter,
            mode=mode,
            has_api_key=self.has_api_key(),
            last_health_ok=self._health_ok,
            last_health_at=self._health_at,
            last_error=self._listener.last_error,
        )
        if mode == "local_only":
            base.reason = "Режим local_only: работа только с локальными моделями"
            return base
        if not base.has_api_key:
            base.reason = (
                "Не задан ключ облака. Впишите его на экране «Ключи доступа» "
                f"или задайте переменную окружения {self.api_key_env}"
            )
            return base
        if self._breaker.current_state == "open":
            # ВАЖНО: pybreaker переходит в half-open только при попытке вызова.
            # Если решать по current_state, облако не восстановится никогда:
            # status() запретит вызов, а без вызова состояние не сменится.
            # Поэтому по истечении reset_timeout разрешаем ПРОБНЫЙ вызов.
            elapsed = time.time() - self._opened_at if self._opened_at else 0.0
            if self._opened_at and elapsed >= self.reset_timeout_sec:
                base.allowed = True
                base.breaker_state = "half-open"
                base.reason = "Circuit Breaker: пробная попытка восстановления облака"
                return base
            remaining = max(0, int(self.reset_timeout_sec - elapsed))
            base.reason = (
                f"Circuit Breaker открыт после {self._breaker.fail_counter} сбоев; "
                f"повторная попытка через ~{remaining} с"
            )
            return base
        if check_health:
            ok = self.health_check()
            base.last_health_ok = ok
            base.last_health_at = self._health_at
            if not ok:
                detail = self._health_detail or f"нет ответа за {self.health_timeout_sec} с"
                base.reason = f"Проверка связи с облаком не прошла: {detail}"
                return base
        base.allowed = True
        base.reason = "Облако доступно"
        return base

    # ---- защищённый вызов ----------------------------------------------

    def call_cloud(self, func: Callable[[], Any], task_mode: str | None = None) -> Any:
        """
        Вызов облака под защитой Circuit Breaker.

        Бросает CloudUnavailable — вызывающий код обязан иметь локальный путь.
        Успешный вызов закрывает breaker, сбойный увеличивает счётчик.
        """
        state = self.status(task_mode=task_mode)
        if not state.allowed:
            raise CloudUnavailable(state.reason)
        try:
            return self._breaker.call(func)
        except pybreaker.CircuitBreakerError as exc:
            raise CloudUnavailable(f"Circuit Breaker: {exc}") from exc
        except Exception as exc:  # noqa: BLE001
            raise CloudUnavailable(f"Ошибка облачного вызова: {type(exc).__name__}: {exc}") from exc

    # ---- обслуживание ---------------------------------------------------

    @property
    def _opened_at(self) -> float | None:
        return self._listener.opened_at

    def reset_breaker(self) -> None:
        """Кнопка «Сбросить Circuit Breaker» на панели управления."""
        self._breaker.close()
        self._listener.last_error = None
        self._listener.opened_at = None

    def open_breaker(self) -> None:
        """Принудительно отключить облако (кнопка в UI)."""
        self._breaker.open()

    @property
    def breaker_transitions(self) -> list[tuple[float, str, str]]:
        return list(self._listener.transitions)
