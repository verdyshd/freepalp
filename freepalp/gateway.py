"""
FreePalp HTTP Gateway — FastAPI сервер.

Запуск:
  python freepalp/app.py --web
  или: uvicorn freepalp.gateway:app --port 28800

Endpoints:
  GET  /              → WebUI
  POST /api/chat      → выполнить задачу
  GET  /api/status    → статус системы
  GET  /api/models    → список моделей
  GET  /api/providers → провайдеры
  GET  /api/tools     → список инструментов
  GET  /api/memory    → HOT память
  GET  /api/memory/stats    → статистика
  GET  /api/memory/search?q → поиск
  POST /api/memory/clean    → очистка
  GET  /api/crons     → cron задачи
  GET  /api/metrics   → метрики
  POST /api/improve   → самоулучшение
  GET  /docs          → Swagger
"""

from __future__ import annotations
import sys
import re
import json
import asyncio
import logging
import os
import signal
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Windows-консоль по умолчанию cp1251: любой print() текста модели с экзотическим
# символом (напр. BOM ﻿) ронял ВЕСЬ запрос UnicodeEncodeError'ом.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

from typing import Optional
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ──────────────────────────────────────────────────────────────
from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(_app):
    """Современный lifespan (заменяет deprecated on_event)."""
    await _startup_logic()
    yield
    await _shutdown_logic()


app = FastAPI(
    title="FreePalp AI Orchestrator",
    description="Multi-agent AI with ReAct loop and self-correction",
    version="1.0.0",
    lifespan=_lifespan,
)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_STATIC = Path(__file__).parent / "web" / "static"
if _STATIC.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

_orch = None

# ── Conversation history: conv_id → list of {role, content} ──────────────
# Хранит последние 10 обменов (20 сообщений) на сессию.
# По умолчанию "default" — общий чат без явного conv_id.
_conversations: dict[str, list[dict]] = {}
_MAX_HISTORY = 10   # последних обменов (каждый обмен = 2 сообщения)

# ── Настройки системы (config/settings.json) ────────────────────────────
_SETTINGS_PATH = Path(__file__).parent / "config" / "settings.json"
_SETTINGS_DEFAULTS = {
    "idle_shutdown_minutes": 30,   # 0 = не выключаться
    "si_on_startup": False,        # самоулучшение при старте сервера (жжёт квоту!)
    "beacon_shutdown": True,       # гасить сервер при закрытии вкладки
    "retry_threshold": 0.7,        # порог критика для ретрая
}


def load_settings() -> dict:
    s = dict(_SETTINGS_DEFAULTS)
    try:
        if _SETTINGS_PATH.exists():
            s.update(json.loads(_SETTINGS_PATH.read_text("utf-8")))
    except Exception:
        pass
    return s


def save_settings(s: dict) -> None:
    _SETTINGS_PATH.write_text(
        json.dumps(s, ensure_ascii=False, indent=2), "utf-8")


# ── Auto-shutdown ─────────────────────────────────────────────────────────
# Сервер автоматически выключается если нет запросов IDLE_SHUTDOWN_MINUTES.
# Приоритет: env FREEPALP_IDLE_SHUTDOWN > settings.json > 30.
_env_idle = os.environ.get("FREEPALP_IDLE_SHUTDOWN")
IDLE_SHUTDOWN_MINUTES: int = (int(_env_idle) if _env_idle is not None
                              else int(load_settings()["idle_shutdown_minutes"]))
_last_activity: float = time.time()   # обновляется при каждом /api/chat
_shutdown_task: asyncio.Task | None = None
_startup_time:  float = 0.0           # время запуска — для защиты от ранних beacons
_BEACON_GRACE_SEC = 15                # игнорировать /api/shutdown первые N секунд


def _touch_activity() -> None:
    """Обновить время последней активности."""
    global _last_activity
    _last_activity = time.time()


def _get_orch():
    global _orch
    if _orch is None:
        from freepalp.core.orchestrator import Orchestrator
        _orch = Orchestrator()
    return _orch


async def _idle_watchdog() -> None:
    """
    Фоновая корутина: если нет запросов IDLE_SHUTDOWN_MINUTES минут — завершаем процесс.
    Запускается при старте, отменяется при shutdown.
    """
    if IDLE_SHUTDOWN_MINUTES <= 0:
        return   # отключено

    interval = 60   # проверяем раз в минуту
    threshold = IDLE_SHUTDOWN_MINUTES * 60

    while True:
        await asyncio.sleep(interval)
        idle = time.time() - _last_activity
        if idle >= threshold:
            print(f"\n  [AutoShutdown] Нет активности {IDLE_SHUTDOWN_MINUTES} мин → завершаю сервер...\n")
            await _do_shutdown()
            return


async def _do_shutdown() -> None:
    """
    Graceful shutdown: сохраняет состояние и завершает процесс.
    Использует SIGINT через отдельный поток — uvicorn обрабатывает его корректно.
    """
    global _orch
    try:
        from freepalp.memory.session_memory import save_active_sessions
        save_active_sessions(_conversations)
        print("  [Shutdown] Sessions saved")
    except Exception as e:
        print(f"  [Shutdown] Session save error: {e}")
    if _orch is not None:
        try:
            await _orch.stop()
            print("  [Shutdown] Orchestrator stopped")
        except Exception as e:
            print(f"  [Shutdown] Orchestrator stop error: {e}")
    # Посылаем SIGINT из отдельного потока — uvicorn поймает и завершится чисто
    def _send_sigint():
        time.sleep(0.1)
        os.kill(os.getpid(), signal.SIGINT)
    threading.Thread(target=_send_sigint, daemon=True).start()


async def _schedule_restart(delay: float = 3.0, reason: str = "self-improvement") -> None:
    """
    Перезапускает сервер через delay секунд.
    Запускает новый процесс с задержкой (ждёт освобождения порта), затем завершает текущий.
    """
    await asyncio.sleep(delay)
    print(f"  [Restart] Перезапуск после {reason}...")
    try:
        import subprocess as _sp
        from .core.winproc import no_window
        _root = Path(__file__).parent.parent
        if sys.platform == "win32":
            # Новый процесс: ждём 5с (пока освободится порт) → запускаем сервер.
            # no_window() — чтобы не мигало чёрное окно cmd при перезапуске.
            _sp.Popen(
                f'cmd /c "timeout /t 5 /nobreak >nul 2>&1 && python freepalp\\app.py --web"',
                shell=True,
                cwd=str(_root),
                **no_window(),
            )
        else:
            _sp.Popen(
                ["/bin/sh", "-c", f"sleep 5 && python freepalp/app.py --web"],
                cwd=str(_root),
            )
        print("  [Restart] Новый процесс запущен, завершаем текущий...")
    except Exception as e:
        print(f"  [Restart] Ошибка запуска нового процесса: {e}")
    await _do_shutdown()


async def _shutdown_logic():
    """Graceful shutdown — сохраняем состояние перед выходом."""
    global _orch, _shutdown_task

    # Отменяем watchdog если он ещё крутится
    if _shutdown_task is not None and not _shutdown_task.done():
        _shutdown_task.cancel()

    if _orch is not None:
        try:
            # Сохраняем активные сессии на диск (crash recovery при следующем старте)
            from freepalp.memory.session_memory import save_active_sessions
            save_active_sessions(_conversations)
        except Exception as e:
            print(f"  [Shutdown] Session save error: {e}")
        try:
            await _orch.stop()
            print("  [Shutdown] Orchestrator stopped cleanly")
        except Exception as e:
            print(f"  [Shutdown] Orchestrator stop error: {e}")


