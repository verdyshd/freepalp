"""
session_keys — per-session API ключи (схема qclaw: каждый со своими ключами).

Зачем: когда друг подключается через Cloudflare Tunnel, он вводит СВОИ ключи
(хранятся в его браузере), а не пользуется ключами хоста. При этом память и
самообучение остаются общими на хосте — система учится на всех.

Механика:
  - ContextVar хранит ключи текущего запроса (async-task-local, не глобально)
  - В «session mode» (ключи заданы) инференс берёт ТОЛЬКО session-ключи;
    ключи хоста (.env) НИКОГДА не используются для запроса друга
  - В обычном режиме (хост на localhost) — ключи из .env как раньше

Безопасность: если у друга нет ключа провайдера → get_api_key вернёт ""
→ вызов к провайдеру упадёт с 401 → роутер переключится на следующий.
Ключ хоста при этом не тратится.
"""

from __future__ import annotations
import os
import contextvars

# None  → режим хоста (ключи из .env)
# dict  → session mode (ключи друга: {provider: key})
_session: contextvars.ContextVar = contextvars.ContextVar("fp_session_keys", default=None)

# env-переменная → имя провайдера
_ENV_TO_PROVIDER: dict[str, str] = {
    "GROQ_API_KEY":       "groq",
    "OPENROUTER_API_KEY": "openrouter",
    "CEREBRAS_API_KEY":   "cerebras",
    "GEMINI_API_KEY":     "gemini",
    "SAMBANOVA_API_KEY":  "sambanova",
    "TOGETHER_API_KEY":   "together",
    "NOVITA_API_KEY":     "novita",
    "MISTRAL_API_KEY":    "mistral",
    "ANTHROPIC_API_KEY":  "anthropic",
    "OPENAI_API_KEY":     "openai",
}


def set_session_keys(keys: dict | None) -> None:
    """Устанавливает ключи текущего запроса. None — режим хоста."""
    _session.set(keys)


def get_session_keys() -> dict | None:
    return _session.get()


def is_session_mode() -> bool:
    """True если активны session-ключи (запрос друга через туннель)."""
    return _session.get() is not None


def get_api_key(env_var: str) -> str:
    """Возвращает ключ для провайдера с учётом session-режима.

    Session mode → ключ друга для этого провайдера (или '' если у него нет).
    Режим хоста  → os.environ[env_var].
    """
    sk = _session.get()
    if sk is not None:
        prov = _ENV_TO_PROVIDER.get(env_var)
        if not prov:
            return ""
        return sk.get(prov) or ""
    return os.environ.get(env_var, "")


def get_session_key(provider: str) -> str:
    """Ключ сессии по имени провайдера (groq, mistral, ...). '' если нет."""
    sk = _session.get()
    if not sk:
        return ""
    return sk.get(provider) or ""


def session_providers() -> list[str]:
    """Список провайдеров для которых у текущей сессии есть ключ."""
    sk = _session.get()
    if not sk:
        return []
    return [p for p, k in sk.items() if k]
