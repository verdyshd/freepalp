"""
Session Logger — сохраняет каждую сессию в JSONL формате.
Вдохновлено: QClaw session-logs skill.

Структура:
  freepalp/memory/sessions/
    sessions.json           ← индекс сессий
    <session-id>.jsonl      ← полный лог сессии

JSONL формат совместим с QClaw — можно анализировать теми же инструментами.
"""

from __future__ import annotations
import json
import uuid
from pathlib import Path
from datetime import datetime
from typing import Optional

SESSIONS_DIR = Path(__file__).parent.parent / "memory" / "sessions"
SESSIONS_INDEX = SESSIONS_DIR / "sessions.json"


class SessionLogger:
    """
    Логирует каждое взаимодействие в JSONL файл.
    Каждый запуск app.py = новая сессия.
    """

    def __init__(self, session_id: Optional[str] = None):
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + str(uuid.uuid4())[:4]
        self.log_file = SESSIONS_DIR / f"{self.session_id}.jsonl"
        self._write_session_header()
        self._update_index()

    # ─────────────────────────────────────────────
    # Публичный API
    # ─────────────────────────────────────────────

    def log_user(self, text: str):
        """Логирует сообщение пользователя."""
        self._append({
            "type": "message",
            "timestamp": _now(),
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": text}]
            }
        })

    def log_assistant(self, text: str, model: str = "", tokens: int = 0,
                      iterations: int = 1, score: float = 0.0):
        """Логирует ответ агента."""
        self._append({
            "type": "message",
            "timestamp": _now(),
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
                "model": model,
                "usage": {
                    "tokens": tokens,
                    "iterations": iterations,
                    "critic_score": round(score, 2)
                }
            }
        })

    def log_tool_call(self, tool_name: str, args: dict, result: dict):
        """Логирует вызов инструмента."""
        self._append({
            "type": "toolCall",
            "timestamp": _now(),
            "tool": tool_name,
            "args": args,
            "result_ok": result.get("ok", False),
            "result_preview": str(result)[:200]
        })

    def log_error(self, error: str, context: str = ""):
        """Логирует ошибку."""
        self._append({
            "type": "error",
            "timestamp": _now(),
            "error": error,
            "context": context
        })

    # ─────────────────────────────────────────────
    # Внутренние методы
    # ─────────────────────────────────────────────

    def _append(self, record: dict):
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _write_session_header(self):
        self._append({
            "type": "session",
            "timestamp": _now(),
            "session_id": self.session_id,
            "version": "freepalp/0.1"
        })

    def _update_index(self):
        """Обновляет sessions.json индекс."""
        index: dict = {}
        if SESSIONS_INDEX.exists():
            try:
                index = json.loads(SESSIONS_INDEX.read_text(encoding="utf-8"))
            except Exception:
                index = {}
        index[self.session_id] = {
            "created_at": _now(),
            "file": f"{self.session_id}.jsonl"
        }
        # Хранить последние 200 сессий
        if len(index) > 200:
            oldest = sorted(index.keys())[0]
            del index[oldest]
        SESSIONS_INDEX.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )


# ─────────────────────────────────────────────
# Поиск по сессиям (как в QClaw session-logs skill)
# ─────────────────────────────────────────────

def search_sessions(query: str, last_n: int = 5) -> list[dict]:
    """
    Ищет query в последних N сессиях.
    Возвращает список совпадений с контекстом.
    """
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    files = sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)

    for log_file in files[:last_n]:
        matches = []
        try:
            for line in log_file.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("type") == "message":
                    content = record["message"].get("content", [])
                    for c in content:
                        if isinstance(c, dict) and query.lower() in c.get("text", "").lower():
                            matches.append({
                                "role": record["message"]["role"],
                                "snippet": c["text"][:200],
                                "timestamp": record["timestamp"]
                            })
        except Exception:
            continue
        if matches:
            results.append({"session": log_file.stem, "matches": matches})

    return results


def get_session_stats() -> dict:
    """Статистика сессий."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    files = list(SESSIONS_DIR.glob("*.jsonl"))
    total_messages = 0
    for f in files:
        try:
            lines = f.read_text(encoding="utf-8").splitlines()
            total_messages += sum(
                1 for l in lines
                if '"type": "message"' in l or '"type":"message"' in l
            )
        except Exception:
            pass
    return {
        "total_sessions": len(files),
        "total_messages": total_messages,
        "sessions_dir": str(SESSIONS_DIR)
    }


def _now() -> str:
    return datetime.now().isoformat()
