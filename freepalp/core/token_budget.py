"""
TokenBudget — умное управление квотами API и ротация ключей.

Функции:
  - Отслеживает запросы/токены по каждому провайдеру и ключу
  - Поддерживает ротацию ключей: GROQ_API_KEY, GROQ_API_KEY_2, GROQ_API_KEY_3, ...
  - После 429 — кулдаун, потом автовосстановление
  - Оценивает оставшуюся квоту для отображения в UI
  - Сохраняет состояние в memory/token_budget.json

Использование нескольких ключей:
  В .env добавить:
    GROQ_API_KEY=gsk_...первый...
    GROQ_API_KEY_2=gsk_...второй...
    GROQ_API_KEY_3=gsk_...третий...
"""

from __future__ import annotations

import os
import json
import time
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

BUDGET_FILE = Path(__file__).parent.parent / "memory" / "token_budget.json"

# Известные лимиты бесплатных провайдеров (приблизительные значения)
KNOWN_LIMITS: dict[str, dict] = {
    "groq":       {"req_per_min": 30,   "req_per_day": 14_400,  "tokens_per_day": 500_000},
    "cerebras":   {"req_per_min": 60,   "req_per_day": 30_000,  "tokens_per_day": 1_000_000},
    "sambanova":  {"req_per_min": 30,   "req_per_day": 10_000,  "tokens_per_day": 500_000},
    "openrouter": {"req_per_min": 20,   "req_per_day": 50_000,  "tokens_per_day": 1_000_000},
    "gemini":     {"req_per_min": 15,   "req_per_day": 1_500,   "tokens_per_day": 32_000_000},
    "together":   {"req_per_min": 60,   "req_per_day": 10_000,  "tokens_per_day": 200_000},
    "novita":     {"req_per_min": 30,   "req_per_day": 5_000,   "tokens_per_day": 100_000},
    "mistral":    {"req_per_min": 60,   "req_per_day": 50_000,  "tokens_per_day": 1_000_000_000},
    "ollama":     {"req_per_min": 9999, "req_per_day": 9_999_999, "tokens_per_day": 9_999_999},
}

# Кулдаун после 429 (секунды)
COOLDOWN_AFTER_429: dict[str, int] = {
    "groq":       90,    # per-minute limit → 90s
    "cerebras":   60,
    "sambanova":  120,
    "openrouter": 60,
    "gemini":     60,
    "together":   60,
    "novita":     60,
    "mistral":    60,
    "ollama":     0,     # нет rate limits
}


def _utc_day_start() -> float:
    """Unix timestamp начала текущего дня UTC."""
    t = time.time()
    return t - (t % 86400)


class KeySlot:
    """Один API-ключ одного провайдера."""

    def __init__(self, provider: str, api_key: str, slot_idx: int = 0):
        self.provider    = provider
        self.api_key     = api_key
        self.slot_idx    = slot_idx

        # Счётчик за текущую минуту
        self.req_this_min: int  = 0
        self.min_window_start   = time.time()

        # Счётчик за текущий день
        self.req_today: int     = 0
        self.tokens_today: int  = 0
        self.day_start: float   = _utc_day_start()

        # 429 кулдаун
        self.limited_until: float = 0.0
        self.total_429s: int      = 0

    # ------------------------------------------------------------------ #
    # Состояние                                                            #
    # ------------------------------------------------------------------ #

    def _reset_stale_windows(self) -> None:
        now = time.time()
        if now - self.min_window_start >= 60:
            self.req_this_min    = 0
            self.min_window_start = now
        today = _utc_day_start()
        if today > self.day_start:
            self.req_today   = 0
            self.tokens_today = 0
            self.day_start   = today

    @property
    def is_available(self) -> bool:
        if time.time() < self.limited_until:
            return False
        self._reset_stale_windows()
        limits = KNOWN_LIMITS.get(self.provider, {})
        rpm  = limits.get("req_per_min",  9999)
        rpd  = limits.get("req_per_day",  9999)
        # Оставляем 10% буфер до лимита
        if self.req_this_min >= int(rpm * 0.90):
            return False
        if self.req_today >= int(rpd * 0.95):
            return False
        return True

    def cooldown_left(self) -> int:
        return max(0, int(self.limited_until - time.time()))

    def remaining_today(self) -> int:
        self._reset_stale_windows()
        daily = KNOWN_LIMITS.get(self.provider, {}).get("req_per_day", 9999)
        return max(0, daily - self.req_today)

    # ------------------------------------------------------------------ #
    # Мутации                                                              #
    # ------------------------------------------------------------------ #

    def record_request(self, tokens_used: int = 0) -> None:
        self._reset_stale_windows()
        self.req_this_min += 1
        self.req_today    += 1
        self.tokens_today += tokens_used

    def record_429(self) -> None:
        self.total_429s += 1
        cooldown = COOLDOWN_AFTER_429.get(self.provider, 120)
        # Прогрессивный кулдаун: каждый следующий 429 дольше
        self.limited_until = time.time() + cooldown * min(self.total_429s, 4)
        # Если много 429 — считаем дневной лимит исчерпанным
        if self.total_429s >= 5:
            daily = KNOWN_LIMITS.get(self.provider, {}).get("req_per_day", 9999)
            self.req_today = daily

    def reset_429(self) -> None:
        """Сбросить кулдаун (вызывается при ↻ обновлении или смене ключей)."""
        self.limited_until = 0.0
        self.total_429s    = 0

    # ------------------------------------------------------------------ #
    # Сериализация                                                         #
    # ------------------------------------------------------------------ #

    def to_dict(self) -> dict:
        return {
            "slot_idx":      self.slot_idx,
            "req_today":     self.req_today,
            "tokens_today":  self.tokens_today,
            "day_start":     self.day_start,
            "limited_until": self.limited_until,
            "total_429s":    self.total_429s,
        }

    def from_dict(self, d: dict) -> None:
        self.req_today     = d.get("req_today",     0)
        self.tokens_today  = d.get("tokens_today",  0)
        self.day_start     = d.get("day_start",     _utc_day_start())
        self.limited_until = d.get("limited_until", 0.0)
        self.total_429s    = d.get("total_429s",    0)
        self._reset_stale_windows()   # автосброс если новый день