async def _startup_logic():
    """
    Startup sequence:
      1. Восстанавливаем прерванные диалоги из active_sessions/
      2. Строим дайджест последних сессий → инжектируем в HOT память
      3. Запускаем live discovery моделей
      4. Запускаем watchdog авто-выключения
    """
    global _conversations, _shutdown_task, _startup_time
    _startup_time = time.time()   # фиксируем время старта

    # 1. Восстанавливаем активные сессии (crash recovery)
    try:
        from freepalp.memory.session_memory import load_active_sessions
        restored = load_active_sessions()
        if restored:
            _conversations.update(restored)
            print(f"  [Memory] Restored {len(restored)} active session(s) from disk")
    except Exception as e:
        print(f"  [Memory] Session restore error: {e}")

    # 2. Дайджест последних сессий → HOT память
    try:
        from freepalp.memory.session_memory import get_or_build_digest
        from freepalp.memory.memory_manager import MemoryManager
        digest = get_or_build_digest()
        if digest:
            from freepalp.memory.memory_manager import MEMORY_ROOT
            hot_path = MEMORY_ROOT / "hot_memory.md"
            _inject_digest_into_hot(hot_path, digest)
            print(f"  [Memory] Session digest injected into HOT ({len(digest)} chars)")
    except Exception as e:
        print(f"  [Memory] Digest injection error: {e}")

    # 2b. Автозапуск Ollama (если была подключена раньше) — до discovery,
    #     чтобы локальные модели сразу попали в роутер.
    try:
        from freepalp.core import ollama_autostart as _oa
        status = await asyncio.to_thread(_oa.ensure_running)
        if status == "started":
            print("  [Ollama] Авто-поднята при старте (была подключена ранее)")
        elif status == "failed":
            print("  [Ollama] Была подключена, но поднять не удалось (проверь установку)")
    except Exception as e:
        print(f"  [Ollama] autostart error: {e}")

    # 3. Live discovery
    try:
        orch = _get_orch()
        await orch.router.initialize()
    except Exception:
        pass

    # 3a. MCP-серверы (если сконфигурированы) — в фоне, чтобы npx не тормозил
    #     старт; инструменты регистрируются в ALL_TOOLS по готовности.
    async def _connect_mcp():
        import asyncio as _asyncio
        try:
            from freepalp.core import mcp_client as _mcp
            summary = await _asyncio.to_thread(_mcp.get().connect_all)
            if summary["connected"]:
                print(f"  [MCP] Подключено {len(summary['connected'])} серверов, "
                      f"{summary['tools']} инструментов: {', '.join(summary['connected'])}")
            if summary["failed"]:
                print(f"  [MCP] Не поднялись: {', '.join(summary['failed'])}")
        except Exception as e:
            print(f"  [MCP] Ошибка подключения: {e}")
    asyncio.create_task(_connect_mcp())

    # 3b. Startup self-improvement check — улучшаем если есть проблемные типы задач
    async def _startup_improve():
        import asyncio as _asyncio
        await _asyncio.sleep(5)   # даём серверу полностью запуститься
        try:
            orch = _get_orch()
            if orch.self_improvement.needs_improve_on_startup():
                print("  [SI] Startup check: found improvement candidates — starting auto-improve...")
                report = await orch.self_improvement.run(force=False, max_candidates=3)
                if report.get("version_activated"):
                    print(f"  [SI] Auto-improved on startup -> v{report['version_proposed']} activated!")
                elif report.get("error"):
                    print(f"  [SI] Startup auto-improve skipped: {report['error']}")
        except Exception as e:
            print(f"  [SI] Startup improve error: {e}")

    # Самоулучшение при старте — только если явно включено (по умолчанию ВЫКЛ:
    # жжёт квоту провайдеров и конкурирует с первой задачей пользователя)
    if load_settings().get("si_on_startup"):
        asyncio.create_task(_startup_improve())
    else:
        print("  [SI] Запуск при старте отключён (settings.si_on_startup=false)")

    # 4. Watchdog авто-выключения
    if IDLE_SHUTDOWN_MINUTES > 0:
        _shutdown_task = asyncio.create_task(_idle_watchdog())
        print(f"  [AutoShutdown] Активен — выключусь через {IDLE_SHUTDOWN_MINUTES} мин без запросов"
              f" (FREEPALP_IDLE_SHUTDOWN=0 чтобы отключить)")


