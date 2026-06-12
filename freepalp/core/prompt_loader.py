"""
PromptLoader — загружает prompts.json и предоставляет доступ ко всем настройкам.

Это единственный источник правды для:
  - task_keywords
  - worker_prompts
  - critic_system
  - retry_threshold
  - complexity_markers

Поддерживает горячую перезагрузку: reload() применяет новую версию без рестарта.
"""

import json
import os
from pathlib import Path
from typing import Optional

_PROMPTS_PATH = Path(__file__).parent.parent / "config" / "prompts.json"
_RULES_PATH   = Path(__file__).parent.parent / "FREEPALP_RULES.md"
_cache: Optional[dict] = None
_rules_cache: Optional[str] = None


def _load() -> dict:
    with open(_PROMPTS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def get() -> dict:
    """Возвращает текущий конфиг (кешированный)."""
    global _cache
    if _cache is None:
        _cache = _load()
    return _cache


def reload() -> dict:
    """Принудительно перечитывает prompts.json с диска."""
    global _cache, _rules_cache
    _cache = _load()
    _rules_cache = None  # сбрасываем кеш правил тоже
    return _cache


def get_rules() -> str:
    """Загружает FREEPALP_RULES.md (кешируется до reload)."""
    global _rules_cache
    if _rules_cache is None:
        if _RULES_PATH.exists():
            _rules_cache = _RULES_PATH.read_text("utf-8")
        else:
            _rules_cache = ""
    return _rules_cache


def get_task_keywords() -> dict[str, list[str]]:
    return get()["task_keywords"]


def get_worker_prompt(task_type: str) -> str:
    """Возвращает системный промпт Worker + обязательные правила качества."""
    prompts = get()["worker_prompts"]
    base = prompts.get(task_type, prompts["general"])
    # Компактный свод обязательных правил (не весь FREEPALP_RULES, только для Worker)
    rules_suffix = (
        "\n\n[ОБЯЗАТЕЛЬНЫЕ ПРАВИЛА]\n"
        "- Идентичность: ты — FreePalp, мульти-агентный AI-оркестратор. На вопросы "
        "«кто ты», «как тебя зовут», «какая ты модель» отвечай только: FreePalp. "
        "НИКОГДА не называй себя ChatGPT, GPT, Claude, Gemini, LLaMA, Qwen или именем "
        "базовой модели\n"
        "- Код: всегда type hints, docstring, обработка ошибок для IO\n"
        "- Импорты: только реально существующие пакеты (rabbitmq→pika, firebase→firebase_admin)\n"
        "- Факты: не придумывать, GIL снимается при IO, threading подходит для IO\n"
        "- Кодировка: open(..., encoding='utf-8'), subprocess без text=True\n"
        "- Ответ на языке пользователя\n"
        "- Если используешь знание из [Контекст агента] — укажи источник в скобках: "
        "(из hot_memory) или (из corrections)"
    )
    return base + rules_suffix


def get_critic_system() -> str:
    return get()["critic_system"]


def get_retry_threshold() -> float:
    # settings.json пользователя приоритетнее значения из prompts.json
    settings_path = Path(__file__).parent.parent / "config" / "settings.json"
    try:
        if settings_path.exists():
            s = json.loads(settings_path.read_text("utf-8"))
            if "retry_threshold" in s:
                return float(s["retry_threshold"])
    except Exception:
        pass
    return float(get().get("retry_threshold", 0.7))


def get_complexity_markers() -> list[str]:
    return get().get("complexity_markers", [])


def get_version() -> str:
    return get().get("version", "unknown")
