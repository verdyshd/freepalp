"""
Shell Tools — БЕЗОПАСНОЕ выполнение shell команд.
Whitelist подход: только разрешённые команды.
"""

import asyncio
import re as _re_shell
import shlex
from pathlib import Path

# Разрешённые команды (whitelist)
ALLOWED_COMMANDS = {
    "python", "python3", "pip", "pip3",
    "node", "npm", "npx",
    "git",
    "ls", "dir", "cat", "type",
    "mkdir", "echo",
    "pytest", "mypy", "black", "ruff",
}

# Запрещённые паттерны в аргументах
BLOCKED_PATTERNS = [
    "rm -rf", "del /f", "format",
    "> /dev/", ">/dev/",
    "curl | sh", "wget | sh",
    "eval(", "exec(",
    "/etc/passwd", "/etc/shadow",
    "sudo", "su -",
]

# Рабочая директория для команд — sandbox
SANDBOX_ROOT = Path(__file__).parent.parent / "sandbox"


async def run_command(command: str, timeout: int = 30) -> dict:
    """
    Выполняет команду в sandbox с ограничениями.
    Возвращает stdout, stderr, returncode.
    """
    # Проверка безопасности
    safety_check = _check_safety(command)
    if not safety_check["ok"]:
        return safety_check

    try:
        # Убедиться что sandbox существует
        SANDBOX_ROOT.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SANDBOX_ROOT),
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            proc.kill()
            return {
                "ok": False,
                "error": f"Таймаут {timeout}с. Команда прервана.",
                "returncode": -1,
            }

        return {
            "ok": proc.returncode == 0,
            "stdout": stdout.decode("utf-8", errors="replace"),
            "stderr": stderr.decode("utf-8", errors="replace"),
            "returncode": proc.returncode,
        }

    except Exception as e:
        return {"ok": False, "error": str(e), "returncode": -1}


def _check_safety(command: str) -> dict:
    """Проверяет команду на безопасность."""
    cmd_lower = command.lower().strip()

    # Проверка заблокированных паттернов
    for pattern in BLOCKED_PATTERNS:
        if pattern.lower() in cmd_lower:
            return {
                "ok": False,
                "error": f"Запрещённая команда: содержит '{pattern}'",
            }

    # Запрет shell-метасимволов: whitelist проверяет только первый токен,
    # цепочки (cmd1 ; cmd2, cmd1 && evil, ... | sh, $(...), `...`) обходили бы её.
    for meta in [";", "&&", "||", "|", "`", "$(", ">", "<", "&"]:
        if meta in command:
            return {
                "ok": False,
                "error": f"Запрещён символ '{meta}' в команде (защита от обхода whitelist).",
            }

    # Получить базовую команду
    try:
        parts = shlex.split(command)
    except ValueError:
        return {"ok": False, "error": "Некорректный синтаксис команды"}

    if not parts:
        return {"ok": False, "error": "Пустая команда"}

    base_cmd = Path(parts[0]).name.lower()
    # Убрать .exe для Windows
    if base_cmd.endswith(".exe"):
        base_cmd = base_cmd[:-4]

    if base_cmd not in ALLOWED_COMMANDS:
        return {
            "ok": False,
            "error": f"Команда '{base_cmd}' не в whitelist. "
                     f"Разрешены: {', '.join(sorted(ALLOWED_COMMANDS))}",
        }

    # python <файл>: исполняемый файл обязан лежать в песочнице. Иначе агент
    # обходит whitelist: write_file скрипта куда угодно -> python script.py.
    # ВАЖНО: парсим СЫРУЮ команду (она уходит в shell как есть; shlex в posix-режиме
    # съедает бэкслеши Windows и маскирует ..\evil.py).
    if base_cmd in ("python", "python3"):
        if _re_shell.search(r"(^|\s)-c(\s|$)", command):
            return {"ok": False,
                    "error": "python -c запрещён (обход whitelist). Запиши код "
                             "в файл в sandbox через write_file и запусти его."}
        for raw in _re_shell.findall(r"[\w.\\/:\-]+\.py\b", command):
            norm = raw.replace("\\", "/")
            if norm.startswith("./"):
                norm = norm[2:]
            if ".." in norm.split("/") or norm.startswith("/") or ":" in norm:
                return {"ok": False,
                        "error": f"Запуск python-файла вне песочницы запрещён: {raw}"}

    return {"ok": True}


def get_allowed_commands() -> list[str]:
    """Список разрешённых команд."""
    return sorted(ALLOWED_COMMANDS)


# Реестр инструментов
SHELL_TOOLS = {
    "run_command": {
        "fn": run_command,
        "description": "Выполняет shell команду в sandbox. Аргументы: command (str), timeout (int, default=30)",
        "args": ["command"],
        "async": True,
    },
    "get_allowed_commands": {
        "fn": get_allowed_commands,
        "description": "Список разрешённых команд",
        "args": [],
    },
}
