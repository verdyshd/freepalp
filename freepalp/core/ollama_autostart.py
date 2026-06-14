"""
Автозапуск Ollama при старте FreePalp.

Логика «если была подключена раньше»: при первом успешном обнаружении Ollama
ставим флаг was_connected + запоминаем путь к exe. На последующих стартах, если
Ollama не отвечает, а флаг стоит и exe найден — поднимаем `ollama serve` в фоне.
Так пользователям, которые Ollama не используют, ничего не запускается.

Состояние — в freepalp/memory/ollama_state.json (runtime, не в git).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

_STATE = Path(__file__).parent.parent / "memory" / "ollama_state.json"
_OLLAMA_URL = "http://localhost:11434/api/tags"

# Типичные места установки Ollama (Windows / *nix)
_KNOWN_PATHS = [
    Path(os.path.expanduser("~")) / "AppData/Local/Programs/Ollama/ollama.exe",
    Path("C:/Program Files/Ollama/ollama.exe"),
    Path("F:/Tools/Ollama/ollama.exe"),
    Path("/usr/local/bin/ollama"),
    Path("/usr/bin/ollama"),
    Path(os.path.expanduser("~")) / ".ollama/bin/ollama",
]


def _load() -> dict:
    try:
        return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        _STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def is_responding(timeout: float = 2.0) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(_OLLAMA_URL, timeout=timeout):
            return True
    except Exception:
        return False


def find_exe(hint: Optional[str] = None) -> Optional[str]:
    if hint and Path(hint).exists():
        return hint
    found = shutil.which("ollama")
    if found:
        return found
    for p in _KNOWN_PATHS:
        if p.exists():
            return str(p)
    return None


def mark_connected() -> None:
    """Зафиксировать, что Ollama была подключена (вызывать при обнаружении)."""
    st = _load()
    st["was_connected"] = True
    exe = find_exe(st.get("exe"))
    if exe:
        st["exe"] = exe
    _save(st)


def _spawn(exe: str) -> bool:
    try:
        from .winproc import no_window
        kwargs: dict = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
                        "stdin": subprocess.DEVNULL}
        if sys.platform == "win32":
            # CREATE_NO_WINDOW — без мигающего консольного окна. НЕ комбинируем с
            # DETACHED_PROCESS: эти флаги конфликтуют, и окно может всё равно мелькать.
            kwargs.update(no_window())
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([exe, "serve"], **kwargs)
        return True
    except Exception:
        return False


def ensure_running() -> str:
    """Главная точка: вызывать при старте сервера.
    Возвращает: 'already' | 'started' | 'failed' | 'skipped'."""
    if is_responding():
        mark_connected()
        return "already"
    st = _load()
    if not st.get("was_connected"):
        return "skipped"   # раньше не подключалась — не трогаем
    exe = find_exe(st.get("exe"))
    if not exe:
        return "failed"    # флаг есть, но бинарь не найден
    if not _spawn(exe):
        return "failed"
    # Ждём подъёма (до ~12с)
    for _ in range(12):
        time.sleep(1.0)
        if is_responding():
            return "started"
    return "failed"
