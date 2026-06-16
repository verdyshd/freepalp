"""
ModelDiscovery — автообнаружение доступных бесплатных LLM-моделей.

При старте Router запрашивает у каждого настроенного провайдера
актуальный список моделей. Это устраняет проблему устаревшего models.json.

Поддерживаемые провайдеры (добавляются автоматически при наличии ключа):
  - Groq            (GROQ_API_KEY)
  - OpenRouter      (OPENROUTER_API_KEY)   — фильтрует модели с price==0
  - Cerebras        (CEREBRAS_API_KEY)
  - Ollama          (локальный, без ключа)

Возвращает список ModelConfig совместимых dict-ов.
Кешируется на SESSION_CACHE_TTL секунд (не опрашивать при каждом запросе).
"""

import os
import json
import time
import socket
from pathlib import Path
from typing import Optional

SESSION_CACHE_TTL = 3600   # 1 час — обновляем список раз в час
_cache: Optional[dict] = None   # {"ts": float, "models": list[dict]}

# -----------------------------------------------------------------------
# Мэппинг Groq model_id → характеристики (то что API не возвращает)
# -----------------------------------------------------------------------
GROQ_MODEL_META = {
    # Формат: "model_id": {"tier": "...", "max_tokens": N, "cost_per_1k": X}
    # Groq Free Tier: все модели бесплатны → cost_per_1k = 0.0
    "llama-3.3-70b-versatile":              {"tier": "cloud_fast",  "max_tokens": 4096, "cost_per_1k": 0.0},
    "llama-3.1-8b-instant":                 {"tier": "cloud_fast",  "max_tokens": 4096, "cost_per_1k": 0.0},
    "meta-llama/llama-4-scout-17b-16e-instruct": {"tier": "cloud_fast", "max_tokens": 4096, "cost_per_1k": 0.0},
    "qwen/qwen3-32b":                       {"tier": "cloud_fast",  "max_tokens": 4096, "cost_per_1k": 0.0},
    "groq/compound":                        {"tier": "cloud_heavy", "max_tokens": 4096, "cost_per_1k": 0.0},
    "groq/compound-mini":                   {"tier": "cloud_fast",  "max_tokens": 4096, "cost_per_1k": 0.0},
    "openai/gpt-oss-120b":                  {"tier": "cloud_heavy", "max_tokens": 4096, "cost_per_1k": 0.0},
    "openai/gpt-oss-20b":                   {"tier": "cloud_fast",  "max_tokens": 4096, "cost_per_1k": 0.0},
    "allam-2-7b":                           {"tier": "cloud_fast",  "max_tokens": 2048, "cost_per_1k": 0.0},
}

# Модели которые не являются chat-моделями (skip)
GROQ_NON_CHAT = {"whisper", "guard", "safeguard", "orpheus"}

# Модели которые мы хотим использовать как приоритетные (сортировка)
GROQ_PREFERRED_ORDER = [
    "llama-3.3-70b-versatile",
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "qwen/qwen3-32b",
    "llama-3.1-8b-instant",
    "groq/compound-mini",
    "openai/gpt-oss-20b",
]


def _is_chat_model_groq(model_id: str) -> bool:
    """Фильтр: пропускаем speech/embedding/guard модели."""
    mid_lower = model_id.lower()
    return not any(skip in mid_lower for skip in GROQ_NON_CHAT)


def _groq_meta(model_id: str) -> dict:
    """Возвращает мета-данные для Groq модели (tier, max_tokens, cost)."""
    if model_id in GROQ_MODEL_META:
        return GROQ_MODEL_META[model_id]
    # Эвристика для незнакомых моделей
    mid = model_id.lower()
    if any(s in mid for s in ["70b", "72b", "120b", "32b", "scout"]):
        tier, max_t, cost = "cloud_fast", 4096, 0.0   # Groq Free Tier
    elif any(s in mid for s in ["8b", "7b", "mini", "instant", "20b"]):
        tier, max_t, cost = "cloud_fast", 4096, 0.0   # Groq Free Tier
    else:
        tier, max_t, cost = "cloud_fast", 4096, 0.0   # Groq Free Tier
    return {"tier": tier, "max_tokens": max_t, "cost_per_1k": cost}


