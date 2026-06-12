"""
System Tools — инструменты для управления самой системой FreePalp.

Позволяют Worker-агенту автономно:
  - читать/обновлять память
  - управлять cron-задачами
  - просматривать провайдеры и модели
  - работать с навыками
  - смотреть статистику и метрики

Это "внутренние" инструменты — агент вызывает их через ReAct loop.
"""

from __future__ import annotations
import os
import json
from pathlib import Path
from typing import Optional

_BASE = Path(__file__).parent.parent


# ══════════════════════════════════════════════════════════════════════════════
# MEMORY
# ══════════════════════════════════════════════════════════════════════════════

def memory_read(tier: str = "hot") -> dict:
    """
    Прочитать содержимое памяти агента.

    Args:
        tier: "hot" (активная) | "corrections" (исправления) | "stats" (статистика)

    Returns:
        {"ok": True, "content": str, "lines": int}
    """
    try:
        from freepalp.memory.memory_manager import MemoryManager
        mm = MemoryManager()
        if tier == "hot":
            content = mm.load_hot()
            return {"ok": True, "tier": "hot", "content": content, "lines": len(content.splitlines())}
        elif tier == "corrections":
            from freepalp.memory.memory_manager import CORRECTIONS
            corrections_path = CORRECTIONS
            content = corrections_path.read_text("utf-8") if corrections_path.exists() else ""
            return {"ok": True, "tier": "corrections", "content": content[:3000], "lines": len(content.splitlines())}
        elif tier == "stats":
            stats = mm.get_stats()
            return {"ok": True, "tier": "stats", **stats}
        else:
            return {"ok": False, "error": f"Неизвестный tier: {tier}. Доступны: hot, corrections, stats"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def memory_write(content: str, mode: str = "append") -> dict:
    """
    Записать заметку в HOT память агента.

    Args:
        content: текст для записи
        mode:    "append" (добавить) | "replace" (заменить всё)

    Returns:
        {"ok": True, "lines_total": int} или {"ok": True, "skipped": "duplicate", ...}
    """
    try:
        from freepalp.memory.memory_manager import MemoryManager, HOT_FILE
        mm = MemoryManager()
        if mode == "replace":
            HOT_FILE.write_text(content, encoding="utf-8")
        else:
            # Проверяем дубликат перед записью (первые 80 символов)
            key = content.lower().strip()[:80]
            existing = mm.load_hot()
            if key in existing.lower():
                return {"ok": True, "lines_total": len(existing.splitlines()), "skipped": "duplicate"}
            mm.add_to_hot("agent", content)
        hot = mm.load_hot()
        return {"ok": True, "lines_total": len(hot.splitlines())}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def memory_search(query: str, max_results: int = 5) -> dict:
    """
    Поиск по COLD архиву памяти.

    Args:
        query:       поисковый запрос
        max_results: максимум результатов

    Returns:
        {"ok": True, "results": [...], "total": int}
    """
    try:
        from freepalp.memory.memory_manager import MemoryManager
        mm  = MemoryManager()
        res = mm.search_cold(query, max_results=int(max_results))  # str→int для native FC
        return {"ok": True, "results": res, "total": len(res)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def memory_forget(keyword: str) -> dict:
    """
    Удалить из HOT памяти строки содержащие ключевое слово.

    Args:
        keyword: слово для поиска и удаления

    Returns:
        {"ok": True, "removed": int}
    """
    try:
        from freepalp.memory.memory_manager import MemoryManager
        mm      = MemoryManager()
        removed = mm.forget(keyword)
        return {"ok": True, "keyword": keyword, "removed": removed}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# CRON
# ══════════════════════════════════════════════════════════════════════════════

def cron_list() -> dict:
    """
    Список всех cron-задач.

    Returns:
        {"ok": True, "tasks": [...], "total": int}
    """
    try:
        from freepalp.core.cron_manager import CronManager
        from datetime import datetime
        cm    = CronManager()
        crons = cm.list_crons()
        now   = datetime.now()
        tasks = []
        for c in crons:
            next_r = c.get("next_run", "")
            try:
                dt    = datetime.fromisoformat(next_r)
                delta = int((dt - now).total_seconds())
                if delta < 0:
                    next_in = "OVERDUE"
                elif delta < 3600:
                    next_in = f"in {delta//60}m"
                elif delta < 86400:
                    next_in = f"in {delta//3600}h"
                else:
                    next_in = f"in {delta//86400}d"
            except Exception:
                next_in = next_r[:16]
            tasks.append({
                "id":        c["id"],
                "name":      c["name"],
                "interval_h": c["interval_h"],
                "next_run":  next_in,
                "run_count": c.get("run_count", 0),
                "builtin":   c.get("builtin", False),
            })
        return {"ok": True, "tasks": tasks, "total": len(tasks)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cron_add(name: str, interval: str, command: str) -> dict:
    """
    Добавить новую cron-задачу.

    Args:
        name:     имя задачи
        interval: интервал (например: "2h", "1d", "30m", "1w")
        command:  команда для выполнения

    Returns:
        {"ok": True, "id": str, "interval_h": int}
    """
    try:
        from freepalp.core.cron_manager import CronManager
        cm   = CronManager()
        task = cm.add(name, interval, command)
        if task:
            return {"ok": True, "id": task["id"], "name": task["name"], "interval_h": task["interval_h"]}
        else:
            return {"ok": False, "error": f"Не удалось распознать интервал: '{interval}'. Форматы: 30m, 2h, 1d, 1w"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def cron_remove(task_id: str) -> dict:
    """
    Удалить cron-задачу по ID.

    Args:
        task_id: ID задачи (из cron_list)

    Returns:
        {"ok": True} или {"ok": False, "error": "..."}
    """
    try:
        from freepalp.core.cron_manager import CronManager
        cm = CronManager()
        ok = cm.remove(task_id)
        if ok:
            return {"ok": True, "removed": task_id}
        else:
            return {"ok": False, "error": f"Задача '{task_id}' не найдена или является встроенной"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# PROVIDERS & MODELS
# ══════════════════════════════════════════════════════════════════════════════

def providers_list() -> dict:
    """
    Статус всех LLM-провайдеров.

    Returns:
        {"ok": True, "active": [...], "inactive": [...]}
    """
    try:
        from freepalp.core.model_discovery import get_providers_status
        providers = get_providers_status()
        active   = [p for p in providers if p["configured"]]
        inactive = [p for p in providers if not p["configured"]]
        return {
            "ok":      True,
            "active":  [{"name": p["name"], "models": p["models"][:60], "limits": p["limits"][:60]} for p in active],
            "inactive": [{"name": p["name"], "env_key": p["env_key"], "signup": p["signup"]} for p in inactive],
            "active_count":   len(active),
            "inactive_count": len(inactive),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


def models_list() -> dict:
    """
    Список доступных LLM-моделей.

    Returns:
        {"ok": True, "models": [...], "total": int}
    """
    try:
        from freepalp.core.model_discovery import get_cached
        models_raw = get_cached()
        models = [
            {"name": m.get("name", m.get("model_id", "?")), "provider": m.get("provider", "?"), "tier": m.get("tier", "?")}
            for m in models_raw
        ]
        return {"ok": True, "models": models[:20], "total": len(models)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# MCP
# ══════════════════════════════════════════════════════════════════════════════

def mcp_list() -> dict:
    """
    Список MCP-серверов (настроенных и доступных).

    Returns:
        {"ok": True, "configured": [...], "available_ready": [...]}
    """
    try:
        from freepalp.core.mcp_discovery import discover_mcp_servers
        info = discover_mcp_servers()
        ready = [s for s in info["available"] if s["ready"]]
        return {
            "ok":          True,
            "configured":  [{"name": s["name"], "description": s.get("description", ""), "source": s.get("source", "")} for s in info["configured"]],
            "available_ready": [{"name": s["name"], "npm_package": s["npm_package"], "env_key": s.get("env_key")} for s in ready],
            "sources":     info["sources"],
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# SKILLS
# ══════════════════════════════════════════════════════════════════════════════

def skills_list() -> dict:
    """
    Список доступных навыков (встроенных и пользовательских).

    Returns:
        {"ok": True, "skills": [...], "total": int}
    """
    try:
        skills_dir = _BASE / "skills"
        skills     = []
        if skills_dir.exists():
            for f in sorted(skills_dir.glob("*.py")):
                if not f.name.startswith("__"):
                    # Читаем первую строку docstring
                    lines  = f.read_text("utf-8").splitlines()
                    doc    = ""
                    for line in lines[1:5]:
                        line = line.strip().strip('"""').strip("'''").strip()
                        if line and not line.startswith("#"):
                            doc = line
                            break
                    skills.append({"name": f.stem, "description": doc, "path": str(f)})
        return {"ok": True, "skills": skills, "total": len(skills)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def skill_run(skill_name: str, text: str, **kwargs) -> dict:
    """
    Запустить конкретный навык по имени.

    Args:
        skill_name: имя навыка (из skills_list)
        text:       входной текст для навыка

    Returns:
        {"ok": True, "result": str}
    """
    import importlib.util
    import asyncio

    skills_dir = _BASE / "skills"
    skill_path = skills_dir / f"{skill_name}.py"

    if not skill_path.exists():
        return {"ok": False, "error": f"Навык '{skill_name}' не найден. Используй skills_list() для просмотра доступных."}

    try:
        spec   = importlib.util.spec_from_file_location(skill_name, skill_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        run_fn = getattr(module, f"run_{skill_name}", None)
        if not run_fn:
            return {"ok": False, "error": f"В навыке нет функции run_{skill_name}()"}

        import inspect
        if inspect.iscoroutinefunction(run_fn):
            result = asyncio.get_event_loop().run_until_complete(run_fn(text, **kwargs))
        else:
            result = run_fn(text, **kwargs)

        if isinstance(result, dict):
            return result
        return {"ok": True, "result": str(result)}
    except Exception as e:
        return {"ok": False, "error": f"Ошибка выполнения навыка '{skill_name}': {e}"}


# ══════════════════════════════════════════════════════════════════════════════
# METRICS & SELF-IMPROVEMENT
# ══════════════════════════════════════════════════════════════════════════════

def metrics_summary() -> dict:
    """
    Сводная статистика метрик: задачи, оценки, токены, стоимость.

    Returns:
        {"ok": True, "total": int, "avg_score": float, "total_cost_usd": float, ...}
    """
    try:
        from freepalp.core.self_improvement.metrics import Evaluator
        collector = Evaluator()
        stats     = collector.get_stats_summary()
        return {"ok": True, **stats}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
# Реестр инструментов
# ══════════════════════════════════════════════════════════════════════════════

SYSTEM_TOOLS: dict = {
    # Memory
    "memory_read": {
        "description": "Прочитать HOT память, corrections или статистику памяти агента",
        "fn":    memory_read,
        "async": False,
        "args":  {"tier": "hot|corrections|stats"},
    },
    "memory_write": {
        "description": "Записать заметку в HOT память агента (append или replace)",
        "fn":    memory_write,
        "async": False,
        "args":  {"content": "str", "mode": "append|replace"},
    },
    "memory_search": {
        "description": "Поиск по архиву памяти (COLD tier) по ключевым словам",
        "fn":    memory_search,
        "async": False,
        "args":  {"query": "str", "max_results": "int"},
    },
    "memory_forget": {
        "description": "Удалить из HOT памяти строки содержащие ключевое слово",
        "fn":    memory_forget,
        "async": False,
        "args":  {"keyword": "str"},
    },
    # Cron
    "cron_list": {
        "description": "Список всех cron-задач с временем следующего запуска",
        "fn":    cron_list,
        "async": False,
        "args":  {},
    },
    "cron_add": {
        "description": "Добавить периодическую задачу (интервал: 30m, 2h, 1d, 1w)",
        "fn":    cron_add,
        "async": False,
        "args":  {"name": "str", "interval": "str (30m|2h|1d|1w)", "command": "str"},
    },
    "cron_remove": {
        "description": "Удалить cron-задачу по ID",
        "fn":    cron_remove,
        "async": False,
        "args":  {"task_id": "str"},
    },
    # Providers & Models
    "providers_list": {
        "description": "Статус LLM-провайдеров: какие активны, какие нужно настроить",
        "fn":    providers_list,
        "async": False,
        "args":  {},
    },
    "models_list": {
        "description": "Список доступных LLM-моделей с провайдерами",
        "fn":    models_list,
        "async": False,
        "args":  {},
    },
    # MCP
    "mcp_list": {
        "description": "Список MCP-серверов: настроенных и готовых к подключению",
        "fn":    mcp_list,
        "async": False,
        "args":  {},
    },
    # Skills
    "skills_list": {
        "description": "Список доступных навыков FreePalp (встроенных и пользовательских)",
        "fn":    skills_list,
        "async": False,
        "args":  {},
    },
    "skill_run": {
        "description": "Запустить конкретный навык по имени с входным текстом",
        "fn":    skill_run,
        "async": False,
        "args":  {"skill_name": "str", "text": "str"},
    },
    # Metrics
    "metrics_summary": {
        "description": "Статистика: сколько задач, средний балл критика, стоимость токенов",
        "fn":    metrics_summary,
        "async": False,
        "args":  {},
    },
}