def _inject_digest_into_hot(hot_path: Path, digest: str) -> None:
    """
    Вставляет / обновляет блок дайджеста в hot_memory.md.
    Блок обрамлён маркерами чтобы можно было обновлять без дублей.
    """
    MARKER_START = "<!-- SESSION_DIGEST_START -->"
    MARKER_END   = "<!-- SESSION_DIGEST_END -->"

    hot_path.parent.mkdir(parents=True, exist_ok=True)
    existing = hot_path.read_text(encoding="utf-8") if hot_path.exists() else ""

    block = f"{MARKER_START}\n{digest}\n{MARKER_END}"

    if MARKER_START in existing:
        # Заменяем старый блок
        import re as _re
        updated = _re.sub(
            rf"{_re.escape(MARKER_START)}.*?{_re.escape(MARKER_END)}",
            block,
            existing,
            flags=_re.DOTALL,
        )
    else:
        # Добавляем в конец
        updated = existing.rstrip() + "\n\n" + block + "\n"

    hot_path.write_text(updated, encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# WebUI
# ══════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def root():
    index = _STATIC / "index.html"
    if index.exists():
        return HTMLResponse(index.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>FreePalp</h1><p>index.html not found</p>")


# ══════════════════════════════════════════════════════════════
# Chat
# ══════════════════════════════════════════════════════════════

MAX_MESSAGE_LEN = 32_000   # символов — защита от OOM и token-limit ошибок


class AttachmentItem(BaseModel):
    name: str                        # имя файла
    mime: str = "application/octet-stream"  # MIME тип
    data: str                        # base64-encoded содержимое


class ChatRequest(BaseModel):
    message: str
    context: Optional[dict] = None
    conversation_id: Optional[str] = None   # ID диалога для хранения истории
    attachments: Optional[list[AttachmentItem]] = None  # прикреплённые файлы/картинки
    session_keys: Optional[dict] = None     # qclaw-режим: ключи друга {provider: key}


def _apply_session_keys(req: "ChatRequest") -> None:
    """qclaw: если запрос принёс ключи друга — включаем session-режим.
    Инференс будет использовать ТОЛЬКО эти ключи, не ключи хоста.
    Память и самообучение остаются общими на хосте.
    """
    try:
        from freepalp.core import session_keys as _sk
        keys = req.session_keys
        if keys and isinstance(keys, dict) and any(keys.values()):
            # оставляем только непустые
            clean = {p: k for p, k in keys.items() if k}
            _sk.set_session_keys(clean)
        else:
            _sk.set_session_keys(None)   # режим хоста
    except Exception:
        pass


@app.post("/api/shutdown")
async def api_shutdown(source: str = "ui"):
    """
    Явный shutdown — вызывается из UI (source=ui) или sendBeacon при закрытии вкладки (source=beacon).
    Защита: игнорируем beacon в первые BEACON_GRACE_SEC секунд после старта
    (браузер может прислать стale beacon от предыдущей сессии).
    """
    uptime = time.time() - _startup_time
    # Beacon всегда игнорируем — только явная кнопка "Выключить" (source=ui) или restart
    # Beacon срабатывает при любом reload/navigate страницы и убивал сервер mid-training
    if source == "beacon":
        return {"ok": False, "ignored": True,
                "reason": "beacon shutdown disabled — use UI button"}

    print(f"\n  [Shutdown] Запрос от {source} (uptime={uptime:.0f}s)...")

    async def _delayed():
        await asyncio.sleep(0.3)
        await _do_shutdown()

    asyncio.create_task(_delayed())
    return {"ok": True, "message": "Сервер завершает работу..."}


@app.post("/api/chat")
async def chat(req: ChatRequest):
    _touch_activity()   # сбрасываем счётчик бездействия
    _apply_session_keys(req)   # qclaw: ключи друга если переданы

    # ── Защита от огромных сообщений (OOM / token-limit) ─────────────
    if len(req.message) > MAX_MESSAGE_LEN:
        return JSONResponse(
            status_code=400,
            content={
                "error": (
                    f"Сообщение слишком длинное: {len(req.message):,} символов "
                    f"(максимум {MAX_MESSAGE_LEN:,}). Сократи текст и попробуй снова."
                )
            },
        )

    try:
        orch    = _get_orch()
        conv_id = req.conversation_id or "default"

        # Достаём историю этого диалога
        history = _conversations.get(conv_id, [])

        # Собираем контекст с историей
        ctx = dict(req.context or {})
        if history:
            ctx["conversation_history"] = history

        # Прикреплённые файлы — декодируем текстовые, картинки передаём как base64
        if req.attachments:
            import base64 as _b64
            text_parts: list[str] = []
            image_parts: list[dict] = []
            for att in req.attachments:
                is_image = att.mime.startswith("image/")
                if is_image:
                    image_parts.append({
                        "name": att.name,
                        "mime": att.mime,
                        "data": att.data,   # base64
                    })
                else:
                    # Текстовый файл — декодируем и инжектируем как текст
                    try:
                        raw = _b64.b64decode(att.data).decode("utf-8", errors="replace")
                        # Обрезаем до 8k символов чтобы не перегрузить контекст
                        if len(raw) > 8000:
                            raw = raw[:8000] + "\n... (обрезано)"
                        text_parts.append(f"[FILE: {att.name}]\n{raw}\n[/FILE]")
                    except Exception as e:
                        text_parts.append(f"[FILE: {att.name}] (ошибка декодирования: {e})")
            if text_parts:
                ctx["attachments_text"] = "\n\n".join(text_parts)
            if image_parts:
                ctx["attachments_images"] = image_parts

        result = await asyncio.wait_for(
            orch.run(req.message, context=ctx),
            timeout=300.0,   # глобальный таймаут запроса (DAG-задачи дольше)
        )
        fb     = result.critic_feedback
        tin  = sum(m.metadata.get("tokens_in",  0)   for m in result.messages)
        tout = sum(m.metadata.get("tokens_out", 0)   for m in result.messages)
        cost = sum(m.metadata.get("cost_usd",   0.0) for m in result.messages)
        import re
        tool_calls = []
        for msg in result.messages:
            for m in re.finditer(r"TOOL RESULT \[(\w+)\]", msg.content):
                tool_calls.append({"tool": m.group(1), "ok": True})

        # Сохраняем обмен в историю (только если ответ не ошибка)
        if result.final_answer and not result.final_answer.startswith("[Ошибка"):
            history = history + [
                {"role": "user",      "content": req.message},
                {"role": "assistant", "content": result.final_answer},
            ]
            # Оставляем только последние _MAX_HISTORY обменов
            _conversations[conv_id] = history[-(  _MAX_HISTORY * 2):]

            # Snapshot на диск — crash recovery (не ждём)
            try:
                from freepalp.memory.session_memory import save_snapshot
                save_snapshot(conv_id, _conversations[conv_id])
            except Exception:
                pass

        return {
            "answer":          result.final_answer,
            "model":           result.model_used,
            "iterations":      result.iterations,
            "elapsed":         result.elapsed_seconds,
            "score":           fb.score if fb else None,
            "issues":          fb.issues if fb else [],
            "tokens_in":       tin,
            "tokens_out":      tout,
            "cost_usd":        round(cost, 6),
            "tool_calls":      tool_calls,
            "task_id":         result.task_id,
            "conversation_id": conv_id,
            "history_len":     len(_conversations.get(conv_id, [])) // 2,
        }
    except asyncio.TimeoutError:
        return JSONResponse(
            status_code=504,
            content={"error": "Запрос выполнялся слишком долго (>120 с) и был отменён. Попробуй упростить задачу."},
        )
    except BaseException as e:
        # Ловим всё включая MemoryError, чтобы сервер не падал
        logger.error("Chat handler crash: %s: %s", type(e).__name__, e)
        return JSONResponse(status_code=500, content={"error": f"{type(e).__name__}: {e}"})


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    SSE endpoint: стримит прогресс-события во время обработки задачи.
    Формат: text/event-stream, каждое событие = data: {json}\\n\\n
    Последнее событие: {"type": "done", "answer": "...", ...}
    """
    _touch_activity()
    _apply_session_keys(req)   # qclaw: ключи друга если переданы
    if len(req.message) > MAX_MESSAGE_LEN:
        async def _err():
            yield f'data: {json.dumps({"type":"error","text":"Сообщение слишком длинное"})}\n\n'
        return StreamingResponse(_err(), media_type="text/event-stream")

    queue: asyncio.Queue = asyncio.Queue()
    _bg_tasks: set = set()   # сильная ссылка — защита от GC (Python 3.10+)

    async def on_event(event: dict):
        await queue.put(event)

    async def _run_orch():
        try:
            orch    = _get_orch()
            conv_id = req.conversation_id or "default"
            history = _conversations.get(conv_id, [])
            ctx = dict(req.context or {})
            if history:
                ctx["conversation_history"] = history
            if req.attachments:
                import base64 as _b64
                text_parts, image_parts = [], []
                for att in req.attachments:
                    if att.mime.startswith("image/"):
                        image_parts.append({"name": att.name, "mime": att.mime, "data": att.data})
                    else:
                        try:
                            raw = _b64.b64decode(att.data).decode("utf-8", errors="replace")[:8000]
                            text_parts.append(f"[FILE: {att.name}]\n{raw}\n[/FILE]")
                        except Exception as e:
                            text_parts.append(f"[FILE: {att.name}] (ошибка: {e})")
                if text_parts:  ctx["attachments_text"]   = "\n\n".join(text_parts)
                if image_parts: ctx["attachments_images"] = image_parts

            result = await asyncio.wait_for(
                orch.run(req.message, context=ctx, on_event=on_event),
                timeout=300.0,   # сложные DAG-задачи (многофайловые) дольше
            )
            fb   = result.critic_feedback
            tin  = sum(m.metadata.get("tokens_in",  0)   for m in result.messages)
            tout = sum(m.metadata.get("tokens_out", 0)   for m in result.messages)
            cost = sum(m.metadata.get("cost_usd",   0.0) for m in result.messages)
            import re as _re
            tool_calls = [{"tool": m, "ok": True}
                          for msg in result.messages
                          for m in _re.findall(r"TOOL RESULT \[(\w+)\]", msg.content)]
            # Превьюабельные артефакты, созданные в этой задаче (HTML/SVG)
            artifacts = []
            for msg in result.messages:
                for t in (msg.metadata.get("tools_called") or []):
                    pth = (t.get("path") or "").replace("\\", "/")
                    if pth and pth.lower().endswith((".html", ".htm", ".svg")):
                        artifacts.append(pth)
            artifacts = list(dict.fromkeys(artifacts))

            if result.final_answer and not result.final_answer.startswith("[Ошибка"):
                new_hist = _conversations.get(conv_id, []) + [
                    {"role": "user",      "content": req.message},
                    {"role": "assistant", "content": result.final_answer},
                ]
                _conversations[conv_id] = new_hist[-(_MAX_HISTORY * 2):]
                try:
                    from freepalp.memory.session_memory import save_snapshot
                    save_snapshot(conv_id, _conversations[conv_id])
                except Exception:
                    pass

            await queue.put({
                "type":            "done",
                "answer":          result.final_answer,
                "model":           result.model_used,
                "iterations":      result.iterations,
                "elapsed":         result.elapsed_seconds,
                "score":           fb.score if fb else None,
                "issues":          fb.issues if fb else [],
                "tokens_in":       tin,
                "tokens_out":      tout,
                "cost_usd":        round(cost, 6),
                "tool_calls":      tool_calls,
                "task_id":         result.task_id,
                "conversation_id": conv_id,
                "history_len":     len(_conversations.get(conv_id, [])) // 2,
                "task_type":       result.messages[0].metadata.get("task_type", "") if result.messages else "",
                "critic_score":    fb.score if fb else 0.0,
                "artifacts":       artifacts,
            })
        except asyncio.CancelledError:
            # Пользователь нажал Стоп — фиксируем в истории, чтобы контекст не потерялся
            cid = req.conversation_id or "default"
            _conversations.setdefault(cid, []).append(
                {"role": "assistant", "content": f"[Задача прервана пользователем: {req.message[:120]}]"}
            )
            raise
        except asyncio.TimeoutError:
            await queue.put({"type": "error", "text": "Таймаут (>120 с)"})
        except Exception as e:
            await queue.put({"type": "error", "text": str(e)})
        finally:
            await queue.put(None)   # sentinel

    async def event_generator():
        # Сразу отправляем ping чтобы убедиться что соединение живо
        yield f"data: {json.dumps({'type': 'ping'}, ensure_ascii=False)}\n\n"
        task = asyncio.create_task(_run_orch())
        _bg_tasks.add(task)                       # сильная ссылка — защита от GC
        task.add_done_callback(_bg_tasks.discard)  # освобождаем после завершения
        try:
            _silent = 0.0
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=20.0)
                    _silent = 0.0
                except asyncio.TimeoutError:
                    # Тишина ≠ смерть задачи: шлём heartbeat, держим соединение.
                    # Раньше тут был ложный 'Queue timeout' при живой задаче.
                    _silent += 20.0
                    if _silent >= 600.0:   # 10 мин полной тишины — сдаёмся честно
                        yield f"data: {json.dumps({'type': 'error', 'text': 'Задача не отвечает >10 мин — прервана'}, ensure_ascii=False)}\n\n"
                        break
                    yield f"data: {json.dumps({'type': 'heartbeat', 'silent_sec': int(_silent)}, ensure_ascii=False)}\n\n"
                    continue
                if event is None:
                    break
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except asyncio.CancelledError:
            task.cancel()
        except Exception as gen_e:
            yield f"data: {json.dumps({'type': 'error', 'text': f'Generator error: {gen_e}'}, ensure_ascii=False)}\n\n"
        finally:
            if not task.done():
                task.cancel()

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/sse-test")
async def sse_test():
    """Minimal SSE test with Queue+Task pattern (strong task reference)."""
    q: asyncio.Queue = asyncio.Queue()
    _tasks = set()  # strong ref to prevent GC
    async def producer():
        await asyncio.sleep(0.1)
        await q.put({"msg": "hello"})
        await asyncio.sleep(0.3)
        await q.put({"msg": "world"})
        await q.put(None)
    async def gen():
        yield f"data: {{\"step\":1}}\n\n"
        await asyncio.sleep(0.1)
        yield f"data: {{\"step\":2}}\n\n"
        # старт producer
        loop = asyncio.get_event_loop()
        t = loop.create_task(producer())
        _tasks.add(t)
        t.add_done_callback(_tasks.discard)
        yield f"data: {{\"step\":3}}\n\n"
        try:
            ev = await asyncio.wait_for(q.get(), timeout=5.0)
            yield f"data: {json.dumps(ev)}\n\n"
        except asyncio.TimeoutError:
            yield f"data: {{\"err\":\"timeout - task never ran\"}}\n\n"
        except Exception as exc:
            yield f"data: {{\"err\":\"{type(exc).__name__}: {str(exc)[:80]}\"}}\n\n"
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.delete("/api/chat/history")
async def clear_history(conversation_id: str = "default"):
    """Сбросить историю диалога (в памяти + на диске)."""
    _conversations.pop(conversation_id, None)
    try:
        from freepalp.memory.session_memory import clear_snapshot
        clear_snapshot(conversation_id)
    except Exception:
        pass
    return {"ok": True, "conversation_id": conversation_id}


@app.get("/api/chat/sessions")
async def list_sessions():
    """Список активных (сохранённых) сессий с превью первого сообщения."""
    from freepalp.memory.session_memory import ACTIVE_DIR
    sessions = []
    if ACTIVE_DIR.exists():
        import json as _json
        for f in sorted(ACTIVE_DIR.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                data = _json.loads(f.read_text(encoding="utf-8"))
                history = data.get("history", [])
                # Первое сообщение пользователя как заголовок
                first_user = next((m["content"] for m in history if m.get("role") == "user"), "")
                sessions.append({
                    "conv_id":    data.get("conv_id", f.stem),
                    "saved_at":   data.get("saved_at", ""),
                    "msg_count":  data.get("msg_count", len(history)),
                    "title":      first_user[:60] or "Диалог",
                    "in_memory":  data.get("conv_id", f.stem) in _conversations,
                })
            except Exception:
                pass
    return {"sessions": sessions, "total": len(sessions)}


@app.get("/api/chat/sessions/{conv_id}")
async def get_session(conv_id: str):
    """Загрузить историю конкретной сессии (из памяти или снэпшота на диске)."""
    import json as _json
    # Сначала смотрим в памяти
    if conv_id in _conversations:
        return {"conv_id": conv_id, "history": _conversations[conv_id], "source": "memory"}
    # Потом на диске
    from freepalp.memory.session_memory import ACTIVE_DIR
    snap = ACTIVE_DIR / f"{conv_id}.json"
    if snap.exists():
        try:
            data = _json.loads(snap.read_text(encoding="utf-8"))
            return {"conv_id": conv_id, "history": data.get("history", []), "source": "disk"}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})
    return JSONResponse(status_code=404, content={"error": "Session not found"})


@app.get("/api/chat/digest")
async def get_session_digest():
    """Дайджест последних сессий (что было обсуждено)."""
    try:
        from freepalp.memory.session_memory import get_or_build_digest
        digest = get_or_build_digest()
        return {"digest": digest, "lines": len(digest.splitlines()) if digest else 0}
    except Exception as e:
        return {"digest": "", "error": str(e)}


# ══════════════════════════════════════════════════════════════
# Status / Models / Providers / Tools
# ══════════════════════════════════════════════════════════════

@app.get("/api/status")
async def api_status():
    try:
        orch = _get_orch()
        ms   = orch.get_available_models()
        from freepalp.agents.tool_agent import ALL_TOOLS
        from freepalp.core import prompt_loader
        idle_sec = int(time.time() - _last_activity)
        return {
            "version":               prompt_loader.get_version(),
            "current_model":         ms[0]["name"] if ms else "—",
            "total_models":          len(ms),
            "active_providers":      len(set(m["provider"] for m in ms)),
            "total_tools":           len(ALL_TOOLS),
            "discovery_live":        getattr(orch.router, "_discovery_used", False),
            "idle_shutdown_minutes": IDLE_SHUTDOWN_MINUTES,
            "idle_seconds":          idle_sec,
        }
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/models")
async def api_models():
    try:
        return {"models": _get_orch().get_available_models()}
    except Exception as e:
        return {"models": [], "error": str(e)}


@app.get("/api/providers")
async def api_providers():
    try:
        from freepalp.core.model_discovery import get_providers_status
        ps = get_providers_status()
        return {"active": [p for p in ps if p["configured"]], "inactive": [p for p in ps if not p["configured"]]}
    except Exception as e:
        return {"active": [], "inactive": [], "error": str(e)}


@app.get("/api/tools")
async def api_tools():
    try:
        from freepalp.agents.tool_agent import ALL_TOOLS
        return {
            "tools": [{"name": n, "description": i.get("description",""), "async": i.get("async", False)} for n, i in ALL_TOOLS.items()],
            "total": len(ALL_TOOLS),
        }
    except Exception as e:
        return {"tools": [], "error": str(e)}


# ══════════════════════════════════════════════════════════════
# Memory
# ══════════════════════════════════════════════════════════════

@app.get("/api/memory")
async def api_memory():
    try:
        hot = _get_orch().memory.load_hot()
        return {"hot": hot, "lines": len(hot.splitlines())}
    except Exception as e:
        return {"hot": "", "error": str(e)}


@app.get("/api/memory/stats")
async def api_memory_stats():
    try:
        return _get_orch().memory.get_stats()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/hooks")
async def api_hooks():
    """Список зарегистрированных хуков и статистика срабатываний."""
    try:
        hm = getattr(_get_orch(), "hooks", None)
        if hm is None:
            return {"ok": False, "error": "hooks недоступны"}
        return {"ok": True, "hooks": hm.list_hooks(), "stats": hm.stats()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/memory/search")
async def api_memory_search(q: str = ""):
    if not q:
        return {"results": [], "total": 0}
    try:
        mem = _get_orch().memory
        # Гибридный поиск: векторный (по смыслу) + keyword архив
        if hasattr(mem, "hybrid_search"):
            results = mem.hybrid_search(q)
        else:
            results = mem.search_cold(q)
        return {"results": results, "total": len(results), "query": q}
    except Exception as e:
        return {"results": [], "error": str(e)}


@app.get("/api/memory/graph")
async def api_memory_graph(max_nodes: int = 120, threshold: float = 0.35,
                           upto: int = 0):
    """Честный граф памяти: узлы — реальные записи vector_store, рёбра —
    косинусная близость их НАСТОЯЩИХ векторов (тех же, которыми ищет память).
    Ничего не рисуется «для красоты» — только фактические данные индекса.

    upto > 0 — «машина времени»: граф по первым upto записям (индекс
    append-only хронологический), для прокрутки развития памяти."""
    import math
    try:
        idx_path = Path(__file__).parent / "memory" / "vector_index.json"
        if not idx_path.exists():
            return {"ok": False, "error": "vector_index.json не найден"}
        data = json.loads(idx_path.read_text(encoding="utf-8"))
        all_entries = data.get("entries") or []
        if upto > 0:
            entries = all_entries[:upto][-max_nodes:]   # срез истории
        else:
            entries = all_entries[-max_nodes:]          # свежие важнее

        nodes = []
        vecs = []
        for e in entries:
            v = e.get("vec") or []
            norm = math.sqrt(sum(x * x for x in v)) or 1.0
            vecs.append([x / norm for x in v])
            nodes.append({
                "id":    e.get("id"),
                "text":  (e.get("text") or "")[:120],
                "layer": e.get("layer") or "episodic",
            })

        edges = []
        for i in range(len(vecs)):
            for j in range(i + 1, len(vecs)):
                sim = sum(a * b for a, b in zip(vecs[i], vecs[j]))
                if sim >= threshold:
                    edges.append({"a": i, "b": j, "w": round(sim, 3)})
        # Не топим браузер: максимум 400 самых сильных рёбер
        edges.sort(key=lambda e: -e["w"])
        return {"ok": True, "nodes": nodes, "edges": edges[:400],
                "total_entries": len(data.get("entries") or []),
                "threshold": threshold}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _msg_text(content) -> str:
    """OpenAI content → плоский текст (бывает строкой или массивом частей)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return str(content or "")


@app.post("/v1/chat/completions")
async def openai_compat(request: Request):
    """OpenAI-совместимый эндпоинт — FreePalp как бэкенд для ЛЮБОГО IDE-плагина
    (Continue.dev, Cursor, и т.п.), умеющего в OpenAI API. Указываешь base_url
    http://localhost:28800/v1 — и весь оркестратор (роутинг, инструменты, DAG,
    память) работает под капотом редактора."""
    _touch_activity()
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "невалидный JSON"})

    messages = body.get("messages", []) or []
    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        return JSONResponse(status_code=400, content={"error": "нет user-сообщения"})
    task = _msg_text(user_msgs[-1].get("content"))

    # Предыдущие реплики → история диалога (без system и без последнего user)
    history = []
    for m in messages[:-1]:
        if m.get("role") in ("user", "assistant"):
            history.append({"role": m["role"], "content": _msg_text(m.get("content"))})
    ctx = {"conversation_history": history} if history else {}

    try:
        orch = _get_orch()
        result = await asyncio.wait_for(orch.run(task, context=ctx), timeout=300.0)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

    answer = result.final_answer or ""
    tin  = sum(m.metadata.get("tokens_in",  0)   for m in result.messages)
    tout = sum(m.metadata.get("tokens_out", 0)   for m in result.messages)
    return {
        "id": f"chatcmpl-{result.task_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": f"freepalp/{result.model_used}",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": answer},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": tin, "completion_tokens": tout,
                  "total_tokens": tin + tout},
    }


