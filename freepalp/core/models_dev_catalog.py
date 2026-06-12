"""
Каталог models.dev — сотни OpenAI-совместимых провайдеров одной настройкой.

Идея из MiMo Code / opencode: не хардкодить интеграции, а читать открытый
реестр https://models.dev/api.json. Каждый провайдер там — JSON-запись:
имя env-переменной с ключом, base URL (OpenAI-compatible), список моделей
с ценами и лимитами.

Активация: пользователь кладёт ключ провайдера в .env (имя переменной —
как в каталоге, напр. DEEPSEEK_API_KEY, XAI_API_KEY, MOONSHOT_API_KEY...) —
и провайдер подхватывается при discovery. Ничего больше настраивать не надо.

Кэш: config/models_dev_cache.json, TTL 7 дней (каталог меняется редко).
Сеть недоступна / формат изменился — молча работаем на захардкоженных.
"""

import json
import os
import time
from pathlib import Path

CATALOG_URL = "https://models.dev/api.json"
_CACHE_PATH = Path(__file__).parent.parent / "config" / "models_dev_cache.json"
_CACHE_TTL_SEC = 7 * 24 * 3600

# Сколько моделей максимум брать у одного провайдера (иначе роутер утонет)
_MAX_MODELS_PER_PROVIDER = 6


def _load_cache() -> dict | None:
    try:
        if _CACHE_PATH.exists():
            raw = json.loads(_CACHE_PATH.read_text("utf-8"))
            if time.time() - raw.get("_fetched_at", 0) < _CACHE_TTL_SEC:
                return raw.get("catalog")
    except Exception:
        pass
    return None


async def fetch_catalog(force: bool = False) -> dict | None:
    """Каталог провайдеров (кэш → сеть). None если недоступен."""
    if not force:
        cached = _load_cache()
        if cached is not None:
            return cached
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(CATALOG_URL)
            r.raise_for_status()
            catalog = r.json()
        _CACHE_PATH.write_text(
            json.dumps({"_fetched_at": time.time(), "catalog": catalog},
                       ensure_ascii=False), "utf-8")
        return catalog
    except Exception:
        return _load_cache()  # протухший кэш лучше, чем ничего


def _pick_models(models: dict) -> list[dict]:
    """Отбирает до N моделей провайдера, дешёвые сначала."""
    rows = []
    for mid, m in (models or {}).items():
        if not isinstance(m, dict):
            continue
        cost_in = float((m.get("cost") or {}).get("input") or 0.0)   # $/1M токенов
        ctx     = int((m.get("limit") or {}).get("context") or 0)
        out_max = int((m.get("limit") or {}).get("output") or 4096)
        rows.append({
            "id": mid,
            "tier": "cloud_heavy" if ctx >= 200_000 or cost_in > 2.0 else "cloud_fast",
            "max_tokens": min(out_max, 8192) or 4096,
            "cost": cost_in / 1000.0,   # FreePalp считает за 1k токенов
            "context_window": ctx or None,
        })
    rows.sort(key=lambda r: (r["cost"], -(r["context_window"] or 0)))
    return rows[:_MAX_MODELS_PER_PROVIDER]


def extra_openai_compat_providers(catalog: dict, existing: set[str]) -> dict:
    """Провайдеры из каталога, у которых: есть ключ в окружении, есть base URL,
    и они ещё не захардкожены в FreePalp. Формат — как _OPENAI_COMPAT_PROVIDERS."""
    found: dict = {}
    for pid, p in (catalog or {}).items():
        if not isinstance(p, dict) or pid in existing:
            continue
        base_url = p.get("api")
        env_names = p.get("env") or []
        if not base_url or not env_names:
            continue
        env_key = next((e for e in env_names if os.environ.get(e)), None)
        if not env_key:
            continue
        models = _pick_models(p.get("models") or {})
        if not models:
            continue
        found[pid] = {
            "env_key":  env_key,
            "base_url": base_url.rstrip("/"),
            "free_check": False,
            "models":   models,
            "_source":  "models.dev",
        }
    return found
