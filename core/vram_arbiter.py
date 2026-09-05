"""
VRAM-арбитр (25.08, ночь): сериализация доступа к моделям по классам памяти.

Проблема: модели конкурируют за 8 ГБ VRAM. Загрузка heavy-модели (30B+) в
момент работы другой вытесняет её посреди шага -> зависания на десятки минут
(задачи 32eec6e0, d1d0e9c3). Промптовые правила не помогают — нужен
детерминированный арбитр.

Решение — семафор по классам памяти:
  tiny   (~2 ГБ): router qwen2.5:3b            — всегда разрешён
  medium (~5-16 ГБ): bge-m3, glm-flash, gpt-oss — всегда разрешён
  large  (~20-30 ГБ): nemotron-30b, qwen3.6-35b — exclusive lock
  xlarge (~26+ ГБ): Coder-Next 80B GGUF        — exclusive lock + пауза Ollama

Правила:
  - tiny/medium могут работать параллельно с чем угодно (маленький след)
  - large/xlarge берут эксклюзивный лок: пока работает одна heavy,
    другая heavy подождёт (вместо взаимного вытеснения)
  - перед вызовом xlarge выгружаются все модели Ollama (keep_alive=0),
    чтобы освободить максимум VRAM/RAM для GGUF-выгрузки

Плюс сбор метрик nvidia-smi вокруг каждого вызова — в Blackboard.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

VRAM_VERSION = "1.0"

# Классы памяти моделей (по размеру весов на диске/в RAM).
# Заполняется из models.yaml: поле memory_class у пропозера; дефолт — medium.
DEFAULT_CLASS = "medium"

_CLASSES = ("tiny", "medium", "large", "xlarge")
_EXCLUSIVE = {"large", "xlarge"}   # эти классы не работают одновременно


def classify(model_name: str, extra: dict[str, Any] | None = None) -> str:
    """Определить класс памяти модели по имени/конфигу."""
    name = (model_name or "").lower()
    explicit = (extra or {}).get("memory_class")
    if explicit:
        return str(explicit)
    if "coder-next" in name or "80b" in name:
        return "xlarge"
    for marker in ("nemotron", "35b", "30b", "qwen3.6"):
        if marker in name:
            return "large"
    if "3b" in name and "router" not in name:
        return "tiny"
    return DEFAULT_CLASS


class VRAMArbiter:
    """
    Эксклюзивный доступ к VRAM для heavy-моделей.

    Использование в gateway:
        with arbiter.slot(spec):
            response = call_model(...)

    - tiny/medium проходят без блокировки (кроме случая, когда heavy держит слот)
    - large/xlarge ждут освобождения эксклюзивного лока
    - перед первым входом xlarge выгружает модели Ollama (keep_alive=0)
    """

    version = VRAM_VERSION

    def __init__(self) -> None:
        self._lock = threading.RLock()          # защита внутреннего состояния
        self._exclusive = threading.Semaphore(1)  # слот для large/xlarge
        self._current_class: str | None = None

    # ---- диагностика -----------------------------------------------------

    @staticmethod
    def gpu_stats() -> dict[str, Any] | None:
        """Мгновенный снимок GPU через nvidia-smi. None если недоступен."""
        try:
            r = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=memory.used,memory.total,temperature.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=10)
            used, total, temp = [int(x.strip()) for x in
                                 r.stdout.strip().split(",")]
            return {"vram_used_mb": used, "vram_total_mb": total,
                    "gpu_temp_c": temp}
        except Exception:  # noqa: BLE001 — нет nvidia-smi / таймаут
            return None

    @staticmethod
    def ollama_loaded() -> list[str]:
        """Список загруженных в Ollama моделей (пусто = ничего не держит)."""
        import urllib.request
        try:
            with urllib.request.urlopen(
                    "http://127.0.0.1:11434/api/ps", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return [m.get("name") or m.get("model") or ""
                    for m in data.get("models") or []]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def unload_ollama_models() -> list[str]:
        """Выгрузить все модели Ollama (keep_alive=0). Возвращает имена."""
        import urllib.request
        unloaded: list[str] = []
        for name in VRAMArbiter.ollama_loaded():
            try:
                req = urllib.request.Request(
                    "http://127.0.0.1:11434/api/generate",
                    data=json.dumps({"model": name,
                                     "keep_alive": 0}).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=60)
                unloaded.append(name)
            except Exception:  # noqa: BLE001
                pass
        return unloaded

    # ---- основной API ------------------------------------------------------

    def acquire(self, spec_class: str | None, model_name: str,
                on_event=None) -> dict[str, Any] | None:
        """
        Захватить слот перед вызовом модели.
        Возвращает info-словарь (метрики до вызова) или None для лёгких моделей.
        Блокирует, если класс exclusive и слот занят.
        """
        cls = spec_class or classify(model_name)
        if cls not in _EXCLUSIVE:
            return None

        info: dict[str, Any] = {"model": model_name, "class": cls}
        self._exclusive.acquire()
        self._current_class = cls
        before = self.gpu_stats()
        if before:
            info["gpu_before"] = before

        # xlarge требует максимум памяти: выгружаем всё, что висит в Ollama
        if cls == "xlarge":
            loaded = self.ollama_loaded()
            if loaded:
                unloaded = self.unload_ollama_models()
                info["unloaded"] = unloaded
                if on_event:
                    on_event({"kind": "vram_arbitration",
                              "action": "unload_for_xlarge",
                              "unloaded": unloaded})
                time.sleep(2.0)  # дать драйверу освободить память

        if on_event:
            on_event({"kind": "vram_slot_acquired", **info})
        return info

    def release(self, info: dict[str, Any] | None,
                on_event=None) -> dict[str, Any] | None:
        """Освободить слот. Возвращает метрики после вызова."""
        after = self.gpu_stats()
        if info is not None:
            info["gpu_after"] = after
            if on_event:
                on_event({"kind": "vram_slot_released", **{
                    k: info.get(k) for k in ("model", "class")}})
                if info.get("gpu_before") and after:
                    info["delta_vram_mb"] = (after["vram_used_mb"]
                                             - info["gpu_before"]["vram_used_mb"])
        with self._lock:
            self._current_class = None
        self._exclusive.release()
        return {"gpu_after": after} if info is not None else None


# Модульный singleton: один арбитр на процесс платформы.
ARBITER = VRAMArbiter()