@app.get("/v1/models")
async def openai_models():
    """OpenAI-совместимый список моделей (некоторые IDE-плагины его требуют)."""
    return {"object": "list", "data": [
        {"id": "freepalp", "object": "model", "created": int(time.time()),
         "owned_by": "freepalp"}]}


@app.get("/api/mcp")
async def api_mcp():
    """Статус подключённых MCP-серверов и их инструментов."""
    try:
        from freepalp.core import mcp_client as _mcp
        return {"ok": True, **_mcp.get().status()}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/mcp/reconnect")
async def api_mcp_reconnect():
    """Переподключить MCP-серверы (после правки конфига)."""
    try:
        import asyncio as _asyncio
        from freepalp.core import mcp_client as _mcp
        mgr = _mcp.get()
        mgr.close_all()
        summary = await _asyncio.to_thread(mgr.connect_all)
        return {"ok": True, "summary": summary}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/mcp/add")
async def api_mcp_add(request: Request):
    """Добавить MCP-сервер из UI (без ручной правки конфига) и переподключить."""
    try:
        body = await request.json()
        name = (body.get("name") or "").strip()
        command = (body.get("command") or "").strip()
        raw_args = body.get("args") or []
        if isinstance(raw_args, str):
            # shlex с posix=False: уважает кавычки и НЕ ломает Windows-пути
            # с пробелами (пользователь берёт такой путь в "кавычки").
            import shlex
            try:
                parts = shlex.split(raw_args, posix=False)
            except ValueError:
                parts = raw_args.split()
            # posix=False группирует по кавычкам, но оставляет сами кавычки —
            # снимаем обрамляющие, чтобы subprocess получил чистый путь.
            raw_args = [(p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p)
                        for p in parts if p]
        env = body.get("env") or {}
        if not name or not command:
            return JSONResponse(status_code=400, content={"error": "нужны name и command"})
        cfg_path = Path(__file__).parent / "config" / "mcp_servers.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        cfg.setdefault("mcpServers", {})[name] = {"command": command, "args": raw_args, "env": env}
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        import asyncio as _asyncio
        from freepalp.core import mcp_client as _mcp
        mgr = _mcp.get(); mgr.close_all()
        summary = await _asyncio.to_thread(mgr.connect_all)
        ok = name in summary.get("connected", [])
        return {"ok": ok, "summary": summary,
                "error": None if ok else f"Сервер '{name}' не поднялся — проверь command/args"}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.post("/api/skills/add")
