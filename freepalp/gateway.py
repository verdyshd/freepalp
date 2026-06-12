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

# ── Auto-shutdown ─────────────────────────────────────────────────────────
# Сервер автоматически выключается если нет запросов IDLE_SHUTDOWN_MINUTES.
# 0 = отключено (запустить с env FREEPALP_IDLE_SHUTDOWN=0 чтобы отключить).
IDLE_SHUTDOWN_MINUTES: int = int(os.environ.get("FREEPALP_IDLE_SHUTDOWN", "30"))
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
        _root = Path(__file__).parent.parent
        if sys.platform == "win32":
            # Новый процесс: ждём 5с (пока освободится порт) → запускаем сервер
            _sp.Popen(
                f'cmd /c "timeout /t 5 /nobreak >nul 2>&1 && python freepalp\\app.py --web"',
                shell=True,
                cwd=str(_root),
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

    # 3. Live discovery
    try:
        orch = _get_orch()
        await orch.router.initialize()
    except Exception:
        pass

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
                    print(f"  [SI] Auto-improved on startup → v{report['version_proposed']} activated!")
                elif report.get("error"):
                    print(f"  [SI] Startup auto-improve skipped: {report['error']}")
        except Exception as e:
            print(f"  [SI] Startup improve error: {e}")

    asyncio.create_task(_startup_improve())

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
            timeout=120.0,   # глобальный таймаут запроса
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
                timeout=120.0,
            )
            fb   = result.critic_feedback
            tin  = sum(m.metadata.get("tokens_in",  0)   for m in result.messages)
            tout = sum(m.metadata.get("tokens_out", 0)   for m in result.messages)
            cost = sum(m.metadata.get("cost_usd",   0.0) for m in result.messages)
            import re as _re
            tool_calls = [{"tool": m, "ok": True}
                          for msg in result.messages
                          for m in _re.findall(r"TOOL RESULT \[(\w+)\]", msg.content)]

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
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=110.0)
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'error', 'text': 'Queue timeout'}, ensure_ascii=False)}\n\n"
                    break
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
        # Добавляем подробную разбивку по типам (исключаем API-ошибки)
        records = ev.load_recent(100)
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
