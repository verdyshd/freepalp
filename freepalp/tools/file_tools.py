"""
File Tools — безопасные операции с файлами.
Все операции ограничены SANDBOX_ROOT.
"""

import os
import json
from functools import lru_cache
from pathlib import Path
from typing import Optional

# Корень sandbox — агент не может выйти за его пределы
SANDBOX_ROOT = Path(os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "sandbox")
))


def _safe_path(relative_path: str) -> Path:
    """
    Превращает относительный путь в абсолютный внутри sandbox.
    Блокирует path traversal (../../../etc/passwd).
    """
    norm = relative_path.replace("\\", "/").lstrip("/")
    if norm == "sandbox" or norm.startswith("sandbox/"):
        norm = norm[len("sandbox"):].lstrip("/")
    target = (SANDBOX_ROOT / norm).resolve()
    if target != SANDBOX_ROOT and SANDBOX_ROOT not in target.parents:
        raise PermissionError(
            f"Путь '{relative_path}' выходит за пределы sandbox. Запрещено."
        )
    return target


@lru_cache(maxsize=64)
def _read_cached(abs_path: str, mtime: float) -> str:
    """LRU-кэш чтения файла. Ключ включает mtime — автоинвалидация при изменении."""
    return Path(abs_path).read_text(encoding="utf-8")


def read_file(path: str) -> dict:
    """Читает файл из sandbox. Кэширует результат (инвалидируется при записи)."""
    try:
        safe = _safe_path(path)
        if not safe.exists():
            # Подсказываем что есть рядом — агент не галлюцинирует
            parent = safe.parent
            if parent.exists() and parent.is_dir():
                siblings = sorted(f.name for f in parent.iterdir()
                                  if not f.name.startswith("."))[:20]
                hint = f"В папке '{parent.name}/': {', '.join(siblings)}" if siblings else ""
            else:
                hint = ""
            err = f"Файл не найден: {path}"
            if hint:
                err += f". {hint}"
            return {"ok": False, "error": err}
        mtime = safe.stat().st_mtime
        content = _read_cached(str(safe), mtime)
        return {"ok": True, "content": content, "size": len(content)}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка чтения: {e}"}


def write_file(path: str, content: str) -> dict:
    """Записывает файл в sandbox. Создаёт директории если нужно."""
    try:
        safe = _safe_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")
        _read_cached.cache_clear()  # инвалидируем кэш при записи
        return {"ok": True, "path": str(safe.relative_to(SANDBOX_ROOT))}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка записи: {e}"}


def list_files(path: str = ".") -> dict:
    """Список файлов в директории sandbox."""
    try:
        safe = _safe_path(path)
        if not safe.exists():
            return {"ok": False, "error": f"Директория не найдена: {path}"}
        if not safe.is_dir():
            return {"ok": False, "error": f"Не является директорией: {path}"}

        files = []
        for item in safe.iterdir():
            files.append({
                "name": item.name,
                "type": "dir" if item.is_dir() else "file",
                "size": item.stat().st_size if item.is_file() else 0,
            })
        return {"ok": True, "files": sorted(files, key=lambda x: x["name"])}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delete_file(path: str) -> dict:
    """Удаляет файл из sandbox."""
    try:
        safe = _safe_path(path)
        if not safe.exists():
            return {"ok": False, "error": f"Файл не найден: {path}"}
        if safe.is_dir():
            return {"ok": False, "error": "Используй delete_dir для директорий"}
        safe.unlink()
        return {"ok": True, "deleted": path}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def create_dir(path: str) -> dict:
    """Создаёт директорию в sandbox."""
    try:
        safe = _safe_path(path)
        safe.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "created": path}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def file_exists(path: str) -> bool:
    """Проверяет существование файла."""
    try:
        return _safe_path(path).exists()
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Инструменты самомодификации — доступ к исходникам FreePalp
# Разрешено: чтение всего проекта, запись только в freepalp/ и config/
# ─────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
))

# Пути куда разрешена запись при самомодификации
_WRITE_ALLOWED_PREFIXES = [
    "freepalp/",
    "freepalp\\",
]

# Пути куда запись ЗАПРЕЩЕНА даже при самомодификации
_WRITE_FORBIDDEN = [".env", "requirements.txt", "setup.py"]


def _safe_source_path(relative_path: str) -> Path:
    """Путь внутри PROJECT_ROOT. Блокирует traversal."""
    target = (PROJECT_ROOT / relative_path).resolve()
    if target != PROJECT_ROOT and PROJECT_ROOT not in target.parents:
        raise PermissionError(f"Путь '{relative_path}' выходит за пределы проекта.")
    return target


@lru_cache(maxsize=32)
def _read_source_cached(abs_path: str, mtime: float) -> str:
    return Path(abs_path).read_text(encoding="utf-8")


def read_source(path: str) -> dict:
    """
    Читает исходный файл системы FreePalp (за пределами sandbox).
    Используй для анализа и самоулучшения системы.
    Пример: read_source('freepalp/core/orchestrator.py')
    """
    try:
        safe = _safe_source_path(path)
        if not safe.exists():
            # Подсказка: что есть рядом
            parent = safe.parent
            if parent.exists():
                siblings = sorted(f.name for f in parent.iterdir()
                                  if not f.name.startswith("__") and not f.suffix == ".pyc")[:20]
                hint = f"В '{parent.name}/': {', '.join(siblings)}" if siblings else ""
            else:
                hint = ""
            err = f"Файл не найден: {path}"
            if hint:
                err += f". {hint}"
            return {"ok": False, "error": err}
        if safe.is_dir():
            return {"ok": False, "error": f"Это директория, используй list_source"}
        mtime = safe.stat().st_mtime
        content = _read_source_cached(str(safe), mtime)
        return {"ok": True, "content": content, "size": len(content), "path": path}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка чтения: {e}"}


