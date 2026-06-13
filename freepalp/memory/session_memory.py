"""
FreePalp Session Memory — персистентная память сессий.
Вдохновлено: QClaw session-logs + Claude Code CLAUDE.md/memory.

Три функции:
  1. SNAPSHOT  — после каждого обмена пишем checkpoint на диск.
                 При краше/рестарте диалог не теряется.
  2. RESUME    — при старте gateway загружаем все активные сессии обратно.
  3. CONTEXT   — при старте читаем последние N сессий, строим дайджест
                 "что обсуждали раньше" и инжектируем в HOT memory.

Структура файлов:
  memory/
  ├── active_sessions/
  │   ├── {conv_id}.json     ← текущий диалог (snapshot, перезаписывается)
  │   └── ...
  └── session_digests/
      └── recent.md          ← дайджест последних сессий (загружается в HOT)
"""

from __future__ import annotations
import json
from pathlib import Path
from datetime import datetime
from typing import Optional

_BASE          = Path(__file__).parent
ACTIVE_DIR     = _BASE / "active_sessions"
DIGESTS_DIR    = _BASE / "session_digests"
SESSIONS_DIR   = _BASE / "sessions"        # JSONL логи (уже существуют)
RECENT_DIGEST  = DIGESTS_DIR / "recent.md"

# Сколько прошлых сессий включать в дайджест
DIGEST_SESSIONS = 5
# Максимум строк из каждой сессии в дайджест
DIGEST_MAX_PER_SESSION = 4
# Максимум активных сессий в памяти (на случай много вкладок)
MAX_ACTIVE_SESSIONS = 20
# Сколько обменов хранить в снэпшоте (каждый обмен = 2 сообщения)
SNAPSHOT_MAX_EXCHANGES = 15


def save_snapshot(conv_id: str, history: list[dict]) -> None:
    """
    Сохраняет текущий диалог на диск после каждого обмена.
    Перезаписывает — не накапливает, только актуальное состояние.
    """
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "conv_id":    conv_id,
        "saved_at":   datetime.now().isoformat(),
        "history":    history[-(SNAPSHOT_MAX_EXCHANGES * 2):],  # trim
        "msg_count":  len(history),
    }
    path = ACTIVE_DIR / f"{conv_id}.json"
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_active_sessions() -> dict[str, list[dict]]:
    """
    Загружает все сохранённые активные сессии при старте gateway.
    Возвращает словарь conv_id → history для восстановления _conversations.
    """
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    sessions: dict[str, list[dict]] = {}

    files = sorted(ACTIVE_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    for f in files[:MAX_ACTIVE_SESSIONS]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            conv_id = data.get("conv_id") or f.stem
            history = data.get("history", [])
            if history:  # не загружаем пустые
                sessions[conv_id] = history
        except Exception:
            pass

    return sessions


def save_active_sessions(conversations: dict[str, list[dict]]) -> None:
    """Сохраняет все активные сессии разом — для graceful shutdown gateway."""
    for conv_id, history in conversations.items():
        if history:
            try:
                save_snapshot(conv_id, history)
            except Exception:
                pass


def clear_snapshot(conv_id: str) -> None:
    """Удаляет снэпшот после явного /new chat."""
    path = ACTIVE_DIR / f"{conv_id}.json"
    if path.exists():
        path.unlink()


def build_session_digest() -> str:
    """
    Читает последние N JSONL сессий и строит текстовый дайджест.
    Формат: одна строка на сессию — дата + ключевые вопросы пользователя.

    Возвращает markdown-строку для инжекции в HOT memory.
    """
    if not SESSIONS_DIR.exists():
        return ""

    # Берём последние N файлов по дате изменения
    files = sorted(
        SESSIONS_DIR.glob("*.jsonl"),
        key=lambda f: f.stat().st_mtime,
        reverse=True
    )[:DIGEST_SESSIONS]

    if not files:
        return ""

    lines = ["## Последние сессии FreePalp"]

    for log_file in reversed(files):  # хронологически
        try:
            records = []
            for raw in log_file.read_text(encoding="utf-8").splitlines():
                try:
                    records.append(json.loads(raw))
                except Exception:
                    continue

            # Дата сессии из первой записи
            session_date = ""
            for r in records:
                if r.get("type") == "session":
                    ts = r.get("timestamp", "")
                    session_date = ts[:16] if ts else ""
                    break

            # Вопросы пользователя (первые DIGEST_MAX_PER_SESSION)
            user_msgs = []
            for r in records:
                if r.get("type") == "message":
                    msg = r.get("message", {})
                    if msg.get("role") == "user":
                        content = msg.get("content", [])
                        text = ""
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict):
                                    text = c.get("text", "")[:120]
                                    break
                        elif isinstance(content, str):
                            text = content[:120]
                        if text.strip():
                            user_msgs.append(text.strip())
                        if len(user_msgs) >= DIGEST_MAX_PER_SESSION:
                            break

            if user_msgs:
                date_str = f"[{session_date}] " if session_date else ""
                for i, msg in enumerate(user_msgs):
                    prefix = date_str if i == 0 else " " * len(date_str)
                    lines.append(f"- {prefix}{msg}")

        except Exception:
            continue

    if len(lines) <= 1:
        return ""

    return "\n".join(lines)


def save_digest_to_file(digest: str) -> None:
    """Сохраняет дайджест в файл для отладки."""
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)
    RECENT_DIGEST.write_text(
        f"# FreePalp Recent Sessions Digest\n"
        f"_Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n\n"
        + digest,
        encoding="utf-8"
    )


def get_or_build_digest(force: bool = False) -> str:
    """
    Возвращает кешированный дайджест или строит новый.
    Пересчитывается если файл старше 1 часа или force=True.
    """
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)

    if not force and RECENT_DIGEST.exists():
        age_seconds = (
            datetime.now() -
            datetime.fromtimestamp(RECENT_DIGEST.stat().st_mtime)
        ).total_seconds()
        if age_seconds < 3600:  # кеш 1 час
            content = RECENT_DIGEST.read_text(encoding="utf-8")
            # Возвращаем только блок дайджеста (без заголовка файла)
            for line in content.splitlines():
                if line.startswith("## Последние сессии"):
                    idx = content.index(line)
                    return content[idx:]
            return content

    digest = build_session_digest()
    if digest:
        save_digest_to_file(digest)
    return digest