# --------------------------------------------------------------------------- #
# Главный класс                                                                #
# --------------------------------------------------------------------------- #

class TokenBudget:
    """
    Управляет ротацией ключей и отслеживает квоты всех провайдеров.

    Используй singleton: token_budget.get()
    """

    # Базовые env-переменные провайдеров
    _PROVIDER_ENV: dict[str, str] = {
        "groq":       "GROQ_API_KEY",
        "openrouter": "OPENROUTER_API_KEY",
        "cerebras":   "CEREBRAS_API_KEY",
        "gemini":     "GEMINI_API_KEY",
        "sambanova":  "SAMBANOVA_API_KEY",
        "together":   "TOGETHER_API_KEY",
        "novita":     "NOVITA_API_KEY",
        "mistral":    "MISTRAL_API_KEY",
    }

    def __init__(self) -> None:
        self._slots: dict[str, list[KeySlot]] = {}
        self._save_counter = 0
        self._discover_slots()
        self._load_persisted()

    # ------------------------------------------------------------------ #
    # Инициализация                                                        #
    # ------------------------------------------------------------------ #

    def _discover_slots(self) -> None:
        """Ищет ключи в env: BASE_KEY, BASE_KEY_2, BASE_KEY_3, ..."""
        for provider, base_env in self._PROVIDER_ENV.items():
            slots: list[KeySlot] = []

            # Первичный ключ
            key = os.environ.get(base_env, "")
            if key:
                slots.append(KeySlot(provider, key, slot_idx=0))

            # Дополнительные ключи: GROQ_API_KEY_2, GROQ_API_KEY_3, ...
            for i in range(2, 8):
                extra = os.environ.get(f"{base_env}_{i}", "")
                if extra:
                    slots.append(KeySlot(provider, extra, slot_idx=i - 1))

            if slots:
                self._slots[provider] = slots

        # Ollama — локальный, без ключа
        self._slots["ollama"] = [KeySlot("ollama", "local", slot_idx=0)]

    def _load_persisted(self) -> None:
        if not BUDGET_FILE.exists():
            return
        try:
            data: dict = json.loads(BUDGET_FILE.read_text(encoding="utf-8"))
            for provider, slot_list in data.items():
                if provider not in self._slots:
                    continue
                for entry in slot_list:
                    idx = entry.get("slot_idx", 0)
                    if idx < len(self._slots[provider]):
                        self._slots[provider][idx].from_dict(entry)
        except Exception as exc:
            logger.warning("TokenBudget: не удалось загрузить %s: %s", BUDGET_FILE, exc)

    # ------------------------------------------------------------------ #
    # Публичное API                                                        #
    # ------------------------------------------------------------------ #

    def get_active_key(self, provider: str) -> Optional[str]:
        """
        Возвращает первый доступный ключ провайдера.
        В session-режиме (друг через туннель) — ТОЛЬКО его ключ, не хоста.
        Если все ключи исчерпаны — None.
        """
        # Session mode: ключ друга (или None) — ключи хоста не отдаём
        try:
            from . import session_keys as _sk
            if _sk.is_session_mode():
                return _sk.get_session_key(provider) or None
        except Exception:
            pass
        for slot in self._slots.get(provider, []):
            if slot.is_available:
                return slot.api_key
        return None

    def get_env_key(self, provider: str) -> Optional[str]:
        """
        Возвращает ключ как будто из os.environ, но с ротацией.
        Используется в model_discovery вместо os.environ.get(key).
        """
        return self.get_active_key(provider) or os.environ.get(
            self._PROVIDER_ENV.get(provider, ""), ""
        )

    def record_success(self, provider: str, tokens_used: int = 0,
                       api_key: Optional[str] = None) -> None:
        """Записать успешный запрос."""
        for slot in self._slots.get(provider, []):
            if api_key is None or slot.api_key == api_key:
                slot.record_request(tokens_used)
                break
        self._maybe_save()

    def record_429(self, provider: str, api_key: Optional[str] = None) -> None:
        """Записать rate limit (429). Переключает на следующий ключ."""
        slots = self._slots.get(provider, [])
        for slot in slots:
            if api_key is None or slot.api_key == api_key:
                slot.record_429()
                logger.warning(
                    "TokenBudget: 429 на %s (слот %d), кулдаун %ds",
                    provider, slot.slot_idx, slot.cooldown_left()
                )
                break
        self.save()

    def is_provider_available(self, provider: str) -> bool:
        return any(s.is_available for s in self._slots.get(provider, []))

    def get_cooldown_expired_providers(self) -> list[str]:
        """Список провайдеров, чей кулдаун истёк → можно восстановить."""
        result = []
        for provider, slots in self._slots.items():
            for slot in slots:
                # Кулдаун был выставлен, но уже прошёл
                if slot.limited_until > 0 and slot.cooldown_left() == 0:
                    result.append(provider)
                    break
        return result

    def reset_cooldowns(self, provider: Optional[str] = None) -> None:
        """Сбросить кулдауны (при ↻ рефреше или смене ключей)."""
        targets = [provider] if provider else list(self._slots.keys())
        for p in targets:
            for slot in self._slots.get(p, []):
                slot.reset_429()
        self.save()

    def keys_count(self, provider: str) -> int:
        return len(self._slots.get(provider, []))

    # ------------------------------------------------------------------ #
    # Summary для UI                                                       #
    # ------------------------------------------------------------------ #

    def get_summary(self) -> list[dict]:
        """Возвращает список провайдеров с оставшейся квотой (для UI)."""
        result = []
        for provider, slots in self._slots.items():
            limits      = KNOWN_LIMITS.get(provider, {})
            daily_limit = limits.get("req_per_day", 9999)
            keys_count  = len(slots)

            total_remaining = sum(s.remaining_today() for s in slots)
            total_used      = sum(s.req_today for s in slots)
            total_tokens    = sum(s.tokens_today for s in slots)
            cooldown_min    = min((s.cooldown_left() for s in slots), default=0)
            available       = any(s.is_available for s in slots)
            total_limit     = daily_limit * keys_count

            pct_used = round(total_used / max(1, total_limit) * 100)

            result.append({
                "provider":        provider,
                "available":       available,
                "keys_count":      keys_count,
                "used_today":      total_used,
                "tokens_today":    total_tokens,
                "remaining_today": total_remaining,
                "daily_limit":     total_limit,
                "pct_used":        pct_used,
                "cooldown_sec":    cooldown_min,
                "is_local":        provider == "ollama",
            })

        # Сначала доступные, потом по убыванию оставшейся квоты
        result.sort(key=lambda x: (not x["available"], -x["remaining_today"]))
        return result

    # ------------------------------------------------------------------ #
    # Персистентность                                                      #
    # ------------------------------------------------------------------ #

    def _maybe_save(self) -> None:
        self._save_counter += 1
        if self._save_counter % 5 == 0:
            self.save()

    def save(self) -> None:
        try:
            BUDGET_FILE.parent.mkdir(parents=True, exist_ok=True)
            data = {
                p: [s.to_dict() for s in slots]
                for p, slots in self._slots.items()
            }
            BUDGET_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8"
            )
        except Exception as exc:
            logger.warning("TokenBudget: не удалось сохранить: %s", exc)


# --------------------------------------------------------------------------- #
# Singleton                                                                    #
# --------------------------------------------------------------------------- #

_instance: Optional[TokenBudget] = None


def get() -> TokenBudget:
    """Возвращает singleton TokenBudget."""
    global _instance
    if _instance is None:
        _instance = TokenBudget()
    return _instance


def reset() -> None:
    """Пересоздать singleton (при перезапуске или смене .env)."""
    global _instance
    _instance = None