def list_source(path: str = ".") -> dict:
    """
    Список файлов/директорий в проекте FreePalp.
    Используй для навигации по исходникам перед анализом или изменением.
    Пример: list_source('freepalp/core') или list_source('freepalp')
    """
    try:
        safe = _safe_source_path(path)
        if not safe.exists():
            return {"ok": False, "error": f"Директория не найдена: {path}"}
        if not safe.is_dir():
            return {"ok": False, "error": f"Не директория: {path}"}

        files = []
        for item in safe.iterdir():
            if item.name.startswith("__pycache__") or item.suffix == ".pyc":
                continue
            files.append({
                "name":  item.name,
                "type":  "dir" if item.is_dir() else "file",
                "size":  item.stat().st_size if item.is_file() else 0,
            })
        return {"ok": True, "files": sorted(files, key=lambda x: (x["type"] == "file", x["name"]))}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _git_autocommit(abs_path, rel_path: str) -> None:
    """Автокоммит самомодификации в git (тихо; если git нет — просто пропускаем).

    Каждое изменение исходников агентом попадает в историю версий системы,
    видимую в /api/system/versions и через `git log`."""
    import subprocess
    from pathlib import Path as _P
    repo_root = str(_P(__file__).parent.parent.parent)  # корень репозитория
    try:
        subprocess.run(["git", "add", str(abs_path)],
                       capture_output=True, cwd=repo_root, timeout=10)
        subprocess.run(
            ["git", "-c", "user.name=FreePalp", "-c", "user.email=freepalp@local",
             "commit", "-m", f"self-mod: {rel_path} (изменено агентом)"],
            capture_output=True, cwd=repo_root, timeout=15,
        )
    except Exception:
        pass  # отсутствие git не должно ломать запись файла


def write_source(path: str, content: str) -> dict:
    """
    Записывает/изменяет исходный файл системы FreePalp.
    Разрешено только для freepalp/ директории.
    Используй для самоулучшения: исправления багов, добавления функций.
    ВАЖНО: всегда сначала read_source чтобы не потерять существующий код.
    Пример: write_source('freepalp/config/prompts.json', '...')
    """
    try:
        # Проверяем что путь в разрешённых директориях
        norm = path.replace("\\", "/")
        allowed = any(norm.startswith(p.replace("\\", "/")) for p in _WRITE_ALLOWED_PREFIXES)
        if not allowed:
            return {
                "ok": False,
                "error": f"Запись разрешена только в freepalp/. Путь '{path}' не разрешён.",
            }

        # Запрещённые критические файлы
        if any(norm.endswith(f) for f in _WRITE_FORBIDDEN):
            return {"ok": False, "error": f"Запись в '{path}' запрещена (критический файл)."}

        safe = _safe_source_path(path)
        safe.parent.mkdir(parents=True, exist_ok=True)
        safe.write_text(content, encoding="utf-8")
        _read_source_cached.cache_clear()  # инвалидируем кэш при записи
        _git_autocommit(safe, path)        # автолог самомодификаций (см. /api/system/versions)
        return {"ok": True, "path": path, "size": len(content),
                "message": "Файл обновлён. Перезапусти сервер для применения изменений."}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка записи: {e}"}


# Реестр инструментов для ToolAgent
FILE_TOOLS = {
    "read_file": {
        "fn": read_file,
        "description": "Читает файл из рабочей папки (sandbox). Аргумент: path (str)",
        "args": ["path"],
    },
    "write_file": {
        "fn": write_file,
        "description": "Записывает файл в рабочую папку (sandbox). Аргументы: path (str), content (str)",
        "args": ["path", "content"],
    },
    "list_files": {
        "fn": list_files,
        "description": "Список файлов в рабочей папке (sandbox). Аргумент: path (str, default='.')",
        "args": ["path"],
    },
    "delete_file": {
        "fn": delete_file,
        "description": "Удаляет файл из sandbox. Аргумент: path (str)",
        "args": ["path"],
    },
    "create_dir": {
        "fn": create_dir,
        "description": "Создаёт директорию в sandbox. Аргумент: path (str)",
        "args": ["path"],
    },
    "read_source": {
        "fn": read_source,
        "description": "Читает исходный файл системы FreePalp для анализа и самоулучшения. Аргумент: path (str, например 'freepalp/core/orchestrator.py')",
        "args": ["path"],
    },
    "list_source": {
        "fn": list_source,
        "description": "Список файлов в исходниках проекта FreePalp. Аргумент: path (str, например 'freepalp' или 'freepalp/core')",
        "args": ["path"],
    },
    "write_source": {
        "fn": write_source,
        "description": "Изменяет исходный файл системы FreePalp (самомодификация). Только для freepalp/ директории. Аргументы: path (str), content (str). Всегда сначала вызови read_source!",
        "args": ["path", "content"],
    },
}
