"""
Router — выбирает модель для задачи.

При инициализации:
  1. Загружает routing_rules из models.json (тип задачи → tier)
  2. Запрашивает у каждого настроенного провайдера актуальный список моделей
     (Groq, OpenRouter, Cerebras, Ollama) — через model_discovery
  3. Если discovery недоступен — fallback на статичный models.json

Это устраняет проблему устаревших model_id (decommissioned моделей).
"""

import json
import os
import asyncio
from typing import Optional
from .models import TaskRequest, TaskType, ModelConfig, ModelTier

CONFIG_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "models.json")


def _load_config() -> dict:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _models_from_config(config: dict) -> list[ModelConfig]:
    """Строит список ModelConfig из статичного models.json."""
    result = []
    for m in config["models"]:
        result.append(ModelConfig(
            name=m["name"],
            tier=ModelTier(m["tier"]),
            provider=m["provider"],
            model_id=m["model_id"],
            max_tokens=m.get("max_tokens", 4096),
            temperature=m.get("temperature", 0.3),
            cost_per_1k=m.get("cost_per_1k", 0.0),
            available=m.get("available", True),
            context_window=m.get("context_window", 8192),
        ))
    return result


def _models_from_discovery(discovered: list[dict]) -> list[ModelConfig]:
    """Строит список ModelConfig из результатов model_discovery."""
    result = []
    for m in discovered:
        result.append(ModelConfig(
            name=m["name"],
            tier=ModelTier(m["tier"]),
            provider=m["provider"],
            model_id=m["model_id"],
            max_tokens=m.get("max_tokens", 4096),
            temperature=m.get("temperature", 0.3),
            cost_per_1k=m.get("cost_per_1k", 0.0),
            available=m.get("available", True),
            context_window=m.get("context_window", 8192),
        ))
    return result


# Приоритет провайдеров внутри тира (меньше = лучше).
# cloud_heavy: GitHub GPT-4o первый (128K, бесплатно), Gemini для больших контекстов
# cloud_fast:  Cerebras (1000+ tok/s), Groq (быстро), остальные
PROVIDER_TIER_PRIORITY: dict[str, list[str]] = {
    # groq первым в cloud_heavy — gpt-oss-120b и groq/compound проверены и стабильны
    "cloud_heavy": ["groq", "sambanova", "gemini", "mistral", "nvidia", "cohere", "cloudflare", "zai", "together", "novita", "anthropic", "openrouter"],
    "cloud_fast":  ["groq", "cerebras", "sambanova", "gemini", "mistral", "zai", "nvidia", "cohere", "cloudflare", "together", "novita", "openrouter"],
    "local_small": ["ollama"],
    "local_large": ["ollama"],
}

# Задачи с большим контекстом — Gemini (1M) вместо GPT-4o (128K)
_LARGE_CONTEXT_TASKS = frozenset({"file_ops", "coding_large"})
_LARGE_CONTEXT_PROVIDER_PRIORITY = ["gemini", "github", "sambanova", "openrouter", "anthropic"]

# Порог переключения на большой контекст по РЕАЛЬНОЙ длине запроса.
# ~60k токенов ≈ 240k символов (грубо 4 символа на токен для смеси кода/текста).
# Если запрос длиннее — слать на модель с большим окном, независимо от типа задачи.
_LARGE_CONTEXT_TOKEN_THRESHOLD = 60_000
_CHARS_PER_TOKEN = 4   # грубая оценка без токенизатора
_LARGE_CONTEXT_CHAR_THRESHOLD = _LARGE_CONTEXT_TOKEN_THRESHOLD * _CHARS_PER_TOKEN


def _estimate_tokens(text: str) -> int:
    """Грубая оценка числа токенов без внешнего токенизатора."""
    return len(text) // _CHARS_PER_TOKEN if text else 0


