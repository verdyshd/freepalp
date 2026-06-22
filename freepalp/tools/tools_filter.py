"""
tools_filter.py

Utility for selecting a subset of FreePalp tools that are relevant for a
particular *task_type*. Reduces prompt size by injecting only needed tools.

Author:  FreePalp Worker (generated) + Claude integration
"""

from __future__ import annotations
from typing import Dict, Mapping, Set


# Mapping from task-type → набор категорий инструментов
_TASK_TYPE_TO_CATEGORIES: Mapping[str, Set[str]] = {
    "coding_small":  {"file_tools", "shell_tools", "memory_tools"},
    "coding_large":  {"file_tools", "shell_tools", "memory_tools"},
    "review":        {"file_tools", "memory_tools", "shell_tools"},
    "architecture":  {"file_tools", "memory_tools"},
    "text":          {"memory_tools", "file_tools"},
    # general — +web_tools: агент должен мочь искать/фетчить, когда это нужно
    "general":       {"memory_tools", "file_tools", "web_tools"},
    # search/research — полный веб: поиск, fetch И интерактивный браузер
    "search":        {"web_tools", "browser_tools", "memory_tools", "file_tools"},
    "research":      {"web_tools", "browser_tools", "memory_tools", "file_tools"},
    "web":           {"web_tools", "browser_tools", "memory_tools", "file_tools"},
    "file_ops":      {"file_tools", "memory_tools"},
    "shell":         {"shell_tools", "file_tools"},
}

# Mapping от конкретного инструмента → его категория
_TOOL_TO_CATEGORY: Mapping[str, str] = {
    # file-tools (sandbox)
    "read_file":    "file_tools",
    "write_file":   "file_tools",
    "list_files":   "file_tools",
    "delete_file":  "file_tools",
    "create_dir":   "file_tools",
    # source-tools (self-modification)
    "read_source":  "file_tools",
    "list_source":  "file_tools",
    "write_source": "file_tools",
    # shell-tools
    "run_command":          "shell_tools",
    "get_allowed_commands": "shell_tools",
    # web-tools
    "web_search":    "web_tools",
    "fetch_page":    "web_tools",
    # browser-tools
    "browser_open":       "browser_tools",
    "browser_screenshot": "browser_tools",
    "browser_click":      "browser_tools",
    "browser_fill":       "browser_tools",
    "browser_extract":    "browser_tools",
    "browser_eval":       "browser_tools",
    # memory-tools
    "memory_read":   "memory_tools",
    "memory_write":  "memory_tools",
    "memory_search": "memory_tools",
    "memory_forget": "memory_tools",
    # github-tools
    "github_search_code":   "github_tools",
    "github_create_issue":  "github_tools",
    "github_list_repos":    "github_tools",
    # notification-tools
    "send_notification": "notification_tools",
    # system-tools
    "get_system_info":   "system_tools",
    "get_python_info":   "system_tools",
}


def filter_tools(all_tools: Dict[str, dict], task_type: str) -> Dict[str, dict]:
    """
    Возвращает подмножество ``all_tools``, оставляя только инструменты,
    релевантные для указанного ``task_type``.

    Параметры
    ----------
    all_tools : dict
        Полный словарь всех доступных инструментов.
    task_type : str
        Тип задачи (coding_small, review, text, general, ...).

    Возвращаемое значение
    ----------------------
    dict
        Отфильтрованный словарь инструментов.

    Пример
    -------
    >>> tools = {"read_file": {}, "run_command": {}, "memory_read": {}}
    >>> filter_tools(tools, "text")
    {'memory_read': {}}
    """
    if not isinstance(all_tools, dict):
        raise TypeError("all_tools must be a dict")
    task_type_norm = (task_type or "general").strip().lower()
    needed = _TASK_TYPE_TO_CATEGORIES.get(task_type_norm, {"memory_tools"})

    return {
        name: meta
        for name, meta in all_tools.items()
        if _TOOL_TO_CATEGORY.get(name, "unknown") in needed
    }
