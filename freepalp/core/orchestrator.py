# -*- coding: utf-8 -*-
"""Orchestrator – центральный контроллер FreePalp.

Provides a thread‑safe, async‑aware orchestrator with graceful shutdown,
type hints and proper error handling.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path
from typing import Any, Dict, Optional

# ----------------------------------------------------------------------
# Optional third‑party imports – guarded to keep core usable without them
# ----------------------------------------------------------------------
try:
    import pika  # noqa: F401  (RabbitMQ support, optional)
except ImportError:  # pragma: no cover
    pika = None  # type: ignore[assignment]

# ----------------------------------------------------------------------
# Local imports
# ----------------------------------------------------------------------
from .models import (
    AgentMessage,
    CriticFeedback,
    TaskRequest,
    TaskResult,
    TaskStatus,
    TaskType,
)
from .task_parser import parse_task
from .router import Router
from . import prompt_loader
from .session_logger import SessionLogger
from .user_profile import UserProfile
from .self_improvement.controller import SelfImprovementController
from ..agents.architect_agent import ArchitectAgent
from ..agents.critic_agent import CriticAgent
from ..agents.tool_agent import ToolAgent
from ..agents.worker_agent import WorkerAgent
from ..memory.memory_manager import MemoryManager
from .cron_manager import CronManager

# ----------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------
MAX_ITERATIONS = 3
# Простые задачи — максимум 2 попытки (меньше задержек)
_FAST_TASK_TYPES = frozenset({"coding_small", "text", "shell", "search", "general"})
_FAST_MAX_ITERATIONS = 2
STATE_DIR = (Path(__file__).parent.parent / "state").resolve()
RULES_FILE = (Path(__file__).parent.parent / "FREEPALP_RULES.md").resolve()
MAX_BUFFER_SIZE = 10_000  # safeguard against OOM in long‑running sessions

# ----------------------------------------------------------------------
# Logging configuration
# ----------------------------------------------------------------------
logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# ----------------------------------------------------------------------
# Helper print – uses logger (info level) and flushes
# ----------------------------------------------------------------------
def _p(text: str) -> None:
    """Thread‑safe, UTF‑8‑aware print wrapper.

    Uses the module logger at INFO level; falls back to ASCII‑only output
    if the console cannot encode Unicode.
    """
    try:
        logger.info(text)
    except UnicodeEncodeError:
        logger.info(text.encode("ascii", errors="replace").decode("ascii"))

# ----------------------------------------------------------------------
# Детерминированная проверка: просили сохранить файл, но write_file/write_source
# не вызывались, а ответ содержит код текстом — модель "галлюцинирует" tool call.
# ----------------------------------------------------------------------
import re as _re

_FILE_REQUEST_RE = _re.compile(
    r"(сохрани|сохранить|создай файл|создать файл|запиши в файл|"
    r"в .{0,15}файл|файле|файлом|файл[аеу]?\b|"
    r"в (папк|директори)|реализ\w+ в |создай в |положи в |"
    r"[ (]\S+/[\w.\-]+|"  # путь вида sandbox/..., dir/file.py — подразумевает запись на диск
    r"save (it |this )?(to|as|in) a? ?file|create a? ?file|write .* to a? ?file|"
    r"(in|to) a? ?(folder|directory)|implement .{0,30} in \S+/|"
    r"single file|one file|in one file)",
    _re.IGNORECASE,
)
_CODE_BLOCK_RE = _re.compile(r"```")
_FILE_TOOLS = {"write_file", "write_source"}  # create_dir не пишет содержимое — не считается

# Явный запрос на создание: глагол + объект. При таком запросе отсутствие файловых
# tool-calls — провал, даже если в ответе нет код-блока (агент «рассказал» вместо «сделал»).
_STRONG_FILE_REQUEST_RE = _re.compile(
    r"(созда(й|ть)|сохрани|запиши|собери|положи|сложи|create|save|write|make)\b"
    r".{0,80}?(папк|файл|директори|отч[её]т|folder|directory|file|report|\.\w{2,4}\b)",
    _re.IGNORECASE | _re.DOTALL,
)

# Признаки того, что вместо реального содержимого записана заглушка-отсылка
_PLACEHOLDER_RE = _re.compile(
    r"(из предыдущего сообщения|как (указано|показано) выше|see (the )?(code )?above|"
    r"as shown above|as previously|код выше|без изменений|same as before|"
    r"\[(полный код|full code|код игры)[^\]]*\])",
    _re.IGNORECASE,
)


def _detect_unfulfilled_file_request(user_input: str, worker_output: str, tools_used: list[dict]) -> Optional[str]:
    """Возвращает текст проблемы, если пользователь явно просил сохранить/создать файл,
    а агент либо не вызвал write_file/write_source, либо записал в него заглушку
    вместо реального содержимого."""
    _filename_mentioned = _re.search(r"\b[\w\-а-яё]+\.[a-z]{2,5}\b", (user_input or "").lower())
    if not (_FILE_REQUEST_RE.search(user_input or "")
            or _STRONG_FILE_REQUEST_RE.search(user_input or "")
            or _filename_mentioned):
        return None

    write_calls = [t for t in tools_used if t["tool"] in _FILE_TOOLS]

    if not write_calls:
        strong = bool(_STRONG_FILE_REQUEST_RE.search(user_input or ""))
        dir_calls = [t for t in tools_used if t["tool"] == "create_dir"]
        # Слабый сигнал («файл» упомянут вскользь) — флагуем только если агент
        # напечатал код текстом. Явный запрос на создание — флагуем всегда,
        # кроме случая когда просили только папку и create_dir был вызван.
        if not strong and not _CODE_BLOCK_RE.search(worker_output or ""):
            return None
        if strong and dir_calls and not _re.search(r"файл|\.\w{2,4}\b|file|отч[её]т|report",
                                                   user_input or "", _re.IGNORECASE):
            return None
        return (
            "Пользователь просил сохранить/создать файл, но агент не вызвал write_file/write_source "
            "— код показан только текстом, файл на диске не создан/не обновлён. "
            "На этот раз НЕ выводи код в ответе. Вместо этого СРАЗУ выведи tool_call блок ровно такого вида:\n"
            '```tool_call\n{"tool": "write_file", "args": {"path": "имя_файла.html", "content": "<полный код файла>"}}\n```\n'
            "Никакого текста до или после блока на этом шаге."
        )

    # Пользователь назвал конкретные файлы — они должны быть среди записанных
    # (запись вспомогательного скрипта не считается выполнением просьбы).
    requested = set(_re.findall(r"\b([\w\-а-яё]+\.[a-z]{2,5})\b", (user_input or "").lower()))
    if requested:
        written = {(call.get("path") or "").lower().replace("\\", "/").rsplit("/", 1)[-1]
                   for call in write_calls}
        missing = requested - written
        if missing:
            return (
                f"Пользователь явно просил создать файл(ы): {', '.join(sorted(missing))} — "
                "но они не были записаны (write_file вызывался только для других файлов). "
                "Вызови write_file для каждого запрошенного файла с полным содержимым."
            )

    # write_file вызывался — проверяем, что content не заглушка и не пустышка
    for call in write_calls:
        content = call.get("content", "") or ""
        if len(content.strip()) < 50 or _PLACEHOLDER_RE.search(content):
            return (
                "Агент вызвал write_file/write_source, но записал в файл заглушку "
                f"вместо реального содержимого (фрагмент: {content[:120]!r}). "
                "На этот раз вызови write_file ещё раз с args.content = ПОЛНЫЙ рабочий код файла целиком, "
                "без ссылок на 'предыдущее сообщение' и без сокращений."
            )

    return None


# Задача-починка: переписывать файл, не прочитав его — запрещено
_FIX_REQUEST_RE = _re.compile(r"(почин|исправ|поправ|почему не работает|\bfix\b|repair|broken)", _re.IGNORECASE)


def _detect_blind_rewrite(user_input: str, tools_used: list[dict]) -> Optional[str]:
    """Возвращает проблему, если в задаче-починке агент записал файл, не прочитав
    его сначала (наблюдалось: «почини» = переписал с нуля хуже и потерял функционал)."""
    if not _FIX_REQUEST_RE.search(user_input or ""):
        return None
    def _base(p):
        return (p or "").lower().replace("\\", "/").rsplit("/", 1)[-1]
    for i, t in enumerate(tools_used):
        if t.get("tool") in _FILE_TOOLS and t.get("path"):
            target = _base(t["path"])
            was_read = any(x.get("tool") in ("read_file", "read_source")
                           and _base(x.get("path", "")) == target
                           for x in tools_used[:i])
            if not was_read:
                return (
                    f"Это задача-починка, но агент переписал {t['path']} НЕ прочитав его "
                    "(read_file не вызывался для этого файла). Так теряется рабочий код. "
                    "Сначала вызови read_file, найди конкретную проблему и внеси точечное "
                    "исправление, сохранив остальной код."
                )
    return None


# Ответ начинается с системной ошибки воркера ([Провайдер ...], [Ошибка ...]).
# Наблюдалось: критик дал 0.85 за «[Провайдер novita-ai не поддерживается]»;
# затем «[Ошибка Gemini: 429 ...]» с вложенными ] прошла мимо строгого варианта
# регекса и при недоступном критике получила проходные 0.72. Поэтому матчим
# только префикс: легитимный ответ так не начинается.
_SYSTEM_ERROR_ANSWER_RE = _re.compile(
    r"^\s*\[(Провайдер|Ошибка|Provider|Error)", _re.IGNORECASE)


def _detect_system_error_answer(answer: str) -> Optional[str]:
    """Возвращает проблему, если финальный ответ — голое сообщение об ошибке."""
    if _SYSTEM_ERROR_ANSWER_RE.match(answer or ""):
        return (
            f"Ответ агента — системное сообщение об ошибке ({(answer or '').strip()[:80]}), "
            "а не ответ пользователю. Нужен ретрай на другой модели."
        )
    return None


def _detect_leaked_tool_call(answer: str) -> Optional[str]:
    """Финальный ответ содержит сырой протокол tool calls — неисполненный вызов
    или маркер __NATIVE_TOOL__. Наблюдалось на экзамене: последний write_file
    не исполнился, его tool_call-блок стал «ответом», критик дал 0.96."""
    a = answer or ""
    if "__NATIVE_TOOL__" in a or "```tool_call" in a:
        return (
            "Финальный ответ содержит сырой tool_call-блок — это неисполненная "
            "команда, а не ответ пользователю. Заверши начатые действия "
            "инструментами и дай человеку текстовый итог: что сделано, где лежит."
        )
    return None


# Инструменты, создающие/меняющие файлы — их результаты нельзя терять при ретрае
_WORK_TOOLS = {"write_file", "write_source", "create_dir", "copy_file"}


def _preserve_done_work_hint(tools_used: list) -> Optional[str]:
    """Если попытка провалилась ПОСЛЕ реальной tool-работы — ретрай должен
    продолжить с места провала, а не пересоздавать всё с нуля (наблюдалось:
    groq создал каркас, финальный вызов упал в 429, работа выброшена)."""
    done = []
    for t in tools_used or []:
        if t.get("tool") in _WORK_TOOLS:
            arg = t.get("args", {}) if isinstance(t.get("args"), dict) else {}
            path = arg.get("path") or ""
            done.append(f"{t['tool']}({path})" if path else t["tool"])
    if not done:
        return None
    return (
        "Прошлая попытка УЖЕ выполнила часть работы: " + ", ".join(done[:10]) +
        ". НЕ пересоздавай это с нуля — проверь сделанное, доделай недостающее "
        "и дай финальный текстовый ответ."
    )


# Вопрос об идентичности агента + чужие бренды, которыми слабые модели себя называют
_IDENTITY_QUESTION_RE = _re.compile(
    r"(как тебя зовут|кто ты\b|ты кто\b|какая ты модель|тво[её] имя|представься|"
    r"what('s| is) your name|who are you|which (model|ai) are you)",
    _re.IGNORECASE,
)
_WRONG_IDENTITY_RE = _re.compile(
    r"(меня зовут|я|зовут|my name is|i am|i'm|name's)[\s:—-]{0,3}"
    r"(chatgpt|chat gpt|gpt-?[345o]|claude|gemini|llama|qwen|mistral|deepseek|copilot|bard)\b",
    _re.IGNORECASE,
)


def _detect_identity_violation(user_input: str, worker_output: str) -> Optional[str]:
    """Возвращает текст проблемы, если на вопрос об идентичности агент
    назвался чужим брендом (галлюцинация слабых моделей, критик это пропускает)."""
    if not _IDENTITY_QUESTION_RE.search(user_input or ""):
        return None
    out = worker_output or ""
    m = _WRONG_IDENTITY_RE.search(out)
    if m and "freepalp" not in out.lower():
        return (
            f"Агент назвался чужим брендом ({m.group(2)!r}) вместо FreePalp. "
            "Ты — FreePalp, мульти-агентный AI-оркестратор. Ответь на вопрос заново, "
            "представившись именно FreePalp, не упоминая названия базовых моделей."
        )
    return None


# ----------------------------------------------------------------------
# Orchestrator implementation
# ----------------------------------------------------------------------
class Orchestrator:
    """Central controller of FreePalp.

    Parameters
    ----------
    session_id: Optional[str]
        Identifier of the current user session; if ``None`` a new UUID is generated.
    router: Optional[Router]
        Allows dependency injection for testing.
    memory: Optional[MemoryManager]
        Allows mocking of the memory subsystem.
    """

    def __init__(
        self,
        session_id: Optional[str] = None,
        *,
        router: Optional[Router] = None,
        memory: Optional[MemoryManager] = None,
    ) -> None:
        self.router: Router = router or Router()
        self.tool_agent: ToolAgent = ToolAgent()
        self.memory: MemoryManager = memory or MemoryManager()
        self.user_profile: UserProfile = UserProfile()
        self.session: SessionLogger = SessionLogger(session_id)
        self.self_improvement: SelfImprovementController = SelfImprovementController(
            model_id="llama-3.3-70b-versatile",
            provider=os.getenv("FREEPALP_IMPROVEMENT_PROVIDER", "groq"),
        )
        self.cron: CronManager = CronManager()
        self._stop_event = asyncio.Event()
        self._ensure_dirs()
        # Hook-система: реестр авто-триггеров на события (вдохновлено Claude Flow)
        try:
            from .hooks import HookManager, register_default_hooks
            self.hooks = HookManager()
            register_default_hooks(self.hooks, self)
        except Exception as exc:
            logger.warning("Hooks init failed: %s", exc)
            self.hooks = None
        # Heartbeat – sync call (no async work needed)
        try:
            self.memory.heartbeat()
        except Exception as exc:
            logger.warning("Heartbeat failed: %s", exc)

    # ------------------------------------------------------------------
    # Directory preparation
    # ------------------------------------------------------------------
    def _ensure_dirs(self) -> None:
        """Create required state directories if they do not exist.

        Raises
        ------
        OSError
            If the directory cannot be created (e.g., permission error).
        """
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("Failed to create state directory %s: %s", STATE_DIR, exc)
            raise

    # ------------------------------------------------------------------
    # Public API – main processing entry point
    # ------------------------------------------------------------------
    async def run(
        self,
        user_input: str,
        context: Optional[Dict[str, Any]] = None,
        on_event: Optional[Any] = None,   # async callable(event_dict) — для SSE стриминга
    ) -> TaskResult:
        """Process a user request and return the final ``TaskResult``.

        The method performs the full pipeline:
        1️⃣ Cron handling
        2️⃣ Logging of user input
        3️⃣ Profile update
        4️⃣ Task parsing
        5️⃣ Enrichment with hot memory & user context
        6️⃣ Iterative execution (max ``MAX_ITERATIONS``) with routing,
           optional architect/critic feedback and possible retries.
        7️⃣ Result logging and return.
        """
        start_time = time.time()

        async def _emit(event: dict):
            """Отправляет прогресс-событие если задан on_event callback."""
            if on_event:
                try:
                    await on_event(event)
                except Exception:
                    pass

        async def _on_token(delta: str):
            """Форвардит токен-дельту ответа воркера в SSE-поток."""
            await _emit({"type": "token", "delta": delta})

        # 0️⃣ session_start hook → авто-восстановление провайдеров (cooldown)
        if self.hooks:
            await self.hooks.fire("session_start")
        else:
            # Fallback если hooks недоступны
            try:
                from . import token_budget as _tb
                for _prov in _tb.get().get_cooldown_expired_providers():
                    self.router.restore_provider(_prov)
            except Exception:
                pass

        # -1️⃣ Detect self-improvement intent → run actual improvement cycle
        _SELF_IMPROVE_TRIGGERS = (
            "улучши себя", "улучшить себя", "самоулучшение", "само улучшение",
            "запусти улучшение", "запустить улучшение", "improve yourself",
            "run self-improvement", "self improve", "/improve",
            "сделай себя лучше", "оптимизируй промпты", "улучши промпты",
            "проанализируй и улучши", "запусти цикл улучшения",
        )
        _inp_low = user_input.lower().strip()
        if any(t in _inp_low for t in _SELF_IMPROVE_TRIGGERS):
            await _emit({"type": "stage", "stage": "parsing", "text": "Запускаю цикл самоулучшения..."})
            try:
                report = await self.self_improvement.run(force=True, max_candidates=3)
                if report.get("version_activated"):
                    ver = report.get("version_proposed", "?")
                    changes = report.get("changes", [])
                    ch_lines = "\n".join(
                        f"  • **{c['component']}[{c.get('task_type','all')}]**: {c.get('reason','')[:80]}"
                        for c in changes
                    )
                    answer = (
                        f"✅ **Цикл самоулучшения завершён** — активирована версия **v{ver}**\n\n"
                        f"**Что изменено ({len(changes)} компонента):**\n{ch_lines}\n\n"
                        f"**Тесты:** {'✓ прошли' if report['test_passed'] else '✗ не прошли'}\n\n"
                        f"💡 Нажмите кнопку **«Перезапустить»** в разделе Метрики чтобы применить изменения."
                    )
                    # НЕ делаем авто-рестарт здесь — SSE-поток ещё открыт,
                    # и внезапное закрытие сервера выглядит как крэш.
                    # Пользователь использует кнопку Restart в UI (gateway обрабатывает reconnect).
                elif report.get("error"):
                    answer = f"⚠️ Самоулучшение: {report['error']}"
                else:
                    cands = self.self_improvement.evaluator.analyze(
                        self.self_improvement.evaluator.load_recent(50)
                    )
                    if cands:
                        lines = "\n".join(f"  • {c['component']}/{c['task_type']}: {c['problem'][:70]}" for c in cands[:5])
                        answer = f"🔍 Нашёл {len(cands)} кандидата на улучшение, но LLM не смог сгенерировать изменения.\n\n{lines}"
                    else:
                        answer = "✅ Система работает хорошо — кандидатов на улучшение нет."
                await _emit({"type": "stage", "stage": "worker", "text": "Готово"})
                return TaskResult(
                    task_id="self-improve",
                    status=TaskStatus.COMPLETED,
                    final_answer=answer,
                    model_used="self-improvement-engine",
                    iterations=1,
                    elapsed_seconds=0.0,
                    messages=[],
                    critic_feedback=None,
                )
            except Exception as e:
                _p(f"[Self-improve chat] Error: {e}")

        # 0️⃣ Cron tick – execute overdue cron jobs
        cron_handlers = {
            "__memory_cleanup__": self._cron_memory_cleanup,
            "__weekly_digest__": self._cron_weekly_digest,
        }
        executed = await self.cron.tick(cron_handlers)
        if executed:
            _p(f"[Cron] Executed: {', '.join(executed)}")

        # 0b️⃣ Lazy discovery – initialise router only once
        if not getattr(self.router, "is_ready", False):
            await self.router.initialize()

        # 1️⃣ Log user input (guard against I/O errors)
        try:
            self.session.log_user(user_input)
        except OSError as exc:
            logger.warning("Failed to log user input: %s", exc)

        # 2️⃣ Auto‑detect user profile fields
        saved_fields = self.user_profile.scan_and_update(user_input)
        if saved_fields:
            _p(f"[Profile] Saved to USER.md: {', '.join(saved_fields)}")

        # 3️⃣ Parse the task description
        await _emit({"type": "stage", "stage": "parsing", "text": "Анализирую задачу..."})
        request: TaskRequest = parse_task(user_input)
        if context:
            request.context.update(context)

        task_key_str = request.task_type.value if request.task_type else "general"
        complexity_val = request.context.get("complexity", 1)
        await _emit({"type": "stage", "stage": "parsed",
                     "text": f"Тип: {task_key_str} · сложность {complexity_val}/5",
                     "task_type": task_key_str, "complexity": complexity_val})

        # 4️⃣ Enrich request with hot memory and user context.
        # Lean-режим для тривиальных задач: «привет», «2+2» не нуждаются в полной
        # HOT-памяти и семантическом recall — это раздувало промпт до ~15K токенов
        # и замедляло ответ. Для complexity ≤1 и лёгких типов даём только шапку
        # версии + краткий профиль пользователя.
        _LEAN_TYPES = {"general", "text"}
        _is_lean = (complexity_val <= 1 and task_key_str in _LEAN_TYPES
                    and not request.context.get("conversation_history"))

        version_header = f"[Активное ядро FreePalp: v{prompt_loader.get_version()}]"
        user_ctx = self.user_profile.get_context_for_prompt()

        if _is_lean:
            request.context["agent_memory"] = "\n\n".join(
                filter(None, [version_header, user_ctx])
            )
            request.context["lean_mode"] = True
        else:
            hot_mem = self.memory.load_hot()
            # Семантический recall: релевантные воспоминания по смыслу задачи
            recalled = ""
            try:
                if hasattr(self.memory, "recall"):
                    hits = self.memory.recall(request.user_input, k=3)
                    if hits:
                        lines = [f"- ({h['layer']}) {h['text']}" for h in hits if h.get("score", 0) >= 0.1]
                        if lines:
                            recalled = "Релевантный опыт из памяти:\n" + "\n".join(lines)
            except Exception as exc:
                logger.debug("Semantic recall failed: %s", exc)

            # Teacher→skill: инжектим накопленные приёмы для похожих задач —
            # дешёвая модель получает рабочую процедуру ДО первой попытки.
            skills = ""
            try:
                from . import skill_library as _sl
                skills = _sl.get().find_relevant(task_key_str, request.user_input)
            except Exception as exc:
                logger.debug("Skill recall failed: %s", exc)

            if hot_mem or user_ctx or recalled or skills:
                request.context["agent_memory"] = "\n\n".join(
                    filter(None, [version_header, user_ctx, hot_mem, recalled, skills])
                )

        # 5️⃣ Main execution loop (retry up to MAX_ITERATIONS)
        result: Optional[TaskResult] = None
        prev_feedback: Optional[str] = None

        max_iters = _FAST_MAX_ITERATIONS if task_key_str in _FAST_TASK_TYPES else MAX_ITERATIONS

        from . import prompt_loader as _pl
        _retry_threshold = _pl.get_retry_threshold()
        for iteration in range(max_iters):
            # Route → pick model
            model_config = self.router.route(request)

            # Print routing info
            task_key = request.task_type.value if request.task_type else "general"
            complexity = request.context.get("complexity", 1)
            print(f"\n[>] Task type : {task_key}")
            print(f"[>] Complexity: {complexity}/5")
            print(f"[*] Worker model: {model_config.name} ({model_config.provider})\n")
            print(f"[~] Iteration {iteration + 1}/{max_iters}")

            await _emit({"type": "stage", "stage": "routing",
                         "text": f"Модель: {model_config.name} ({model_config.provider})",
                         "model": model_config.name, "provider": model_config.provider,
                         "iteration": iteration + 1, "max_iters": max_iters})

            # Execute Worker
            worker = WorkerAgent(model_config, self.tool_agent)
            print("  [W] Worker running...")
            await _emit({"type": "stage", "stage": "worker",
                         "text": f"Генерирую ответ{'...' if iteration == 0 else ' (повтор ' + str(iteration+1) + ')...'}",
                         "iteration": iteration + 1})
            worker_msg = await worker.run(request, iteration=iteration,
                                          prev_feedback=prev_feedback,
                                          on_token=_on_token if on_event else None)

            # Fallback loop: try up to 3 different models if provider fails
            _fallback_attempts = 0
            while worker_msg.content.startswith("[Ошибка") and _fallback_attempts < 3:
                _fallback_attempts += 1
                err_txt = worker_msg.content
                logger.warning("Worker error (attempt %d): %s", _fallback_attempts, err_txt[:80])
                # If rate-limit — mark entire provider unavailable, not just this model
                if "429" in err_txt or "rate-limit" in err_txt or "rate limit" in err_txt.lower():
                    self.router.mark_provider_unavailable(model_config.provider)
                    logger.warning("Provider %s marked unavailable (rate limit)", model_config.provider)
                    # provider_429 hook → запись в TokenBudget для ротации ключей
                    if self.hooks:
                        await self.hooks.fire("provider_429", provider=model_config.provider)
                    else:
                        try:
                            from . import token_budget as _tb
                            _tb.get().record_429(model_config.provider)
                        except Exception:
                            pass
                else:
                    self.router.mark_unavailable(model_config.name)
                try:
                    model_config = self.router.route(request)
                except RuntimeError:
                    logger.error("No available models after %d fallbacks", _fallback_attempts)
                    break
                worker = WorkerAgent(model_config, self.tool_agent)
                logger.info("Fallback to %s (%s)", model_config.name, model_config.provider)
                await _emit({"type": "stage", "stage": "fallback",
                             "text": f"Fallback → {model_config.name} ({model_config.provider})",
                             "model": model_config.name})
                worker_msg = await worker.run(request, iteration=iteration,
                                              prev_feedback=prev_feedback,
                                              on_token=_on_token if on_event else None)

            # Учёт реальных трат в TokenBudget — иначе квоты в UI всегда
            # рисуются нетронутыми (record_success раньше не звался вообще)
            try:
                from . import token_budget as _tb
                _wt = (worker_msg.metadata.get("tokens_in", 0)
                       + worker_msg.metadata.get("tokens_out", 0))
                _tb.get().record_success(model_config.provider, _wt)
            except Exception:
                pass

            # Report tool calls used
            tools_used = worker_msg.metadata.get("tools_called", [])
            for t in tools_used:
                await _emit({"type": "tool", "tool": t["tool"], "text": f"🔧 Использовал: {t['tool']}"})

            # ── Ярус 1: детерминированные проверки (бесплатно, мгновенно) ──
            # Если типовой провал пойман — LLM-критик не нужен, экономим токены.
            # Раньше критик звался всегда и пропускал эти случаи (давал 0.9+).
            cheap_issues = []
            sys_error = _detect_system_error_answer(worker_msg.content)
            if sys_error:
                cheap_issues.append(sys_error)
            leaked_call = _detect_leaked_tool_call(worker_msg.content)
            if leaked_call:
                cheap_issues.append(leaked_call)
            # Провал после реальной работы → ретраю передаём список сделанного,
            # чтобы он продолжил, а не начинал с нуля
            if (sys_error or leaked_call):
                work_hint = _preserve_done_work_hint(tools_used)
                if work_hint:
                    cheap_issues.append(work_hint)
            unfulfilled = _detect_unfulfilled_file_request(request.user_input, worker_msg.content, tools_used)
            if unfulfilled:
                cheap_issues.append(unfulfilled)
            wrong_identity = _detect_identity_violation(request.user_input, worker_msg.content)
            if wrong_identity:
                cheap_issues.append(wrong_identity)
            blind_rewrite = _detect_blind_rewrite(request.user_input, tools_used)
            if blind_rewrite:
                cheap_issues.append(blind_rewrite)

            if cheap_issues:
                print("  [C] Детерминированная проверка поймала провал — LLM-критик пропущен")
                await _emit({"type": "stage", "stage": "critic",
                             "text": "Провал пойман без LLM-критика (ярус 1)"})
                feedback = CriticFeedback(passed=False, score=0.3,
                                          issues=cheap_issues, must_retry=True)
            elif request.context.get("lean_mode"):
                # Тривиальный чат («привет», «2+2») не нуждается в LLM-критике —
                # он придирался к приветствию как к code review (0.00) и форсил
                # ненужный retry. Детерминированные проверки уже прошли — этого
                # достаточно. Это главный источник тормозов на простых запросах.
                print("  [C] Lean-режим — LLM-критик пропущен")
                feedback = CriticFeedback(passed=True, score=0.9,
                                          issues=[], must_retry=False)
            else:
                # ── Ярус 2: LLM-критик для неоднозначного ──
                critic_model = self.router.get_critic_model()
                critic_agent = CriticAgent(critic_model)
                print("  [C] Critic evaluating...")
                await _emit({"type": "stage", "stage": "critic", "text": "Проверяю качество ответа..."})
                _crit_msg, feedback = await critic_agent.evaluate(request, worker_msg.content, iteration)
                try:
                    from . import token_budget as _tb
                    _ct = (_crit_msg.metadata.get("tokens_in", 0)
                           + _crit_msg.metadata.get("tokens_out", 0)) if _crit_msg else 0
                    _tb.get().record_success(critic_model.provider, _ct)
                except Exception:
                    pass

            score = feedback.score if feedback else 0.0
            blocks = int(score * 10)
            bar = "#" * blocks + "." * (10 - blocks)
            print(f"  [C] Score: [{bar}] {score:.2f}")
            if feedback and feedback.issues:
                print(f"  [!] Issues: {', '.join(feedback.issues[:3])}")

            await _emit({"type": "scored", "score": score,
                         "text": f"Оценка: {int(score*100)}%",
                         "issues": (feedback.issues[:2] if feedback and feedback.issues else [])})

            # Build TaskResult
            result = TaskResult(
                task_id=request.task_id if hasattr(request, "task_id") else f"task_{iteration}",
                status=TaskStatus.COMPLETED if score >= _retry_threshold else TaskStatus.FAILED,
                final_answer=worker_msg.content,
                model_used=model_config.model_id,
                iterations=iteration + 1,
                elapsed_seconds=worker_msg.metadata.get("elapsed", 0.0),
                messages=[worker_msg],
                critic_feedback=feedback,
            )

            # low_score hook → авто-обучение (correction в память)
            if score < 0.5 and feedback and feedback.issues:
                if self.hooks:
                    await self.hooks.fire(
                        "low_score", task_type=task_key,
                        user_input=request.user_input, score=score,
                        issues=feedback.issues,
                    )
                try:
                    from ..memory.consolidation import add_to_long_term
                    lesson = f"Ошибка [{task_key}]: {feedback.issues[0]}"
                    add_to_long_term("lesson", lesson)
                except Exception:
                    pass

            if score >= _retry_threshold:
                # Teacher→skill (полная дистилляция, идея из Odysseus): успех
                # ПОСЛЕ провала — ценный приём. Сохраняем процедуру в SKILL.md,
                # чтобы в следующий раз дешёвая модель справилась с 1-й итерации,
                # а не сжигала ретраи (коррекция накапливается, а не сгорает).
                if iteration > 0:
                    try:
                        from . import skill_library as _sl
                        _sl.get().save_skill(
                            task_type=task_key,
                            user_input=request.user_input,
                            tools_used=tools_used,
                            model_name=model_config.name,
                            overcame_issue=prev_feedback,
                        )
                    except Exception:
                        pass
                break

            prev_feedback = "\n".join(feedback.issues) if feedback and feedback.issues else ""
            print(f"  [>] Retry...")
            await _emit({"type": "stage", "stage": "retry",
                         "text": f"Балл {int(score*100)}% — улучшаю ответ..."})

        print(f"\n  [$$] Tokens: {sum(m.metadata.get('tokens_in',0) for m in result.messages)} in / "
              f"{sum(m.metadata.get('tokens_out',0) for m in result.messages)} out  |  ~${sum(m.metadata.get('cost_usd',0) for m in result.messages):.4f}")
        print(f"Model: {model_config.name} | Score: {result.critic_feedback.score if result.critic_feedback else 0:.2f} | Iters: {result.iterations}")
        print("---")

        # ``result`` is guaranteed to be set after the loop
        assert result is not None

        # 6️⃣ Log final result (guard against I/O errors)
        try:
            self.session.log_assistant(
                result.final_answer,
                model=result.model_used,
                tokens=sum(m.tokens_used for m in result.messages),
            )
        except Exception as exc:
            logger.warning("Failed to log result: %s", exc)

        # 7. Record metrics for self-improvement
        try:
            fb   = result.critic_feedback
            tin  = sum(m.metadata.get("tokens_in",  0)   for m in result.messages)
            tout = sum(m.metadata.get("tokens_out", 0)   for m in result.messages)
            cost = sum(m.metadata.get("cost_usd",   0.0) for m in result.messages)
            self.self_improvement.record_task(
                task_type    = request.task_type.value if request.task_type else "general",
                user_input   = request.user_input,
                critic_score = fb.score if fb else 0.0,
                iterations   = result.iterations,
                model        = result.model_used,
                elapsed      = time.time() - start_time,
                issues       = (fb.issues      or []) if fb else [],
                suggestions  = (fb.suggestions or []) if fb else [],
                tokens_in    = tin,
                tokens_out   = tout,
                cost_usd     = cost,
            )
            # task_complete hook → индексация в эпизодическую память
            if self.hooks:
                await self.hooks.fire(
                    "task_complete",
                    task_type=request.task_type.value if request.task_type else "general",
                    user_input=request.user_input,
                    score=fb.score if fb else 0.0,
                )
            if self.self_improvement.should_autoimprove():
                _p("[Self-improve] Auto-improvement triggered...")
                asyncio.create_task(self._auto_improve_and_restart())
        except Exception as exc2:
            logger.warning("Failed to record metrics: %s", exc2)

        elapsed = time.time() - start_time
        logger.debug("Orchestrator.run completed in %.2f s", elapsed)
        return result

    async def _schedule_restart_after_improve(self) -> None:
        """Планирует рестарт сервера (только для не-SSE контекстов).

        НЕ вызывать пока открыт SSE-поток — сервер закроется, клиент увидит крэш.
        Используй кнопку Restart в UI (gateway.py/_schedule_restart) — она ждёт закрытия потоков.
        """
        try:
            from freepalp.gateway import _schedule_restart
            asyncio.create_task(_schedule_restart(delay=15.0, reason="self-improve via chat"))
        except ImportError:
            pass

    async def _auto_improve_and_restart(self) -> None:
        """Авто-улучшение + перезапуск сервера если версия активирована.

        Ждёт, пока пользователь не активен ≥90с (SI конкурировал за квоту и CPU
        с живыми запросами — наблюдалось: held-out валидация на 15 задачах
        во время ответа пользователю). Максимум ждём 30 минут, потом пропуск —
        счётчик задач снова вызовет SI позже."""
        try:
            try:
                from freepalp import gateway as _gw
                deadline = time.time() + 1800
                while time.time() < deadline:
                    idle = time.time() - getattr(_gw, "_last_activity", 0)
                    if idle >= 90:
                        break
                    await asyncio.sleep(30)
                else:
                    _p("[Self-improve] Пользователь активен 30 мин — SI отложен")
                    return
            except ImportError:
                pass  # CLI-режим — ждать нечего
            report = await self.self_improvement.run()
            if report.get("version_activated"):
                ver = report.get("version_proposed", "?")
                _p(f"[Self-improve] v{ver} activated — scheduling server restart...")
                # Импортируем и вызываем перезапуск из gateway (если запущен)
                try:
                    from freepalp.gateway import _schedule_restart
                    import asyncio as _asyncio
                    _asyncio.create_task(_schedule_restart(
                        delay=30.0,
                        reason=f"auto-improve v{ver}"
                    ))
                except ImportError:
                    pass  # CLI режим — перезапуск не нужен
        except Exception as e:
            _p(f"[Self-improve] Auto-improve error: {e}")

    # ------------------------------------------------------------------
    # Graceful shutdown – can be called from outside (e.g., FastAPI shutdown)
    # ------------------------------------------------------------------
    def get_available_models(self) -> list[dict]:
        """Returns list of available models (for status endpoint)."""
        return [
            {"name": m.name, "provider": m.provider, "tier": m.tier.value}
            for m in self.router.list_available()
        ]

    async def stop(self) -> None:
        """Signal the orchestrator to stop background tasks and clean up."""
        self._stop_event.set()
        try:
            await self.memory.shutdown()
        except Exception:
            pass
        logger.info("Orchestrator stopped")

    # ------------------------------------------------------------------
    # Cron helper implementations (kept minimal for clarity)
    # ------------------------------------------------------------------
    async def _cron_memory_cleanup(self) -> None:
        await self.memory.cleanup()

    async def _cron_weekly_digest(self) -> None:
        await self.self_improvement.generate_weekly_digest()

# Exported symbols for ``from freepalp.core.orchestrator import *``
__all__ = ["Orchestrator", "Router", "MAX_ITERATIONS"]
