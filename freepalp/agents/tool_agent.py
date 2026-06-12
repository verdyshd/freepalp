"""
Tool Agent — предоставляет агентам безопасный доступ к инструментам.
Работает как прокси: LLM запрашивает tool call → ToolAgent исполняет.
"""

import json
import asyncio
from ..tools.file_tools import FILE_TOOLS
from ..tools.shell_tools import SHELL_TOOLS
from ..tools.web_tools import WEB_TOOLS
from ..tools.browser_tools import BROWSER_TOOLS
from ..tools.github_tools import GITHUB_TOOLS
from ..tools.notification_tools import NOTIFICATION_TOOLS
from ..tools.system_tools import SYSTEM_TOOLS
from ..core.models import AgentMessage

# Объединённый реестр всех инструментов
ALL_TOOLS: dict = {
    **FILE_TOOLS,
    **SHELL_TOOLS,
    **WEB_TOOLS,
    **BROWSER_TOOLS,
    **GITHUB_TOOLS,
    **NOTIFICATION_TOOLS,
    **SYSTEM_TOOLS,
}

TOOL_DESCRIPTIONS = "\n".join([
    f"  {name}: {info['description']}"
    for name, info in ALL_TOOLS.items()
])


class ToolAgent:
    """
    Выполняет вызовы инструментов по запросу Worker/Orchestrator.
    """

    # Инструменты, для которых retry не нужен (побочные эффекты)
    _NO_RETRY_TOOLS = frozenset({"write_file", "write_source", "delete_file",
                                  "memory_write", "memory_forget", "cron_add",
                                  "cron_remove", "send_notification"})

    async def execute(self, tool_name: str, _max_retries: int = 2, **kwargs) -> dict:
        """
        Выполняет инструмент по имени. Повторяет до _max_retries раз при сетевых ошибках
        с экспоненциальной задержкой (1s → 2s → 4s). Побочно-эффектные инструменты не повторяются.
        """
        if tool_name not in ALL_TOOLS:
            return {
                "ok": False,
                "error": f"Инструмент '{tool_name}' не найден. "
                         f"Доступны: {', '.join(sorted(ALL_TOOLS.keys()))}",
            }

        tool = ALL_TOOLS[tool_name]
        fn   = tool["fn"]
        retries = 0 if tool_name in self._NO_RETRY_TOOLS else _max_retries

        last_err = None
        for attempt in range(retries + 1):
            try:
                if tool.get("async"):
                    result = await fn(**kwargs)
                else:
                    result = fn(**kwargs)
                return result if isinstance(result, dict) else {"ok": True, "result": result}
            except TypeError as e:
                # Неверные аргументы — retry бессмысленен
                return {"ok": False, "error": f"Неверные аргументы для '{tool_name}': {e}"}
            except (TimeoutError, ConnectionError, OSError) as e:
                last_err = e
                if attempt < retries:
                    delay = 2 ** attempt          # 1s, 2s
                    await asyncio.sleep(delay)
            except Exception as e:
                return {"ok": False, "error": str(e)}

        return {"ok": False, "error": f"Инструмент '{tool_name}' недоступен после {retries+1} попыток: {last_err}"}

    async def execute_from_json(self, tool_call_json: str) -> dict:
        """
        Парсит JSON tool call от LLM и выполняет.
        Формат: {"tool": "read_file", "args": {"path": "main.py"}}
        """
        try:
            call = json.loads(tool_call_json)
            tool_name = call.get("tool") or call.get("name")
            args = call.get("args") or call.get("arguments") or {}
            if not tool_name:
                return {"ok": False, "error": "Не указан 'tool' в вызове"}
            return await self.execute(tool_name, **args)
        except json.JSONDecodeError as e:
            return {"ok": False, "error": f"Некорректный JSON: {e}"}

    def get_tools_description(self) -> str:
        """Возвращает описание всех инструментов для системного промпта."""
        return f"Доступные инструменты:\n{TOOL_DESCRIPTIONS}"

    def list_tools(self) -> list[str]:
        return list(ALL_TOOLS.keys())