async def api_skills_add(request: Request):
    """Добавить пользовательский навык (SKILL.md) из UI."""
    try:
        import re as _re
        body = await request.json()
        name = (body.get("name") or "").strip()
        desc = (body.get("description") or "").strip()
        proc = (body.get("procedure") or "").strip()
        if not name or not proc:
            return JSONResponse(status_code=400, content={"error": "нужны name и procedure"})
        # Keep Cyrillic/Unicode буквы в слаге, не только латиницу
        slug = _re.sub(r"[^\w]+", "_", name.lower(), flags=_re.UNICODE).strip("_")[:60] or "skill"
        from freepalp.core import skill_library as _sl
        d = _sl.get().dir
        path = d / f"user_{slug}.md"
        from datetime import date as _date
        content = (
            f"---\nname: {slug}\ndescription: {desc or name}\ntask_type: user\n"
            f"keywords: {' '.join(name.lower().split())}\ntools: \nsource_model: ручной\n"
            f"created: {_date.today().isoformat()}\nuses: 1\n---\n\n"
            f"# Навык: {name}\n\n## Когда применять\n{desc or name}\n\n"
            f"## Процедура\n{proc}\n\n_Добавлено пользователем вручную._\n")
        path.write_text(content, encoding="utf-8")
        return {"ok": True, "name": slug}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/history/search")
async def api_history_search(q: str = "", limit: int = 15):
    """FTS5-поиск по истории прошлых диалогов."""
    try:
        from freepalp.core import history_search as _hs
        if q.strip():
            await asyncio.to_thread(_hs.reindex)   # подхватить новые сессии
        return {"ok": True, "results": _hs.search(q, limit=limit), "query": q}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/history/reindex")
async def api_history_reindex(force: bool = False):
    try:
        from freepalp.core import history_search as _hs
        res = await asyncio.to_thread(_hs.reindex, force)
        return {"ok": True, **res}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/sandbox/artifacts")
