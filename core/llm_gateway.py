"""
Единая точка вызова любых моделей (локальных и облачных).

Зачем отдельный слой:
  - агенты не знают, куда именно уходит запрос (Ollama или OpenRouter);
  - добавление модели = запись в config/models.yaml, без правки кода;
  - облако вызывается ТОЛЬКО через ModeController (Circuit Breaker + health),
    поэтому «отвал облака» никогда не роняет задачу;
  - разбор tool calls единый (core.tool_parsing), включая модели, которые
    печатают вызов текстом.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from config.resilience import CloudUnavailable, ModeController
from core.schemas import ToolCallRequest
from core.tool_parsing import extract_tool_calls

GATEWAY_VERSION = "1.0"


class ModelCallError(RuntimeError):
    """Ошибка вызова модели (локальной или облачной)."""


@dataclass
class ModelSpec:
    """Описание модели из config/models.yaml. Единый вид для всех провайдеров."""

    id: str
    provider: str
    model: str
    temperature: float = 0.2
    num_ctx: int = 8192
    enabled: bool = True
    supports_tools: bool = True
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any], default_id: str = "") -> "ModelSpec":
        known = {"id", "provider", "model", "temperature", "num_ctx",
                 "enabled", "supports_tools"}
        return cls(
            id=str(data.get("id") or default_id or data.get("model", "model")),
            provider=str(data.get("provider", "ollama")),
            model=str(data["model"]),
            temperature=float(data.get("temperature", 0.2)),
            num_ctx=int(data.get("num_ctx", 8192)),
            enabled=bool(data.get("enabled", True)),
            supports_tools=bool(data.get("supports_tools", True)),
            extra={k: v for k, v in data.items() if k not in known},
        )


@dataclass
class LLMResponse:
    """Унифицированный ответ модели."""

    text: str = ""
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    model: str = ""
    provider: str = "ollama"
    latency_ms: int = 0
    ok: bool = True
    error: str | None = None
    raw_message: dict[str, Any] = field(default_factory=dict)


# Оллама отвечает 500 с текстом tool 'files__read_csv' not found, когда
# модель сама придумала имя функции. Это не сбой инфраструктуры,
# а ошибка модели — её надо вернуть ей же, а не ронять шаг.
_UNKNOWN_TOOL_RE = re.compile(r"tool\s+'([^']+)'\s+not found", re.IGNORECASE)


def _unknown_tool_name(exc: Exception) -> str:
    """Имя несуществующего инструмента из ошибки провайдера или пусто."""
    match = _UNKNOWN_TOOL_RE.search(str(exc))
    return match.group(1) if match else ""


class LLMGateway:
    """
    Шлюз к моделям.

    chat() принимает ModelSpec, сообщения и (необязательно) схемы инструментов;
    возвращает LLMResponse. Ошибка возвращается в объекте, а не исключением, —
    чтобы падение одной модели не обрывало ансамбль Proposers.
    """

    def __init__(self, models_config: dict[str, Any], mode_controller: ModeController):
        self.config = models_config
        self.mc = mode_controller
        providers = models_config.get("providers") or {}
        self.ollama_cfg = providers.get("ollama") or {}
        self.openrouter_cfg = providers.get("openrouter") or {}
        self._ollama_client = None
        self._openrouter_client = None
        # Опциональный приёмник событий (для VRAM-арбитра и диагностики).
        self.event_sink = None

    def set_event_sink(self, sink) -> None:
        """Подключить приёмник событий (напр. platform.emit)."""
        self.event_sink = sink

    # ---- реестр моделей из конфига ---------------------------------------

    def spec(self, role: str) -> ModelSpec:
        """Модель одной роли: router / verifier / embeddings."""
        data = self.config.get(role)
        if not data:
            raise ModelCallError(f"В models.yaml нет роли '{role}'")
        return ModelSpec.from_dict(data, default_id=role)

    def proposer_specs(self, include_cloud: bool = False) -> list[ModelSpec]:
        """Активные Proposers. Порядок из конфига сохраняется."""
        specs = [ModelSpec.from_dict(item) for item in (self.config.get("proposers") or [])]
        if include_cloud:
            specs += [ModelSpec.from_dict(i) for i in (self.config.get("cloud_proposers") or [])]
        return [s for s in specs if s.enabled]

    def lead_agent_extra_specs(self) -> list[ModelSpec]:
        """Тяжёлые модели только для роли Lead-исполнителя (lead_agent_extra).
        Не участвуют в голосовании пропозеров: чётность и разнообразие
        семейств ансамбля не нарушаются."""
        specs = [ModelSpec.from_dict(item)
                 for item in (self.config.get("lead_agent_extra") or [])]
        return [s for s in specs if s.enabled]

    def orchestrator_spec(self) -> ModelSpec:
        """
        Локальный планировщик режима «Оркестратор».

        Отдельная роль, а не judge.local_fallback: планировщику важна скорость
        (замерено 05.08: qwen3:8b 26 с против 69 с у плотной 14B), а арбитру —
        качество рассуждений. Раньше они делили одну модель, и ускорить
        планирование было нельзя, не ослабив арбитра.

        Если роли нет в конфиге, честно падаем на арбитра — старые конфиги
        продолжают работать без правок.
        """
        data = (self.config.get("orchestrator") or {}).get("local")
        if data:
            return ModelSpec.from_dict(data, default_id="orchestrator_local")
        return self.judge_specs()[1]

    def orchestrator_candidates(self) -> list[ModelSpec]:
        """
        Модели, которые пользователь может выбрать планировщиком.

        Берутся из `orchestrator.candidates` в models.yaml. Список задаётся
        конфигом, а не кодом: добавить модель — значит дописать запись.
        Если блока нет, остаётся одна модель по умолчанию — старые конфиги
        продолжают работать.
        """
        raw = (self.config.get("orchestrator") or {}).get("candidates") or []
        specs = [ModelSpec.from_dict(item, default_id="orchestrator_local")
                 for item in raw if item.get("model")]
        if not specs:
            return [self.orchestrator_spec()]
        return specs

    def orchestrator_spec_for(self, model: str | None) -> ModelSpec:
        """
        Планировщик, выбранный пользователем.

        Неизвестное или пустое имя молча даёт модель по умолчанию: выбор
        мог остаться в настройках от конфига, где такой модели уже нет,
        и отменять из-за этого задачу нельзя.
        """
        if not model:
            return self.orchestrator_spec()
        for spec in self.orchestrator_candidates():
            if spec.model == model:
                return spec
        return self.orchestrator_spec()

    def judge_specs(self) -> tuple[ModelSpec | None, ModelSpec]:
        """(облачный судья или None, локальный fallback) — приоритет облаку."""
        judge = self.config.get("judge") or {}
        cloud = judge.get("cloud")
        local = judge.get("local_fallback")
        if not local:
            raise ModelCallError("В models.yaml не задан judge.local_fallback")
        cloud_spec = ModelSpec.from_dict(cloud, default_id="judge_cloud") if cloud else None
        return cloud_spec, ModelSpec.from_dict(local, default_id="judge_local")

    def cloud_judge_chain(self) -> list[ModelSpec]:
        """
        Цепочка облачных арбитров: основной, затем резервные.

        Зачем: бесплатные тиры отдают 429 при перегрузке провайдера (проверено
        на gemma-4), а модели вообще исчезают из каталога — deepseek-v3-0324:free
        был в конфиге, но на OpenRouter его больше нет. Резерв ИНОГО семейства
        не падает вместе с основным.

        Поддерживаются ключи judge.cloud_fallback (объект) и judge.cloud_fallbacks
        (список). Если их нет — поведение прежнее, ровно один облачный арбитр.
        """
        judge = self.config.get("judge") or {}
        chain: list[ModelSpec] = []
        primary = judge.get("cloud")
        if primary:
            chain.append(ModelSpec.from_dict(primary, default_id="judge_cloud"))
        extra = judge.get("cloud_fallbacks") or judge.get("cloud_fallback")
        if isinstance(extra, dict):
            extra = [extra]
        for i, item in enumerate(extra or []):
            if isinstance(item, dict) and item.get("model"):
                chain.append(ModelSpec.from_dict(item, default_id=f"judge_cloud_{i + 2}"))
        # Дубли по имени модели бессмысленны: та же модель упадёт так же
        seen: set[str] = set()
        unique: list[ModelSpec] = []
        for spec in chain:
            if spec.model not in seen:
                seen.add(spec.model)
                unique.append(spec)
        return unique

    # ---- клиенты ---------------------------------------------------------

    def _ollama(self):
        if self._ollama_client is None:
            try:
                import ollama
            except ImportError as exc:
                raise ModelCallError("Не установлен пакет ollama") from exc
            host = self.ollama_cfg.get("base_url", "http://127.0.0.1:11434")
            self._ollama_client = ollama.Client(host=host)
        return self._ollama_client

    def _openrouter(self):
        if self._openrouter_client is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise ModelCallError("Не установлен пакет openai (нужен для OpenRouter)") from exc
            from config.secrets import STORE
            key_env = self.openrouter_cfg.get("api_key_env", "OPENROUTER_API_KEY")
            # STORE учитывает и окружение, и ключ, вписанный в интерфейсе
            api_key = STORE.get(key_env)
            if not api_key:
                raise ModelCallError(
                    "Нет ключа облака. Впишите его на экране «Ключи доступа» "
                    f"в панели управления или задайте переменную {key_env}"
                )
            self._openrouter_client = OpenAI(
                base_url=self.openrouter_cfg.get("base_url", "https://openrouter.ai/api/v1"),
                api_key=api_key,
                timeout=float(self.openrouter_cfg.get("timeout_sec", 120)),
                default_headers={
                    # OpenRouter просит эти заголовки; без них часть моделей отвечает 401/403
                    "HTTP-Referer": "http://localhost",
                    "X-Title": str(self.openrouter_cfg.get("app_title", "agent-platform")),
                },
            )
        return self._openrouter_client

    # ---- главный метод ---------------------------------------------------

    TOOL_HINT = (
        "\n\nЕСЛИ нужны данные из инструментов — ОБЯЗАТЕЛЬНО вызови инструмент, "
        "не выдумывай значения. Если механизм функций недоступен, верни ТОЛЬКО JSON "
        'вида {"name":"<имя_функции>","arguments":{...}} без пояснений и без текста вокруг.'
    )

    def chat(self, spec, messages, tools=None, task_mode=None,
             timeout_sec=None):
        """VRAM-арбитр: heavy-модели не работают одновременно."""
        from core.vram_arbiter import ARBITER, classify

        sink = self.event_sink or (lambda e: None)

        slot = ARBITER.acquire(
            classify(spec.model, getattr(spec, "extra", None)),
            spec.model, on_event=sink)
        try:
            response = self._chat_dispatch(spec, messages, tools, task_mode,
                                           timeout_sec)
        finally:
            if slot:
                ARBITER.release(slot, on_event=sink)
        # Журнал LLM-вызовов: tps/latency по моделям (в БД, не в событийный поток).
        try:
            raw = getattr(response, "raw_message", None) or {}
            eval_count = int(raw.get("_eval_count", 0) or 0)
            eval_dur_ns = int(raw.get("_eval_duration", 0) or 0)
            eval_dur_ms = eval_dur_ns // 1_000_000 if eval_dur_ns else 0  # нс -> мс
            tps = round(eval_count / (eval_dur_ns / 1e9), 1) if eval_dur_ns else 0.0
            self._log_llm_call(
                model=spec.model, provider=spec.provider,
                ok=bool(getattr(response, "ok", False)),
                latency_ms=int(getattr(response, "latency_ms", 0) or 0),
                eval_count=eval_count, eval_duration_ms=eval_dur_ms,
                prompt_eval_count=int(raw.get("_prompt_eval_count", 0) or 0))
            if tps:  # live tps для панели
                sink({"kind": "llm_tps", "model": spec.model,
                      "tps": tps, "latency_ms": int(getattr(response, "latency_ms", 0) or 0),
                      "eval_count": eval_count})
        except Exception:  # noqa: BLE001
            pass
        return response

    def _log_llm_call(self, **kw) -> None:
        """Отложенная запись в Blackboard (импорт внутри — циклическая зависимость)."""
        try:
            from core.blackboard import Blackboard
            bb = getattr(self, "_llm_log_bb", None)
            if bb is None:
                import os as _os
                bb = Blackboard(_os.path.join(
                    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                    "data", "blackboard.sqlite3"))
                self._llm_log_bb = bb
            bb.log_llm_call(**kw)
        except Exception:  # noqa: BLE001
            pass

    def _chat_dispatch(
        self,
        spec: ModelSpec,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        task_mode: str | None = None,
        timeout_sec: int | None = None,
    ) -> LLMResponse:
        """Вызов модели. Никогда не бросает — ошибка приходит в LLMResponse."""
        started = time.perf_counter()
        # Аварийный стоп: флаг-файл кладёт UI при нажатии «Стоп».
        # Проверяем ДО каждого вызова модели — текущий вызов доиграет,
        # следующий вернёт ошибку остановки.
        _flag = Path(__file__).resolve().parent.parent / "data" / "stop_flag"
        if _flag.is_file():
            try:
                _flag.unlink()
            except OSError:
                pass
            return LLMResponse(model=spec.model, provider=spec.provider, ok=False,
                               error="Остановлено пользователем (stop_flag)",
                               latency_ms=int((time.perf_counter() - started) * 1000))
        known = {t["function"]["name"] for t in (tools or [])}
        payload = list(messages)
        if tools and payload and payload[0].get("role") == "system":
            # Подсказка про формат вызова: без неё модели без нативного tool
            # calling просто отвечают прозой (проверено на glm4 и qwen2.5-coder).
            payload[0] = dict(payload[0])
            payload[0]["content"] = str(payload[0]["content"]) + self.TOOL_HINT

        try:
            if spec.provider == "ollama":
                message = self._chat_ollama(spec, payload, tools, timeout_sec)
            elif spec.provider == "llamacpp":
                message = self._chat_llamacpp(spec, payload, tools, timeout_sec)
            elif spec.provider == "openrouter":
                message = self._chat_openrouter(spec, payload, tools, task_mode, timeout_sec)
            else:
                raise ModelCallError(f"Неизвестный провайдер: {spec.provider}")
        except CloudUnavailable as exc:
            return LLMResponse(model=spec.model, provider=spec.provider, ok=False,
                               error=f"Облако недоступно: {exc}",
                               latency_ms=int((time.perf_counter() - started) * 1000))
        except Exception as exc:  # noqa: BLE001
            unknown = _unknown_tool_name(exc)
            if unknown:
                # Модель сгенерировала вызов несуществующего действия
                # (напр. files__read_csv), и Ollama ответила 500 на ВЕСЬ запрос.
                # Раньше из-за этого падал весь шаг плана, хотя предыдущие
                # шаги уже добыли данные (случай 06.08 в журнале).
                # Ошибка — это данные: возвращаем её модели текстом,
                # чтобы она выбрала действие из выданного списка.
                available = ", ".join(sorted(known)) or "(инструменты не выданы)"
                return LLMResponse(
                    text=(f"ОШИБКА: действия «{unknown}» не существует. "
                          f"Доступны только: {available}. "
                          f"Повтори вызов, выбрав действие из списка, "
                          f"и не придумывай данные."),
                    model=spec.model, provider=spec.provider, ok=True,
                    latency_ms=int((time.perf_counter() - started) * 1000),
                    raw_message={"unknown_tool": unknown},
                )
            return LLMResponse(model=spec.model, provider=spec.provider, ok=False,
                               error=f"{type(exc).__name__}: {exc}",
                               latency_ms=int((time.perf_counter() - started) * 1000))

        calls = extract_tool_calls(message, known or None) if tools else []
        return LLMResponse(
            text=str(message.get("content") or ""),
            tool_calls=calls,
            model=spec.model,
            provider=spec.provider,
            latency_ms=int((time.perf_counter() - started) * 1000),
            ok=True,
            raw_message=message,
        )

    def _chat_ollama(self, spec, messages, tools, timeout_sec) -> dict[str, Any]:
        client = self._ollama()
        options = {"temperature": spec.temperature, "num_ctx": spec.num_ctx}
        # keep_alive держит модель в памяти между вызовами: на 8 ГБ VRAM повторная
        # загрузка стоит 5-13 секунд и съедает большую часть времени ответа.
        kwargs: dict[str, Any] = {"model": spec.model, "messages": messages, "options": options}
        keep_alive = spec.extra.get("keep_alive")
        if keep_alive:
            kwargs["keep_alive"] = keep_alive
        # Модели с режимом размышления (Qwen3 и др.) тратят на него десятки
        # секунд. Проверено: для вызова инструментов точность не падает (3/3),
        # а скорость вырастает с 15.4 с до 1.4 с.
        if "think" in spec.extra:
            kwargs["think"] = bool(spec.extra["think"])
        if tools and spec.supports_tools:
            kwargs["tools"] = tools
        response = client.chat(**kwargs)
        message = response.get("message") if isinstance(response, dict) else response.message
        message = message if isinstance(message, dict) else dict(message)
        # eval-статистика для tps-лога. Ollama возвращает pydantic ChatResponse:
        # isinstance(response, dict) = False, поэтому читаем И ключи, И атрибуты.
        for k in ("eval_count", "eval_duration", "prompt_eval_count", "prompt_eval_duration"):
            v = response.get(k) if isinstance(response, dict) else getattr(response, k, None)
            if v:
                message[f"_{k}"] = v
        return message

    @staticmethod
    def _to_openai_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """
        Привести историю к формату OpenAI/OpenRouter.

        Различие форматов, из-за которого падали облачные вызовы:
          - Ollama требует arguments СЛОВАРЁМ, OpenAI — JSON-СТРОКОЙ;
          - у OpenAI каждый tool_call обязан иметь id, а ответ роли tool —
            ссылаться на него через tool_call_id.
        """
        converted: list[dict[str, Any]] = []
        last_ids: list[str] = []
        for message in messages:
            item = dict(message)
            if item.get("role") == "assistant" and item.get("tool_calls"):
                calls = []
                last_ids = []
                for i, call in enumerate(item["tool_calls"]):
                    function = dict((call or {}).get("function") or {})
                    args = function.get("arguments")
                    if not isinstance(args, str):
                        function["arguments"] = json.dumps(args or {}, ensure_ascii=False)
                    call_id = str(call.get("id") or f"call_{len(converted)}_{i}")
                    last_ids.append(call_id)
                    calls.append({"id": call_id, "type": "function", "function": function})
                item["tool_calls"] = calls
                item.setdefault("content", "")
            elif item.get("role") == "tool":
                if "tool_call_id" not in item:
                    item["tool_call_id"] = last_ids.pop(0) if last_ids else "call_0"
            converted.append(item)
        return converted

    def _chat_llamacpp(self, spec, messages, tools, timeout_sec) -> dict[str, Any]:
        """llama-server (llama.cpp): OpenAI-совместимый /v1/chat/completions.
        Для моделей вне Ollama — напр. Qwen3-Coder-Next 80B (GGUF, RAM+VRAM
        offload). Адрес берётся из spec.extra["base_url"] или дефолт :8080."""
        import json as _json
        import urllib.request
        base = (spec.extra or {}).get("base_url", "http://127.0.0.1:8080")
        url = base.rstrip("/") + "/v1/chat/completions"
        payload: dict[str, Any] = {
            "model": (spec.extra or {}).get("server_model", spec.model),
            "messages": [{"role": m["role"], "content": str(m["content"])}
                         for m in messages],
            "temperature": spec.temperature,
            "stream": False,
        }
        if tools and getattr(spec, "supports_tools", False):
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        req = urllib.request.Request(url, data=_json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        started = time.perf_counter()
        with urllib.request.urlopen(req, timeout=timeout_sec or 600) as resp:
            data = _json.loads(resp.read().decode("utf-8"))
        msg = data["choices"][0]["message"]
        calls = [{"function": {"name": c["function"]["name"],
                               "arguments": c["function"]["arguments"]}}
                 for c in (msg.get("tool_calls") or [])
                 if isinstance(c.get("function"), dict)]
        out = {"content": msg.get("content") or "", "tool_calls": calls}
        # usage -> tps-лог (llama-server: completion_tokens / completion_ms)
        usage = data.get("usage") or {}
        if usage.get("completion_tokens"):
            out["_eval_count"] = usage["completion_tokens"]
            ms = usage.get("completion_ms") or usage.get("completion_time")
            if ms:
                out["_eval_duration"] = int(ms) * 1_000_000  # мс -> нс (как у Ollama)
        return out

    def _chat_openrouter(self, spec, messages, tools, task_mode, timeout_sec) -> dict[str, Any]:
        """Облачный вызов строго через ModeController (breaker + health-check)."""
        payload = self._to_openai_messages(messages)

        def do_call() -> dict[str, Any]:
            client = self._openrouter()
            kwargs: dict[str, Any] = {
                "model": spec.model,
                "messages": payload,
                "temperature": spec.temperature,
            }
            if tools and spec.supports_tools:
                kwargs["tools"] = tools
                kwargs["tool_choice"] = "auto"
            completion = client.chat.completions.create(**kwargs)
            choice = completion.choices[0].message
            calls = []
            for call in (choice.tool_calls or []):
                calls.append({"function": {"name": call.function.name,
                                           "arguments": call.function.arguments}})
            return {"content": choice.content or "", "tool_calls": calls}

        return self.mc.call_cloud(do_call, task_mode=task_mode)

    # ---- embeddings ------------------------------------------------------

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Локальные эмбеддинги через Ollama (роль embeddings в models.yaml)."""
        spec = self.spec("embeddings")
        client = self._ollama()
        vectors: list[list[float]] = []
        for text in texts:
            response = client.embeddings(model=spec.model, prompt=text)
            vector = response.get("embedding") if isinstance(response, dict) else response.embedding
            vectors.append(list(vector))
        return vectors

    # ---- диагностика для UI ---------------------------------------------

    def local_models_available(self) -> dict[str, bool]:
        """Какие модели из конфига реально скачаны в Ollama (для экрана «Настройки»)."""
        try:
            data = self._ollama().list()
        except Exception:  # noqa: BLE001
            return {}
        installed = set()
        for item in (data.get("models") if isinstance(data, dict) else data.models) or []:
            name = item.get("model") if isinstance(item, dict) else getattr(item, "model", None)
            if name:
                installed.add(str(name))
        wanted: list[str] = []
        for role in ("router", "verifier", "embeddings"):
            if self.config.get(role):
                wanted.append(str(self.config[role]["model"]))
        for group in ("proposers",):
            for item in self.config.get(group) or []:
                wanted.append(str(item["model"]))
        # Оркестратор — тоже локальная модель, показываем в статусе
        orch_local = ((self.config.get("orchestrator") or {}).get("local") or {}).get("model")
        if orch_local:
            wanted.append(str(orch_local))
        local_judge = ((self.config.get("judge") or {}).get("local_fallback") or {}).get("model")
        if local_judge:
            wanted.append(str(local_judge))
        return {name: name in installed for name in dict.fromkeys(wanted)}