def _safe_name(model_id: str) -> str:
    """Делает короткое имя из model_id для отображения."""
    # "meta-llama/llama-4-scout-17b-16e-instruct" → "llama-4-scout-17b"
    base = model_id.split("/")[-1]
    parts = base.split("-")
    # Берём до 4 значимых частей
    return "-".join(parts[:4])


# -----------------------------------------------------------------------
# Провайдер: Groq
# -----------------------------------------------------------------------

async def _discover_groq() -> list[dict]:
    from . import token_budget as _tb
    api_key = _tb.get().get_active_key("groq") or os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                "https://api.groq.com/openai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            data = r.json()

        models = []
        for m in data.get("data", []):
            mid = m["id"]
            if not _is_chat_model_groq(mid):
                continue
            meta = _groq_meta(mid)
            models.append({
                "name": _safe_name(mid),
                "provider": "groq",
                "model_id": mid,
                "tier": meta["tier"],
                "max_tokens": meta["max_tokens"],
                "temperature": 0.3,
                "cost_per_1k": meta["cost_per_1k"],
                "available": True,
                "context_window": m.get("context_window", 8192),
            })

        # Сортируем: preferred первыми, остальные по алфавиту
        def sort_key(m):
            mid = m["model_id"]
            try:
                return GROQ_PREFERRED_ORDER.index(mid)
            except ValueError:
                return len(GROQ_PREFERRED_ORDER) + 1

        models.sort(key=sort_key)
        return models

    except Exception as e:
        print(f"  [Discovery] Groq error: {e}")
        return []


# -----------------------------------------------------------------------
# Провайдер: OpenRouter (только бесплатные модели)
# -----------------------------------------------------------------------

async def _discover_openrouter() -> list[dict]:
    from . import token_budget as _tb
    api_key = _tb.get().get_active_key("openrouter") or os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            data = r.json()

        models = []
        for m in data.get("data", []):
            pricing = m.get("pricing", {})
            prompt_price = float(pricing.get("prompt", "1") or "1")
            if prompt_price > 0:
                continue   # пропускаем платные

            mid = m["id"]
            ctx = m.get("context_length", 8192)
            max_t = min(4096, ctx // 2)

            models.append({
                "name": _safe_name(mid),
                "provider": "openrouter",
                "model_id": mid,
                "tier": "cloud_fast",
                "max_tokens": max_t,
                "temperature": 0.3,
                "cost_per_1k": 0.0,
                "available": True,
                "context_window": ctx,
            })

        return models[:10]   # топ-10 бесплатных

    except Exception as e:
        print(f"  [Discovery] OpenRouter error: {e}")
        return []


# -----------------------------------------------------------------------
# Провайдер: Cerebras
# -----------------------------------------------------------------------

async def _discover_cerebras() -> list[dict]:
    from . import token_budget as _tb
    api_key = _tb.get().get_active_key("cerebras") or os.environ.get("CEREBRAS_API_KEY", "")
    if not api_key:
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                "https://api.cerebras.ai/v1/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
            r.raise_for_status()
            data = r.json()

        models = []
        for m in data.get("data", []):
            mid = m["id"]
            models.append({
                "name": _safe_name(mid),
                "provider": "cerebras",
                "model_id": mid,
                "tier": "cloud_fast",
                "max_tokens": 4096,
                "temperature": 0.3,
                "cost_per_1k": 0.0,
                "available": True,
                "context_window": m.get("context_window", 8192),
            })
        return models

    except Exception as e:
        print(f"  [Discovery] Cerebras error: {e}")
        return []


# -----------------------------------------------------------------------
# Провайдер: Ollama (локальный)
# -----------------------------------------------------------------------

def _is_ollama_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 11434), timeout=1.0):
            return True
    except OSError:
        return False