class Router:
    """
    Выбирает подходящую модель для задачи.
    Приоритет:
      1. Динамический список из model_discovery (актуальные модели провайдеров)
      2. Статичный models.json (fallback)
      3. Fallback по цепочке fallback_chain
    """

    def __init__(self):
        config = _load_config()
        self.routing_rules: dict[str, str]  = config["routing_rules"]
        self.critic_model_tier    = config.get("critic_model", "cloud_fast")
        self.architect_model_tier = config.get("architect_model", "cloud_fast")
        self.fallback_chain: list[str] = config.get("fallback_chain", ["local_small", "cloud_fast"])
        self._discovery_used = False
        self._initialized = False

        # Загружаем статичный конфиг как базу (быстро, синхронно)
        self.models = _models_from_config(config)

        # Проверяем Ollama через TCP (1 сек)
        from .model_discovery import _is_ollama_running
        if not _is_ollama_running():
            for m in self.models:
                if m.provider == "ollama":
                    m.available = False
            print("  >> Ollama offline - skipping local_small, using cloud.")

        # Проверяем наличие API-ключей для cloud провайдеров
        _PROVIDER_ENV: dict[str, str] = {
            "groq":       "GROQ_API_KEY",
            "openrouter": "OPENROUTER_API_KEY",
            "cerebras":   "CEREBRAS_API_KEY",
            "gemini":     "GEMINI_API_KEY",
            "anthropic":  "ANTHROPIC_API_KEY",
            "sambanova":  "SAMBANOVA_API_KEY",
            "github":     "GITHUB_TOKEN",
            "together":   "TOGETHER_API_KEY",
            "novita":     "NOVITA_API_KEY",
            "mistral":    "MISTRAL_API_KEY",
        }
        for m in self.models:
            env_key = _PROVIDER_ENV.get(m.provider)
            if env_key and not os.environ.get(env_key, ""):
                m.available = False

        available = [m for m in self.models if m.available]
        print(f"  >> {len(available)} моделей (static). Запуск discovery при первом запросе...")

    async def initialize(self):
        """
        Async инициализация: опрашивает провайдеров за живым списком моделей.
        Вызывается из Orchestrator.run() перед первой задачей.
        """
        if self._initialized:
            return
        self._initialized = True
        try:
            discovered = await self._run_discovery()
            if discovered:
                self.models = discovered
                self._discovery_used = True
                available = [m for m in self.models if m.available]
                print(f"  >> Discovery: {len(available)} моделей — " +
                      ", ".join(f"{m.name}({m.provider})" for m in available[:4]) +
                      ("..." if len(available) > 4 else ""))
        except Exception as e:
            print(f"  [Router] Discovery failed: {e} — используем static config")

    async def _run_discovery(self) -> list[ModelConfig]:
        """Запускает model_discovery и возвращает список ModelConfig."""
        from .model_discovery import discover_all
        discovered = await discover_all()
        return _models_from_discovery(discovered)

    async def refresh(self):
        """Принудительно обновляет список моделей (вызывается из /reload)."""
        from .model_discovery import discover_all, invalidate
        invalidate()
        discovered = await discover_all(force=True)
        if discovered:
            self.models = _models_from_discovery(discovered)
            self._discovery_used = True
        return [m for m in self.models if m.available]

    # ------------------------------------------------------------------
    # Публичные методы
    # ------------------------------------------------------------------

    def mark_provider_unavailable(self, provider: str) -> int:
        """Помечает все модели провайдера как недоступные. Возвращает кол-во отключённых."""
        count = 0
        for m in self.models:
            if m.provider.lower() == provider.lower() and m.available:
                m.available = False
                count += 1
        return count

    def restore_provider(self, provider: str) -> int:
        """Восстанавливает все модели провайдера."""
        count = 0
        for m in self.models:
            if m.provider.lower() == provider.lower() and not m.available:
                m.available = True
                count += 1
        return count

    def route(self, request: TaskRequest) -> ModelConfig:
        """Возвращает ModelConfig для выполнения задачи.

        Для задач с большим контекстом (file_ops, coding_large) предпочитает Gemini (1M).
        Поддерживает context["preferred_provider"] для принудительного выбора провайдера.
        """
        task_key       = request.task_type.value if request.task_type else "general"
        preferred_tier = self.routing_rules.get(task_key, "cloud_fast")

        # Если запрошен конкретный провайдер — ищем его первым
        preferred_provider = request.context.get("preferred_provider") if request.context else None
        if preferred_provider:
            model = self._find_available_by_provider(preferred_provider, preferred_tier)
            if model:
                return model

        # ── Маршрутизация по РЕАЛЬНОЙ длине контекста ──
        # Считаем длину запроса + прикреплённого контекста (long_context из context).
        est_tokens = _estimate_tokens(request.user_input or "")
        extra_ctx = ""
        if request.context:
            for k in ("long_context", "file_content", "attached_text", "memory_context"):
                v = request.context.get(k)
                if isinstance(v, str):
                    extra_ctx += v
        est_tokens += _estimate_tokens(extra_ctx)

        is_large_context = (
            task_key in _LARGE_CONTEXT_TASKS
            or est_tokens >= _LARGE_CONTEXT_TOKEN_THRESHOLD
        )

        # Для больших контекстов — модель с большим окном (Gemini 1M и т.д.),
        # причём фильтруем по реальному context_window если знаем размер запроса.
        if is_large_context:
            model = self._find_available_for_context(
                preferred_tier, est_tokens, _LARGE_CONTEXT_PROVIDER_PRIORITY
            )
            if model:
                return model

        model = self._find_available(preferred_tier)
        if model:
            return model
        # Fallback по цепочке
        for tier in self.fallback_chain:
            model = self._find_available(tier)
            if model:
                return model
        raise RuntimeError("Нет доступных моделей! Проверь API ключи / Ollama.")

    def get_critic_model(self) -> ModelConfig:
        model = self._find_available(self.critic_model_tier)
        if not model:
            model = self._find_available("cloud_fast")
        if not model:
            raise RuntimeError("Нет доступной модели для критика.")
        return model

    def get_architect_model(self) -> ModelConfig:
        model = self._find_available(self.architect_model_tier)
        if not model:
            model = self._find_available("cloud_fast")
        return model

    def mark_unavailable(self, model_name: str):
        """Помечает модель как недоступную при ошибке провайдера."""
        for m in self.models:
            if m.name == model_name or m.model_id == model_name:
                m.available = False
                break

    def list_available(self) -> list[ModelConfig]:
        return [m for m in self.models if m.available]

    # ------------------------------------------------------------------
    # Внутренние методы
    # ------------------------------------------------------------------

    @staticmethod
    def _provider_ok(provider: str) -> bool:
        """False, только если TokenBudget знает провайдера и ВСЕ его ключи в
        кулдауне/дневном лимите. Незнакомые провайдеры — ок (fail-open).
        Раньше роутер игнорировал кулдауны: критик выбирал groq в разгар 429
        и получал дефолтные 0.72 вместо оценки."""
        try:
            from . import token_budget as _tb
            slots = _tb.get()._slots.get(provider)
            if not slots:
                return True
            return any(s.is_available for s in slots)
        except Exception:
            return True

    def _find_available_by_provider(self, provider: str, tier_str: str) -> Optional[ModelConfig]:
        """Ищет первую доступную модель указанного провайдера (tier hint)."""
        try:
            tier = ModelTier(tier_str)
        except ValueError:
            tier = None
        # Сначала с нужным tier
        for m in self.models:
            if m.provider.lower() == provider.lower() and m.available:
                if tier is None or m.tier == tier:
                    return m
        # Любой tier того же провайдера
        for m in self.models:
            if m.provider.lower() == provider.lower() and m.available:
                return m
        return None

    def _find_available_for_context(
        self, tier_str: str, est_tokens: int, provider_priority: list[str]
    ) -> Optional[ModelConfig]:
        """Выбирает доступную модель, чьё context_window вмещает запрос.

        1. Сначала — модели с окном >= запрос (+25% запас), по приоритету провайдеров.
        2. Если таких нет — самое большое окно из доступных (лучше что есть).
        """
        needed = int(est_tokens * 1.25) if est_tokens else 0

        def _ctx_window(m: ModelConfig) -> int:
            return getattr(m, "context_window", 0) or 0

        def _rank(m: ModelConfig) -> int:
            try:
                return provider_priority.index(m.provider)
            except ValueError:
                return len(provider_priority)

        available = [m for m in self.models
                     if m.available and self._provider_ok(m.provider)]
        if not available:
            available = [m for m in self.models if m.available]
        if not available:
            return None

        # 1. Модели которые точно вмещают запрос
        fits = [m for m in available if _ctx_window(m) >= needed]
        if fits:
            # сортируем: сначала по приоритету провайдера, потом по большему окну
            fits.sort(key=lambda m: (_rank(m), -_ctx_window(m)))
            return fits[0]

        # 2. Не вмещает никто — берём с максимальным окном
        available.sort(key=lambda m: -_ctx_window(m))
        return available[0]

    def _find_available_with_priority(
        self, tier_str: str, provider_priority: list[str]
    ) -> Optional[ModelConfig]:
        """Как _find_available, но с кастомным порядком провайдеров."""
        try:
            tier = ModelTier(tier_str)
        except ValueError:
            return None
        candidates = [m for m in self.models
                      if m.tier == tier and m.available and self._provider_ok(m.provider)]
        if not candidates:
            candidates = [m for m in self.models if m.tier == tier and m.available]
        if not candidates:
            return None
        def _rank(m: ModelConfig) -> int:
            try:
                return provider_priority.index(m.provider)
            except ValueError:
                return len(provider_priority)
        candidates.sort(key=_rank)
        return candidates[0]

    def _find_available(self, tier_str: str) -> Optional[ModelConfig]:
        try:
            tier = ModelTier(tier_str)
        except ValueError:
            return None

        # Кандидаты текущего тира (провайдеры в 429-кулдауне пропускаем)
        candidates = [m for m in self.models
                      if m.tier == tier and m.available and self._provider_ok(m.provider)]
        if not candidates:
            # Все в кулдауне — деградируем мягко: берём как раньше, без фильтра
            candidates = [m for m in self.models if m.tier == tier and m.available]
        if not candidates:
            return None

        # Сортируем по приоритету провайдера внутри тира
        priority = PROVIDER_TIER_PRIORITY.get(tier_str, [])
        def _rank(m: ModelConfig) -> int:
            try:
                return priority.index(m.provider)
            except ValueError:
                return len(priority)  # неизвестные провайдеры — в конец

        candidates.sort(key=_rank)
        return candidates[0]
