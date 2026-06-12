"""
HookManager — система авто-триггеров на события (вдохновлено Claude Flow).

Зачем: раньше автоматизации (логирование ошибок, индексация в память,
восстановление провайдеров) были разбросаны по orchestrator.run() как
inline-код. Теперь это РЕЕСТР хуков — обработчики регистрируются на события
и срабатывают автоматически, как в Claude Flow (27 hook-точек).

Преимущества:
  - Расширяемость: новую автоматику добавляешь регистрацией хука, не трогая ядро
  - Изоляция: ошибка в одном хуке не ломает остальные
  - Прозрачность: видно все авто-действия в одном месте

События (hook points):
  session_start      — старт сессии/оркестратора
  task_start         — начало обработки задачи
  task_complete      — задача завершена (score, ответ)
  low_score          — критик поставил низкую оценку (< порога)
  error              — исключение при обработке
  provider_429       — провайдер вернул rate-limit
  memory_consolidate — запуск консолидации памяти
"""

from __future__ import annotations
import asyncio
import logging
from collections import defaultdict
from typing import Callable, Any

logger = logging.getLogger("freepalp.hooks")

# Канонические имена событий
EVENT_SESSION_START      = "session_start"
EVENT_TASK_START         = "task_start"
EVENT_TASK_COMPLETE      = "task_complete"
EVENT_LOW_SCORE          = "low_score"
EVENT_ERROR              = "error"
EVENT_PROVIDER_429       = "provider_429"
EVENT_MEMORY_CONSOLIDATE = "memory_consolidate"

KNOWN_EVENTS = {
    EVENT_SESSION_START, EVENT_TASK_START, EVENT_TASK_COMPLETE,
    EVENT_LOW_SCORE, EVENT_ERROR, EVENT_PROVIDER_429, EVENT_MEMORY_CONSOLIDATE,
}


class HookManager:
    """Реестр хуков: регистрация обработчиков + срабатывание событий."""

    def __init__(self):
        self._handlers: dict[str, list[tuple[int, str, Callable]]] = defaultdict(list)
        self._fired_count: dict[str, int] = defaultdict(int)

    def on(self, event: str, handler: Callable, priority: int = 5, name: str = "") -> None:
        """Регистрирует обработчик на событие.
        priority — меньше = раньше. handler может быть sync или async.
        """
        if event not in KNOWN_EVENTS:
            logger.warning("Неизвестное событие хука: %s", event)
        hook_name = name or getattr(handler, "__name__", "anon")
        self._handlers[event].append((priority, hook_name, handler))
        self._handlers[event].sort(key=lambda x: x[0])

    async def fire(self, event: str, **ctx: Any) -> int:
        """Вызывает все обработчики события. Ошибки изолированы.
        Возвращает кол-во успешно выполненных хуков."""
        handlers = self._handlers.get(event, [])
        if not handlers:
            return 0
        self._fired_count[event] += 1
        ok = 0
        for _prio, name, handler in handlers:
            try:
                res = handler(**ctx)
                if asyncio.iscoroutine(res):
                    await res
                ok += 1
            except Exception as exc:
                logger.debug("Хук %s/%s упал: %s", event, name, exc)
        return ok

    def fire_sync(self, event: str, **ctx: Any) -> int:
        """Синхронный fire — только для sync-хуков (например, при старте)."""
        handlers = self._handlers.get(event, [])
        if not handlers:
            return 0
        self._fired_count[event] += 1
        ok = 0
        for _prio, name, handler in handlers:
            try:
                res = handler(**ctx)
                if asyncio.iscoroutine(res):
                    res.close()   # не можем await в sync-контексте
                    continue
                ok += 1
            except Exception as exc:
                logger.debug("Хук %s/%s упал: %s", event, name, exc)
        return ok

    def stats(self) -> dict:
        """Статистика: сколько хуков на событие и сколько раз сработало."""
        return {
            "registered": {ev: len(hs) for ev, hs in self._handlers.items()},
            "fired":      dict(self._fired_count),
        }

    def list_hooks(self) -> list[dict]:
        """Список всех зарегистрированных хуков."""
        out = []
        for ev, hs in self._handlers.items():
            for prio, name, _ in hs:
                out.append({"event": ev, "name": name, "priority": prio})
        return out


# ──────────────────────────────────────────────────────────────────
# Встроенные хуки — авто-автоматизации FreePalp
# ──────────────────────────────────────────────────────────────────

def register_default_hooks(hm: HookManager, orchestrator) -> None:
    """Регистрирует стандартные авто-хуки FreePalp.

    Вызывается из Orchestrator.__init__ после создания HookManager.
    """

    # 1. low_score → авто-обучение: логируем как correction в память
    def _hook_low_score_learn(task_type="", user_input="", score=0.0,
                              issues=None, **_):
        try:
            issues = issues or []
            problem = issues[0] if issues else f"низкий score {score:.2f}"
            lesson  = f"Тип [{task_type}]: избегать — {problem}"
            orchestrator.memory.log_correction(task_type, problem, lesson)
        except Exception:
            pass
    hm.on(EVENT_LOW_SCORE, _hook_low_score_learn, priority=3, name="low_score_learn")

    # 2. task_complete → индексируем результат в эпизодическую память
    def _hook_task_index(task_type="", user_input="", score=0.0, **_):
        try:
            if hasattr(orchestrator.memory, "remember") and user_input:
                from ..memory.memory_manager import LAYER_EPISODIC
                orchestrator.memory.remember(
                    f"[{task_type}] {user_input[:120]} (score {score:.2f})",
                    layer=LAYER_EPISODIC,
                    meta={"score": score},
                )
        except Exception:
            pass
    hm.on(EVENT_TASK_COMPLETE, _hook_task_index, priority=5, name="task_index")

    # 3. provider_429 → запись в token_budget (прогрессивный cooldown)
    def _hook_429_budget(provider="", **_):
        try:
            from . import token_budget as _tb
            if provider:
                _tb.get().record_429(provider)
        except Exception:
            pass
    hm.on(EVENT_PROVIDER_429, _hook_429_budget, priority=2, name="429_budget")

    # 4. session_start → восстановление провайдеров с истёкшим cooldown
    def _hook_restore_providers(**_):
        try:
            from . import token_budget as _tb
            for prov in _tb.get().get_cooldown_expired_providers():
                orchestrator.router.restore_provider(prov)
        except Exception:
            pass
    hm.on(EVENT_SESSION_START, _hook_restore_providers, priority=1, name="restore_providers")
