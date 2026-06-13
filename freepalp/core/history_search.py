"""
FTS5-поиск по собственной истории сессий (идея из MiMo Code).

Агент и пользователь могут искать по всем прошлым диалогам как по базе:
«когда мы обсуждали X?», «что я просил про песочницу?». Индекс — SQLite FTS5
над freepalp/memory/sessions/*.jsonl, инкрементальный (по mtime файла).
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Optional

_MEM = Path(__file__).parent.parent / "memory"
_SESSIONS = _MEM / "sessions"
_DB = _MEM / "history_index.db"


def _connect() -> sqlite3.Connection:
    con = sqlite3.connect(str(_DB))
    con.execute("CREATE TABLE IF NOT EXISTS files(session_id TEXT PRIMARY KEY, mtime REAL)")
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS sess_fts USING fts5("
        "session_id UNINDEXED, ts UNINDEXED, preview UNINDEXED, body)"
    )
    return con


def _extract(path: Path) -> Optional[dict]:
    """Из jsonl-сессии достаёт ts, preview (первый запрос юзера) и весь текст."""
    try:
        ts = ""
        preview = ""
        parts: list[str] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") == "session":
                ts = obj.get("timestamp", "")
            elif obj.get("type") == "message":
                msg = obj.get("message", {})
                role = msg.get("role", "")
                content = msg.get("content", "")
                text = ""
                if isinstance(content, list):
                    text = " ".join(c.get("text", "") for c in content
                                     if isinstance(c, dict) and c.get("type") == "text")
                elif isinstance(content, str):
                    text = content
                if text:
                    parts.append(text)
                    if role == "user" and not preview:
                        preview = text[:120]
        if not parts:
            return None
        return {"session_id": path.stem, "ts": ts,
                "preview": preview or parts[0][:120], "body": "\n".join(parts)}
    except Exception:
        return None


def reindex(force: bool = False) -> dict:
    """Инкрементально индексирует новые/изменённые сессии."""
    if not _SESSIONS.exists():
        return {"indexed": 0, "total": 0}
    con = _connect()
    known = {} if force else {r[0]: r[1] for r in con.execute("SELECT session_id, mtime FROM files")}
    if force:
        con.execute("DELETE FROM sess_fts")
        con.execute("DELETE FROM files")
    indexed = 0
    total = 0
    for path in _SESSIONS.glob("*.jsonl"):
        total += 1
        mtime = path.stat().st_mtime
        if known.get(path.stem) == mtime:
            continue
        data = _extract(path)
        if not data:
            continue
        con.execute("DELETE FROM sess_fts WHERE session_id=?", (path.stem,))
        con.execute("INSERT INTO sess_fts(session_id, ts, preview, body) VALUES (?,?,?,?)",
                    (data["session_id"], data["ts"], data["preview"], data["body"]))
        con.execute("INSERT OR REPLACE INTO files(session_id, mtime) VALUES (?,?)",
                    (path.stem, mtime))
        indexed += 1
    con.commit()
    con.close()
    return {"indexed": indexed, "total": total}


def _fts_query(q: str) -> str:
    """Безопасный FTS5-запрос с префиксным матчингом — терпит словоформы
    (FTS5 без стемминга русского: 'песочниц*' ловит песочница/-е/-у)."""
    import re
    words = re.findall(r"\w+", q.lower(), re.UNICODE)
    terms = []
    for w in words:
        prefix = w if len(w) <= 4 else w[:max(4, len(w) - 2)]
        terms.append(prefix + "*")
    return " ".join(terms) or '""'


def search(query: str, limit: int = 15) -> list[dict]:
    if not query.strip() or not _DB.exists():
        return []
    try:
        con = _connect()
        rows = con.execute(
            "SELECT session_id, ts, preview, snippet(sess_fts, 3, '«', '»', '…', 14) "
            "FROM sess_fts WHERE sess_fts MATCH ? ORDER BY rank LIMIT ?",
            (_fts_query(query), limit),
        ).fetchall()
        con.close()
        return [{"session_id": r[0], "ts": r[1], "preview": r[2], "snippet": r[3]}
                for r in rows]
    except Exception:
        return []