async def _discover_ollama() -> list[dict]:
    if not _is_ollama_running():
        return []
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("http://localhost:11434/api/tags")
            r.raise_for_status()
            data = r.json()

        models = []
        for m in data.get("models", []):
            mid = m["name"]   # e.g. "qwen2.5-coder:7b"
            size_bytes = m.get("size", 0)
            size_gb = size_bytes / 1e9
            tier = "local_small" if size_gb < 10 else "local_large"
            models.append({
                "name": mid.split(":")[0],
                "provider": "ollama",
                "model_id": mid,
                "tier": tier,
                "max_tokens": 4096,
                "temperature": 0.2,
                "cost_per_1k": 0.0,
                "available": True,
                "context_window": 8192,
            })
        return models

    except Exception as e:
        print(f"  [Discovery] Ollama error: {e}")
        return []


# -----------------------------------------------------------------------
# Провайдеры с OpenAI-совместимым API (Together, Novita, Gemini, HF)
# -----------------------------------------------------------------------

# Для провайдеров где нет удобного /models endpoint — используем known-модели
_OPENAI_COMPAT_PROVIDERS = {
    "sambanova": {
        "env_key":  "SAMBANOVA_API_KEY",
        "base_url": "https://api.sambanova.ai/v1",
        "free_check": True,
        "models": [
            # Только проверенные рабочие модели (405B, DeepSeek-R1, DeepSeek-V3 = "model not found")
            {"id": "Meta-Llama-3.3-70B-Instruct",   "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0},
            {"id": "Meta-Llama-3.1-8B-Instruct",    "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0},
            {"id": "Qwen2.5-72B-Instruct",          "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0},
        ],
    },
    # GitHub Models REMOVED — нестабилен: rate limits (429) + request body too large (413 на DeepSeek-R1)
    # При наличии GITHUB_TOKEN переменная окружения НЕ активирует этот провайдер.
    "together": {
        "env_key":  "TOGETHER_API_KEY",
        "base_url": "https://api.together.xyz/v1",
        "free_check": False,   # У Together нет бесплатных, но есть $5 кредитов
        "models": [
            {"id": "meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo", "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0009},
            {"id": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",  "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0002},
            {"id": "mistralai/Mixtral-8x7B-Instruct-v0.1",          "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0006},
            {"id": "Qwen/Qwen2.5-72B-Instruct-Turbo",               "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0012},
            {"id": "deepseek-ai/DeepSeek-V3",                        "tier": "cloud_heavy", "max_tokens": 4096, "cost": 0.0013},
        ],
    },
    "novita": {
        "env_key":       "NOVITA_API_KEY",
        "base_url":      "https://api.novita.ai/v3/openai",
        "free_check":    False,
        # /models у Novita публичный (работает без ключа) — нужна отдельная проверка авторизации
        "auth_check_url": "https://api.novita.ai/v3/openai/chat/completions",
        "auth_check_body": {
            "model": "meta-llama/llama-3.1-8b-instruct",
            "messages": [{"role": "user", "content": "x"}],
            "max_tokens": 1,
        },
        "models": [
            {"id": "meta-llama/llama-3.1-70b-instruct",  "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0008},
            {"id": "meta-llama/llama-3.1-8b-instruct",   "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0002},
            {"id": "qwen/qwen2.5-72b-instruct",           "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0009},
            {"id": "deepseek/deepseek-v3",                "tier": "cloud_heavy", "max_tokens": 4096, "cost": 0.0014},
        ],
    },
    # Mistral La Plateforme — щедрый free tier: 1 req/s, 500k tok/min, 1 МЛРД tok/мес
    "mistral": {
        "env_key":  "MISTRAL_API_KEY",
        "base_url": "https://api.mistral.ai/v1",
        "free_check": True,   # есть бесплатный tier
        "models": [
            {"id": "mistral-large-latest",   "tier": "cloud_heavy", "max_tokens": 4096, "cost": 0.0, "context_window": 131072},
            {"id": "mistral-small-latest",   "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0, "context_window": 131072},
            {"id": "open-mistral-nemo",      "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0, "context_window": 131072},
            {"id": "codestral-latest",       "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0, "context_window": 262144},
        ],
    },
    # z.ai (Zhipu GLM) — glm-4.5-flash БЕСПЛАТНА. Эндпоинт paas/v4 (НЕ openai/v1 — там 404),
    # /models недоступен → авторизацию проверяем через chat/completions (как Novita).
    "zai": {
        "env_key":  "ZAI_API_KEY",
        "base_url": "https://api.z.ai/api/paas/v4",
        "free_check": False,
        "auth_check_url": "https://api.z.ai/api/paas/v4/chat/completions",
        "auth_check_body": {
            "model": "glm-4.5-flash",
            "messages": [{"role": "user", "content": "x"}],
            "max_tokens": 1,
        },
        "models": [
            {"id": "glm-4.5-flash", "tier": "cloud_fast", "max_tokens": 4096, "cost": 0.0, "context_window": 131072},
        ],
    },
}

_GEMINI_MODELS = [
    {"id": "gemini-2.0-flash",        "tier": "cloud_fast",  "max_tokens": 8192, "cost": 0.0},
    {"id": "gemini-1.5-flash",        "tier": "cloud_fast",  "max_tokens": 8192, "cost": 0.0},
    {"id": "gemini-1.5-flash-8b",     "tier": "cloud_fast",  "max_tokens": 4096, "cost": 0.0},
    # gemini-1.5-pro убран — возвращает 404 (model not found) на бесплатном API
]


async def _discover_openai_compat(provider_key: str) -> list[dict]:
    """Универсальный discovery для OpenAI-compatible провайдеров."""
    cfg = _OPENAI_COMPAT_PROVIDERS.get(provider_key)
    if not cfg:
        return []
    from . import token_budget as _tb
    api_key = _tb.get().get_active_key(provider_key) or os.environ.get(cfg["env_key"], "")
    if not api_key:
        return []

    try:
        import httpx
        async with httpx.AsyncClient(timeout=8.0) as client:
            # Если у провайдера /models публичный (напр. Novita) — проверяем авторизацию отдельно
            auth_check_url  = cfg.get("auth_check_url")
            auth_check_body = cfg.get("auth_check_body")
            if auth_check_url and auth_check_body:
                try:
                    auth_r = await client.post(
                        auth_check_url,
                        headers={"Authorization": f"Bearer {api_key}",
                                 "Content-Type": "application/json"},
                        json=auth_check_body,
                        timeout=8.0,
                    )
                    if auth_r.status_code in (401, 403):
                        print(f"  [Discovery] {provider_key}: auth check failed ({auth_r.status_code}) — пропускаем")
                        return []
                except Exception:
                    pass  # Если не получилось проверить — пробуем добавить модели
            else:
                # Стандартная проверка через /models
                r = await client.get(
                    f"{cfg['base_url']}/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                if r.status_code == 401:
                    return []
    except Exception:
        pass   # Если /models недоступен — всё равно добавляем known модели

    models = []
    for m in cfg["models"]:
        models.append({
            "name":         _safe_name(m["id"]),
            "provider":     provider_key,
            "model_id":     m["id"],
            "tier":         m["tier"],
            "max_tokens":   m["max_tokens"],
            "temperature":  0.3,
            "cost_per_1k":  m["cost"],
            "available":    True,
            "context_window": 131072,
        })
    return models


async def _discover_gemini() -> list[dict]:
    """Google Gemini — через REST API (OpenAI-совместимый режим в google.generativeai)."""
    from . import token_budget as _tb
    api_key = _tb.get().get_active_key("gemini") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return []
    models = []
    for m in _GEMINI_MODELS:
        models.append({
            "name":         m["id"].replace("gemini-", "gemini-"),
            "provider":     "gemini",
            "model_id":     m["id"],
            "tier":         m["tier"],
            "max_tokens":   m["max_tokens"],
            "temperature":  0.3,
            "cost_per_1k":  m["cost"],
            "available":    True,
            "context_window": 1000000,
        })
    return models


# -----------------------------------------------------------------------
# Главная функция
# -----------------------------------------------------------------------

async def discover_all(force: bool = False) -> list[dict]:
    """
    Возвращает актуальный список доступных моделей со всех провайдеров.
    Кешируется на SESSION_CACHE_TTL секунд.
    """
    global _cache
    now = time.time()

    if not force and _cache and (now - _cache["ts"]) < SESSION_CACHE_TTL:
        return _cache["models"]

    print("  [Discovery] Опрашиваем провайдеров...")

    # Каталог models.dev: подключаем любые OpenAI-совместимые провайдеры,
    # для которых пользователь положил ключ в .env (сотни — одной настройкой)
    try:
        from .models_dev_catalog import fetch_catalog, extra_openai_compat_providers
        catalog = await fetch_catalog()
        if catalog:
            known = set(_OPENAI_COMPAT_PROVIDERS) | {
                "groq", "openrouter", "cerebras", "gemini", "ollama",
                "novita-ai",                        # каталожный алиас захардкоженного "novita" (иначе дубль моделей)
                "github-models", "github-copilot",  # GitHub Models осознанно убран (429/413); copilot — другая авторизация
            }
            extra = extra_openai_compat_providers(catalog, known)
            for pid, cfg in extra.items():
                if pid not in _OPENAI_COMPAT_PROVIDERS:
                    _OPENAI_COMPAT_PROVIDERS[pid] = cfg
                    print(f"  [Discovery] +{pid} из каталога models.dev ({len(cfg['models'])} моделей)")
    except Exception as e:
        print(f"  [Discovery] models.dev каталог недоступен: {e}")

    # Запускаем все discovery параллельно
    import asyncio
    results = await asyncio.gather(
        _discover_ollama(),
        _discover_groq(),
        _discover_openrouter(),
        _discover_cerebras(),
        _discover_gemini(),
        _discover_openai_compat("sambanova"),
        # GitHub Models отключён — нестабилен (rate limits 429, max size 413)
        _discover_openai_compat("together"),
        _discover_openai_compat("novita"),
        _discover_openai_compat("mistral"),
        return_exceptions=True,
    )

    all_models = []
    provider_names = ["Ollama", "Groq", "OpenRouter", "Cerebras", "Gemini", "SambaNova", "Together", "Novita", "Mistral"]
    for name, r in zip(provider_names, results):
        if isinstance(r, list):
            all_models.extend(r)
            if r:
                print(f"  [Discovery] {name}: {len(r)} моделей")
        else:
            print(f"  [Discovery] {name}: ошибка — {r}")

    # Динамические провайдеры из models.dev (добавлены выше в _OPENAI_COMPAT_PROVIDERS)
    _static_compat = {"sambanova", "together", "novita", "mistral"}
    _dynamic = [k for k in _OPENAI_COMPAT_PROVIDERS if k not in _static_compat]
    if _dynamic:
        dyn_results = await asyncio.gather(
            *[_discover_openai_compat(k) for k in _dynamic], return_exceptions=True)
        for name, r in zip(_dynamic, dyn_results):
            if isinstance(r, list) and r:
                all_models.extend(r)
                print(f"  [Discovery] {name} (models.dev): {len(r)} моделей")

    if not all_models:
        print("  [Discovery] Нет доступных провайдеров — fallback на models.json")

    _cache = {"ts": now, "models": all_models}
    return all_models


def get_cached() -> list[dict]:
    """Синхронный доступ к закешированному списку (после первого discover_all)."""
    return _cache["models"] if _cache else []


def invalidate():
    """Сбросить кеш — вызывается при смене .env или /reload."""
    global _cache
    _cache = None


# -----------------------------------------------------------------------
# Каталог провайдеров
# -----------------------------------------------------------------------

PROVIDERS_CATALOG = [
    {
        "name":     "Groq",
        "env_key":  "GROQ_API_KEY",
        "url":      "https://console.groq.com/keys",
        "signup":   "https://console.groq.com/",
        "free":     True,
        "limits":   "Free: ~14 400 req/day, 6 000 TPM на малых моделях",
        "models":   "llama-3.3-70b, llama-4-scout, qwen3-32b, llama-3.1-8b и др.",
        "notes":    "Лучший старт: быстрый, бесплатный, актуальный список моделей",
    },
    {
        "name":     "OpenRouter",
        "env_key":  "OPENROUTER_API_KEY",
        "url":      "https://openrouter.ai/keys",
        "signup":   "https://openrouter.ai/",
        "free":     True,
        "limits":   "Бесплатные модели без лимитов (с суффиксом :free), платные — по тарифу",
        "models":   "300+ моделей: Llama, Mistral, Gemma, DeepSeek, Phi и др.",
        "notes":    "Самый широкий выбор бесплатных моделей",
    },
    {
        "name":     "Cerebras",
        "env_key":  "CEREBRAS_API_KEY",
        "url":      "https://cloud.cerebras.ai/",
        "signup":   "https://cloud.cerebras.ai/",
        "free":     True,
        "limits":   "Free tier: llama3.1-70b, 60 req/min",
        "models":   "llama3.1-8b, llama3.1-70b (очень быстрые — специализированные чипы)",
        "notes":    "Самый быстрый инференс (1000+ токенов/сек на 70B)",
    },
    {
        "name":     "Google Gemini",
        "env_key":  "GEMINI_API_KEY",
        "url":      "https://aistudio.google.com/apikey",
        "signup":   "https://aistudio.google.com/",
        "free":     True,
        "limits":   "Free: 15 req/min, 1M токенов/мин (gemini-1.5-flash)",
        "models":   "gemini-1.5-flash, gemini-1.5-pro, gemini-2.0-flash",
        "notes":    "Огромный контекст (1M токенов), бесплатно",
    },
    {
        "name":     "Ollama (local)",
        "env_key":  None,
        "url":      "https://ollama.com/download",
        "signup":   None,
        "free":     True,
        "limits":   "Нет лимитов — всё локально",
        "models":   "qwen2.5-coder, deepseek-coder, mistral, llama3, phi3 и др.",
        "notes":    "Приватность 100%, без интернета. Нужен GPU или быстрый CPU",
    },
    {
        "name":     "Anthropic (Claude)",
        "env_key":  "ANTHROPIC_API_KEY",
        "url":      "https://console.anthropic.com/settings/keys",
        "signup":   "https://console.anthropic.com/",
        "free":     False,
        "limits":   "Платный. От $3/1M токенов (Haiku) до $15/1M (Sonnet)",
        "models":   "claude-haiku-3-5, claude-sonnet-4, claude-opus",
        "notes":    "Лучшее качество рассуждений, но платный",
    },
    {
        "name":     "SambaNova",
        "env_key":  "SAMBANOVA_API_KEY",
        "url":      "https://cloud.sambanova.ai/",
        "signup":   "https://cloud.sambanova.ai/",
        "free":     True,
        "limits":   "Бесплатно, без явных лимитов на free tier",
        "models":   "Llama-3.1-405B (!), DeepSeek-R1, DeepSeek-V3, Qwen2.5-72B, Coder-32B",
        "notes":    "Единственный бесплатный доступ к Llama 405B",
    },
    {
        "name":     "GitHub Models",
        "env_key":  "GITHUB_TOKEN",
        "url":      "https://github.com/settings/tokens",
        "signup":   "https://github.com/settings/tokens",
        "free":     True,
        "limits":   "Бесплатно с GitHub аккаунтом, лимиты по req/day",
        "models":   "GPT-4o mini (!), GPT-4o, Phi-4, Llama-3.3-70B, DeepSeek-R1",
        "notes":    "Единственный бесплатный доступ к GPT-4o mini",
    },
    {
        "name":     "Together AI",
        "env_key":  "TOGETHER_API_KEY",
        "url":      "https://api.together.xyz/settings/api-keys",
        "signup":   "https://api.together.xyz/",
        "free":     True,
        "limits":   "$5 кредитов при регистрации, потом платный",
        "models":   "Llama, Mistral, Qwen, DeepSeek, Gemma и др. (OpenAI-совместимый API)",
        "notes":    "Хорошая альтернатива Groq, много моделей",
    },
    {
        "name":     "Novita AI",
        "env_key":  "NOVITA_API_KEY",
        "url":      "https://novita.ai/settings#key-management",
        "signup":   "https://novita.ai/",
        "free":     True,
        "limits":   "$0.5 кредитов при регистрации",
        "models":   "Llama, Mistral, DeepSeek, Qwen (OpenAI-совместимый API)",
        "notes":    "Дешевле многих конкурентов",
    },
    {
        "name":     "Mistral",
        "env_key":  "MISTRAL_API_KEY",
        "url":      "https://console.mistral.ai/api-keys",
        "signup":   "https://console.mistral.ai/",
        "free":     True,
        "limits":   "Бесплатно: 1 req/s, 500k tok/min, 1 МЛРД tok/мес",
        "models":   "Mistral Large/Small, Nemo, Codestral (OpenAI-совместимый API)",
        "notes":    "Очень щедрый free tier, Codestral отлично кодит",
    },
    {
        "name":     "Hugging Face",
        "env_key":  "HF_API_KEY",
        "url":      "https://huggingface.co/settings/tokens",
        "signup":   "https://huggingface.co/join",
        "free":     True,
        "limits":   "Free tier (serverless): ограничен по скорости",
        "models":   "Тысячи open-source моделей",
        "notes":    "Медленно на free tier, но огромный выбор",
    },
]


def get_providers_status() -> list[dict]:
    """
    Возвращает список провайдеров с актуальным статусом ключей.
    configured=True только если ключ есть И провайдер прошёл discovery (есть рабочие модели).
    """
    # Множество провайдеров у которых есть хотя бы одна модель в кэше discovery
    discovered_providers: set[str] = set()
    cached = get_cached()
    for m in cached:
        prov = m.get("provider", "").lower()
        if prov:
            discovered_providers.add(prov)

    result = []
    for p in PROVIDERS_CATALOG:
        env_key = p["env_key"]   # может быть None (Ollama)

        if env_key is None:
            # Ollama — проверяем TCP
            configured = _is_ollama_running()
            key_set = None
        else:
            val = os.environ.get(env_key, "")
            has_key = bool(val)
            key_set = val[:12] + "..." if val else None
            # Провайдер активен только если ключ есть И discovery нашёл его модели
            # (если discovery ещё не запускался — fallback на has_key)
            if discovered_providers and has_key:
                # Нормализуем имя провайдера для сравнения:
                # "Google Gemini" → "google gemini", "Together AI" → "together ai"
                pname = p["name"].lower()
                # Ищем в discovered_providers (ключи: "groq", "novita", "together", "gemini", etc.)
                prov_in_disc = any(
                    d in pname or pname.startswith(d) or pname.endswith(d)
                    for d in discovered_providers
                )
                configured = prov_in_disc
            else:
                # Discovery ещё не запускался — используем наличие ключа как fallback
                configured = has_key

        result.append({
            **p,
            "configured": configured,
            "key_preview": key_set,
        })
    return result
