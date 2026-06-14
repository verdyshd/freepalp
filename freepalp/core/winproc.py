"""
Хелпер: запуск subprocess БЕЗ мигающего консольного окна на Windows.

Проблема: когда FreePalp запущен без привязанной консоли (через pythonw,
ярлык, .bat, как фоновый процесс), каждый вызов git/python/MCP-сервера
открывает на мгновение чёрное окно cmd — выглядит пугающе для обычного
пользователя. CREATE_NO_WINDOW это подавляет; на *nix флаг не нужен.

Использование:
    subprocess.run([...], **no_window())
    subprocess.Popen([...], **no_window())
"""
import sys
import subprocess


def no_window() -> dict:
    """kwargs для subprocess, чтобы не мигало консольное окно (Windows)."""
    if sys.platform == "win32":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}