async def api_sandbox_artifacts():
    """Список превьюабельных артефактов (HTML/SVG) в песочнице — для галереи."""
    try:
        from freepalp.tools.file_tools import SANDBOX_ROOT
        items = []
        for p in SANDBOX_ROOT.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".html", ".htm", ".svg"):
                rel = p.relative_to(SANDBOX_ROOT)
                items.append({
                    "name":  p.name,
                    "path":  str(rel).replace("\\", "/"),
                    "size":  p.stat().st_size,
                    "mtime": p.stat().st_mtime,
                })
        items.sort(key=lambda x: -x["mtime"])
        return {"ok": True, "artifacts": items[:60]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/sandbox/raw")
async def api_sandbox_raw(path: str):
    """Отдаёт файл песочницы для превью в iframe. Путь валидируется через
    _safe_path (никаких выходов за SANDBOX_ROOT). Рендерится в iframe с
    sandbox=allow-scripts — скрипты игр работают, но изолированы от родителя."""
    try:
        from freepalp.tools.file_tools import _safe_path
        from fastapi.responses import FileResponse
        p = _safe_path(path)
        if not p.exists() or not p.is_file():
            return JSONResponse(status_code=404, content={"error": "Файл не найден"})
        if p.suffix.lower() not in (".html", ".htm", ".svg", ".css", ".js", ".png",
                                    ".jpg", ".jpeg", ".gif", ".json", ".txt"):
            return JSONResponse(status_code=415, content={"error": "Тип не поддержан для превью"})
        return FileResponse(str(p))
    except PermissionError:
        return JSONResponse(status_code=403, content={"error": "Путь вне песочницы"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/skills")
async def api_skills():
    """Накопленные приёмы teacher→skill (дистилляция: успех после провала →
    SKILL.md, инжектится в промпт при похожей задаче). Витрина дифференциатора."""
    try:
        from freepalp.core import skill_library as _sl
        out = []
        for sk in _sl.get().all_skills():
            out.append({
                "name":         sk.get("name", ""),
                "task_type":    sk.get("task_type", ""),
                "tools":        sk.get("tools", ""),
                "source_model": sk.get("source_model", ""),
                "uses":         int(sk.get("uses", "1") or "1"),
                "created":      sk.get("created", ""),
                "description":  sk.get("description", ""),
            })
        out.sort(key=lambda s: -s["uses"])
        return {"ok": True, "skills": out, "total": len(out)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/memory/archives")
async def api_memory_archives():
    """Реальные архивы памяти: файлы archive/ + сессии по дням (sessions/*.jsonl)."""
    try:
        mem_dir = Path(__file__).parent / "memory"
        archives = []
        arch_dir = mem_dir / "archive"
        if arch_dir.exists():
            for f in sorted(arch_dir.glob("*.md")):
                txt = f.read_text(encoding="utf-8", errors="replace")
                archives.append({
                    "name":  f.name,
                    "lines": sum(1 for l in txt.splitlines() if l.strip()),
                    "size":  f.stat().st_size,
                })
        # Сессии по дням — для таймлайна
        days: dict = {}
        sess_dir = mem_dir / "sessions"
        if sess_dir.exists():
            for f in sess_dir.glob("*.jsonl"):
                day = f.name[:8]   # YYYYMMDD
                if len(day) == 8 and day.isdigit():
                    days[day] = days.get(day, 0) + 1
        timeline = [{"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "sessions": n}
                    for d, n in sorted(days.items())]
        return {"ok": True, "archives": archives, "timeline": timeline}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/memory/clean")
async def api_memory_clean():
    try:
        return _get_orch().memory.maintenance()
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/memory/levels")
async def api_memory_levels():
    """Уровни памяти по модели человека (★ score распределение)."""
    try:
        from freepalp.memory.consolidation import ConsolidationEngine
        engine = ConsolidationEngine()
        stats  = engine.get_memory_stats()

        # Читаем топ-5 самых сильных записей
        from freepalp.memory.memory_manager import HOT_FILE
        from freepalp.memory.consolidation import parse_hot_line, _get_score
        top = []
        if HOT_FILE.exists():
            lines = HOT_FILE.read_text(encoding="utf-8").splitlines()
            entries = [parse_hot_line(l) for l in lines if l.startswith("- ")]
            entries.sort(key=lambda e: -(e["score"] or 0))
            for e in entries[:5]:
                s = e["score"] or 0
                top.append({
                    "text":  e["text"][:80],
                    "score": s,
                    "level": (
                        "постоянная" if s >= 8 else
                        "долгосрочная" if s >= 5 else
                        "кратковременная" if s >= 3 else "угасает"
                    ),
                })

        return {**stats, "top_entries": top}
    except Exception as e:
        return {"error": str(e)}


# ══════════════════════════════════════════════════════════════
# Cron / Metrics / Self-improve
# ══════════════════════════════════════════════════════════════

@app.get("/api/crons")
async def api_crons():
    try:
        from freepalp.core.cron_manager import CronManager
        from datetime import datetime
        cm, now = CronManager(), datetime.now()
        tasks = []
        for c in cm.list_crons():
            try:
                dt    = datetime.fromisoformat(c.get("next_run", ""))
                secs  = int((dt - now).total_seconds())
                if   secs < 0:      nxt = "OVERDUE"
                elif secs < 3600:   nxt = f"через {secs//60}м"
                elif secs < 86400:  nxt = f"через {secs//3600}ч"
                else:               nxt = f"через {secs//86400}д"
            except Exception:
                nxt = "—"
            tasks.append({**c, "next_run": nxt})
        return {"tasks": tasks, "total": len(tasks)}
    except Exception as e:
        return {"tasks": [], "error": str(e)}


@app.get("/api/metrics")
async def api_metrics():
    try:
        from freepalp.core.self_improvement.metrics import Evaluator
        ev = Evaluator()
        summary = ev.get_stats_summary()
        # Добавляем подробную разбивку по типам (исключаем API-ошибки).
        # Окно 300: при 100 редкие типы (file_ops, search...) выпадали из графика
        records = ev.load_recent(300)
        from collections import defaultdict
        by_type_detail: dict = defaultdict(lambda: {"count": 0, "errors": 0, "scores": [], "elapsed": [], "iterations": []})
        for r in records:
            tt = r.get("task_type", "general")
            is_api_error = r.get("critic_score", 0.0) == 0.0 and r.get("tokens_total", 0) == 0
            by_type_detail[tt]["count"] += 1
            if is_api_error:
                by_type_detail[tt]["errors"] += 1
            else:
                by_type_detail[tt]["scores"].append(r.get("critic_score", 0.0))
                by_type_detail[tt]["elapsed"].append(r.get("elapsed", 0))
                by_type_detail[tt]["iterations"].append(r.get("iterations", 1))
        summary["by_type_detail"] = {
            tt: {
                "count":       d["count"],
                "errors":      d["errors"],
                "avg_score":   round(sum(d["scores"]) / len(d["scores"]), 3) if d["scores"] else 0,
                "avg_elapsed": round(sum(d["elapsed"]) / len(d["elapsed"]), 1) if d["elapsed"] else 0,
                "avg_iter":    round(sum(d["iterations"]) / len(d["iterations"]), 2) if d["iterations"] else 1,
            }
            for tt, d in sorted(by_type_detail.items())
        }
        return summary
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/metrics/history")
async def api_metrics_history(n: int = 20):
    """Последние N записей метрик для построения графика тренда."""
    try:
        from freepalp.core.self_improvement.metrics import Evaluator
        records = Evaluator().load_recent(max(1, min(n, 100)))
        return {
            "records": [
                {
                    "ts":           r.get("ts", ""),
                    "task_type":    r.get("task_type", "general"),
                    "critic_score": r.get("critic_score", 0.0),
                    "model":        r.get("model", ""),
                    "elapsed":      r.get("elapsed", 0),
                    "iterations":   r.get("iterations", 1),
                    "preview":      r.get("preview", "")[:60],
                }
                for r in records
            ]
        }
    except Exception as e:
        return {"records": [], "error": str(e)}


@app.post("/api/improve")
async def api_improve():
    try:
        report = await _get_orch().self_improvement.run(force=True)
        if report.get("version_activated"):
            # Новая версия активирована — перезапускаем через 3с чтобы применить промпты
            report["restarting"] = True
            asyncio.create_task(_schedule_restart(delay=3.0, reason="self-improvement v" + str(report.get("version_proposed", "?"))))
        return report
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/settings")
async def api_settings_get():
    """Текущие настройки системы + дефолты (для UI)."""
    return {"settings": load_settings(), "defaults": _SETTINGS_DEFAULTS}


@app.post("/api/settings")
async def api_settings_set(req: dict):
    """Сохранить настройки. Принимает частичный dict — мержится с текущими.
    idle_shutdown и retry_threshold применяются после перезапуска."""
    try:
        s = load_settings()
        for k, v in (req or {}).items():
            if k in _SETTINGS_DEFAULTS:
                s[k] = v
        save_settings(s)
        return {"ok": True, "settings": s,
                "note": "idle_shutdown/retry_threshold применятся после перезапуска"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# Вехи FreePalp — реконструкция по датам файлов, транскриптам и CHANGELOG.md
_FREEPALP_MILESTONES = [
    {"date": "2026-05-22", "label": "v0.1.0", "kind": "веха",
     "text": "Рождение: прото-пакет octo — оркестратор, Worker/Critic, первые инструменты"},
    {"date": "2026-05-23", "label": "v0.2.0", "kind": "веха",
     "text": "FreePalp: переименование, Gateway+WebUI, память HOT/WARM/COLD, мульти-провайдеры"},
    {"date": "2026-05-24", "label": "v0.3.0", "kind": "веха",
     "text": "Самоулучшение: цикл SI, версионирование промптов, гейт test_mvp"},
    {"date": "2026-06-01", "label": "v0.4.0", "kind": "веха",
     "text": "Улучшение промптов shell/search/general/review, недельный дайджест"},
    {"date": "2026-06-11", "label": "v0.5.0", "kind": "веха",
     "text": "Надёжность: UTF-8, песочница, whitelist, детектор галлюцинаций файлов"},
    {"date": "2026-06-12", "label": "v1.0.0", "kind": "веха",
     "text": "Git-версионирование, кнопка Стоп, двухъярусный критик, BASELINE.md"},
    {"date": "2026-06-12", "label": "v1.1.0", "kind": "веха",
     "text": "Настройки, heartbeat, loop breaker, teacher→skill, каталог models.dev"},
]


def _read_freepalp_version() -> str:
    vf = Path(__file__).parent.parent / "VERSION"
    try:
        return vf.read_text("utf-8").strip()
    except Exception:
        return "1.1.0"


@app.get("/api/system/versions")
async def api_system_versions():
    """Единая версия FreePalp + общая хронология: вехи, изменения кода (git)
    и версии промптов (самоулучшение) в одной ленте."""
    import subprocess
    repo_root = str(Path(__file__).parent.parent)
    history: list[dict] = [dict(m) for m in _FREEPALP_MILESTONES]

    # Изменения кода — git
    try:
        from .core.winproc import no_window
        r = subprocess.run(
            ["git", "log", "--max-count=20", "--pretty=format:%h|%ad|%s",
             "--date=format:%Y-%m-%d %H:%M"],
            capture_output=True, cwd=repo_root, timeout=10, **no_window(),
        )
        if r.returncode == 0:
            for line in r.stdout.decode("utf-8", errors="replace").splitlines():
                parts = line.split("|", 2)
                if len(parts) == 3:
                    history.append({"date": parts[1], "label": parts[0],
                                    "kind": "код", "text": parts[2]})
    except Exception:
        pass

    # Версии промптов — самоулучшение
    try:
        from freepalp.core.self_improvement.version_manager import VersionManager
        for v in VersionManager().list_versions():
            history.append({
                "date": (v.get("proposed_at") or "")[:16].replace("T", " "),
                "label": f"промпты v{v.get('version')}",
                "kind": "промпты",
                "text": v.get("changes", "") or "",
            })
    except Exception:
        pass

    history.sort(key=lambda h: h.get("date") or "", reverse=True)
    return {"ok": True, "version": _read_freepalp_version(), "history": history[:50]}


@app.get("/api/improve/status")
async def api_improve_status():
    """Статус само-улучшения: текущая версия, кандидаты, история версий."""
    try:
        si = _get_orch().self_improvement
        status = si.status()
        # добавляем историю версий
        from freepalp.core.self_improvement.version_manager import VersionManager
        vm = VersionManager()
        versions = vm.list_versions()
        status["versions"] = [
            {
                "version":      v.get("version"),
                "status":       v.get("status"),
                "changes":      v.get("changes", ""),
                "proposed_at":  v.get("proposed_at", ""),
                "activated_at": v.get("activated_at"),
                "test_passed":  v.get("test_passed"),
            }
            for v in versions[-10:]  # последние 10
        ]
        return status
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/improve/rollback")
async def api_improve_rollback():
    """Откат к предыдущей версии промптов."""
    try:
        from freepalp.core.self_improvement.version_manager import VersionManager
        ok, msg = VersionManager().rollback()
        return {"ok": ok, "message": msg}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/provider/disable")
async def api_provider_disable(provider: str):
    """Временно отключить все модели провайдера (до перезапуска)."""
    try:
        count = _get_orch().router.mark_provider_unavailable(provider)
        return {"ok": True, "provider": provider, "disabled_models": count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/provider/enable")
async def api_provider_enable(provider: str):
    """Восстановить все модели провайдера."""
    try:
        count = _get_orch().router.restore_provider(provider)
        return {"ok": True, "provider": provider, "restored_models": count}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/token-budget")
async def api_token_budget():
    """Состояние квот и ротации ключей по провайдерам."""
    try:
        from freepalp.core import token_budget as _tb
        summary = _tb.get().get_summary()
        # Добавить информацию о текущем статусе роутера
        try:
            router = _get_orch().router
            available_models = [m for m in router.models if m.available]
            provider_model_counts = {}
            for m in available_models:
                provider_model_counts[m.provider] = provider_model_counts.get(m.provider, 0) + 1
            for entry in summary:
                entry["models_active"] = provider_model_counts.get(entry["provider"], 0)
        except Exception:
            pass
        return {"ok": True, "providers": summary}
    except Exception as e:
        return {"ok": False, "error": str(e), "providers": []}


@app.post("/api/token-budget/reset")
async def api_token_budget_reset(provider: str = ""):
    """Сбросить кулдауны провайдера (или всех если provider пустой)."""
    try:
        from freepalp.core import token_budget as _tb
        _tb.get().reset_cooldowns(provider or None)
        if provider:
            _get_orch().router.restore_provider(provider)
        return {"ok": True, "reset": provider or "all"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════
# Key Management (.env read/write)
# ══════════════════════════════════════════════════════════════

_ENV_FILE = Path(__file__).parent.parent / ".env"

# Маппинг provider → env var name
_PROVIDER_ENV_KEYS: dict[str, str] = {
    "groq":       "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "cerebras":   "CEREBRAS_API_KEY",
    "gemini":     "GEMINI_API_KEY",
    "sambanova":  "SAMBANOVA_API_KEY",
    "together":   "TOGETHER_API_KEY",
    "novita":     "NOVITA_API_KEY",
    "mistral":    "MISTRAL_API_KEY",
    "zai":        "ZAI_API_KEY",
    "nvidia":     "NVIDIA_API_KEY",
    "cohere":     "COHERE_API_KEY",
    "cloudflare": "CLOUDFLARE_API_TOKEN",
    "anthropic":  "ANTHROPIC_API_KEY",
    "openai":     "OPENAI_API_KEY",
}


def _read_env_lines() -> list[str]:
    if _ENV_FILE.exists():
        return _ENV_FILE.read_text(encoding="utf-8").splitlines()
    return []


def _write_env_lines(lines: list[str]) -> None:
    _ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mask_key(key: str) -> str:
    if not key or len(key) < 8:
        return "***"
    return key[:6] + "..." + key[-4:]


@app.get("/api/keys")
async def api_keys_list():
    """Читает все ключи провайдеров из .env (замаскированные)."""
    result = []
    for provider, base_env in _PROVIDER_ENV_KEYS.items():
        keys = []
        # Primary key
        val = os.environ.get(base_env, "")
        if val:
            keys.append({"slot": 1, "preview": _mask_key(val), "env_var": base_env})
        # Additional keys _2, _3, ...
        for i in range(2, 6):
            extra = os.environ.get(f"{base_env}_{i}", "")
            if extra:
                keys.append({"slot": i, "preview": _mask_key(extra), "env_var": f"{base_env}_{i}"})
        result.append({"provider": provider, "base_env": base_env, "keys": keys})
    return {"ok": True, "providers": result}


class AddKeyRequest(BaseModel):
    provider: str
    api_key: str
    slot: int = 1   # 1=primary, 2=secondary, ...


@app.post("/api/keys/add")
async def api_keys_add(req: AddKeyRequest):
    """Добавляет или обновляет API ключ провайдера в .env файле."""
    base_env = _PROVIDER_ENV_KEYS.get(req.provider.lower())
    if not base_env:
        return {"ok": False, "error": f"Неизвестный провайдер: {req.provider}"}

    env_var = base_env if req.slot == 1 else f"{base_env}_{req.slot}"
    api_key = req.api_key.strip()
    if not api_key:
        return {"ok": False, "error": "Пустой ключ"}

    lines = _read_env_lines()
    # Search for existing line to replace
    updated = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith(f"{env_var}=") or stripped.startswith(f"# {env_var}="):
            lines[i] = f"{env_var}={api_key}"
            updated = True
            break
    if not updated:
        # Find insert position after last key of this provider
        insert_at = len(lines)
        for i, line in enumerate(lines):
            if base_env in line:
                insert_at = i + 1
        lines.insert(insert_at, f"{env_var}={api_key}")

    _write_env_lines(lines)

    # Apply to current process immediately
    os.environ[env_var] = api_key

    # Invalidate token budget & model discovery cache
    try:
        from freepalp.core import token_budget as _tb
        _tb.reset()
        from freepalp.core.model_discovery import invalidate
        invalidate()
        # Re-run discovery & restore provider
        await _get_orch().router.refresh()
        _get_orch().router.restore_provider(req.provider)
    except Exception as e:
        pass

    return {"ok": True, "env_var": env_var, "preview": _mask_key(api_key)}


class DeleteKeyRequest(BaseModel):
    provider: str
    slot: int = 1


@app.post("/api/keys/delete")
async def api_keys_delete(req: DeleteKeyRequest):
    """Удаляет API ключ провайдера из .env (закомментирует строку)."""
    base_env = _PROVIDER_ENV_KEYS.get(req.provider.lower())
    if not base_env:
        return {"ok": False, "error": f"Неизвестный провайдер: {req.provider}"}

    env_var = base_env if req.slot == 1 else f"{base_env}_{req.slot}"
    lines = _read_env_lines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{env_var}="):
            lines[i] = f"# {line.strip()}"  # comment out
            found = True
            break

    if found:
        _write_env_lines(lines)
        os.environ.pop(env_var, None)
        try:
            from freepalp.core import token_budget as _tb
            _tb.reset()
        except Exception:
            pass

    return {"ok": found, "env_var": env_var}


class TestKeyRequest(BaseModel):
    provider: str
    api_key: str


@app.post("/api/keys/test")
async def api_keys_test(req: TestKeyRequest):
    """Быстро проверяет валидность API ключа."""
    import httpx
    provider = req.provider.lower()
    key = req.api_key.strip()
    try:
        async with httpx.AsyncClient(timeout=6.0) as client:
            if provider == "groq":
                r = await client.get("https://api.groq.com/openai/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
            elif provider == "cerebras":
                r = await client.get("https://api.cerebras.ai/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
            elif provider == "openrouter":
                r = await client.get("https://openrouter.ai/api/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
            elif provider == "gemini":
                r = await client.get(
                    f"https://generativelanguage.googleapis.com/v1beta/models?key={key}")
            elif provider == "sambanova":
                r = await client.get("https://api.sambanova.ai/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
            elif provider == "together":
                r = await client.get("https://api.together.xyz/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
            elif provider == "mistral":
                r = await client.get("https://api.mistral.ai/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
            elif provider == "novita":
                # /models публичный, /account endpoints возвращают 403
                # Единственный надёжный способ — минимальный completion (1 токен)
                r = await client.post(
                    "https://api.novita.ai/v3/openai/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": "meta-llama/llama-3.1-8b-instruct",
                          "messages": [{"role": "user", "content": "hi"}],
                          "max_tokens": 1, "stream": False}
                )
            elif provider == "openai":
                r = await client.get("https://api.openai.com/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
            elif provider == "anthropic":
                r = await client.get("https://api.anthropic.com/v1/models",
                                     headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
            else:
                return {"ok": False, "valid": False, "error": f"Тест не реализован для провайдера: {provider}"}

        valid = r.status_code in (200, 201)
        msg = f"HTTP {r.status_code}"
        if r.status_code == 401:
            msg = "Неверный ключ (401)"
        elif r.status_code == 429:
            msg = "Ключ валиден, но rate limit"
            valid = True
        elif r.status_code == 200:
            # count models if available
            try:
                data = r.json()
                n = len(data.get("data", data.get("models", [])))
                msg = f"✅ Валиден — {n} моделей"
            except Exception:
                msg = "✅ Валиден"
        return {"ok": True, "valid": valid, "message": msg, "status": r.status_code}
    except Exception as e:
        return {"ok": False, "valid": False, "error": str(e)[:100]}


# ══════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════

def _print_banner(port: int) -> None:
    """Красивый стартовый баннер с осьминогом (вдохновлено qclaw).

    UTF-8-safe: на cp1251-консолях без эмодзи — ASCII fallback.
    ANSI-цвета включаются на Windows 10+ через VT processing.
    """
    # Пробуем включить ANSI-цвета (Windows 10+)
    use_color = True
    try:
        import os as _os
        if _os.name == "nt":
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
    except Exception:
        use_color = False

    # Цвета
    C  = "\033[38;5;213m" if use_color else ""   # розово-фиолетовый
    B  = "\033[38;5;117m" if use_color else ""   # голубой
    G  = "\033[38;5;84m"  if use_color else ""   # зелёный
    D  = "\033[38;5;245m" if use_color else ""   # серый
    BD = "\033[1m"        if use_color else ""   # жирный
    R  = "\033[0m"        if use_color else ""   # сброс

    url = f"http://localhost:{port}"
    banner = f"""
   {C}{BD}🐙  FreePalp{R}  {D}AI Orchestrator{R}
   {C}╭───────────────────────────────────────────────╮{R}
   {C}│{R}   {D}Multi-agent · ReAct · Self-improving v1.0{R}    {C}│{R}
   {C}╰───────────────────────────────────────────────╯{R}

   {B}🌐  WebUI{R}     {D}→{R}  {BD}{url}{R}
   {B}📚  API docs{R}  {D}→{R}  {url}/docs

   {G}⚡ 11 провайдеров{R}  {D}·{R}  {G}🧠 4-слойная память{R}  {D}·{R}  {G}🪝 hooks{R}
   {D}Запуск...{R}
"""
    # ASCII fallback для cp1251-консолей
    ascii_banner = (
        f"\n   FreePalp AI Orchestrator (v1.0)\n"
        f"   Multi-agent | ReAct | Self-improving\n\n"
        f"   WebUI    -> {url}\n"
        f"   API docs -> {url}/docs\n"
    )
    try:
        print(banner, flush=True)
    except (UnicodeEncodeError, Exception):
        print(ascii_banner, flush=True)


def run_gateway(host: str = "0.0.0.0", port: int = 28800):
    import uvicorn
    _print_banner(port)
    uvicorn.run("freepalp.gateway:app", host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run_gateway()
