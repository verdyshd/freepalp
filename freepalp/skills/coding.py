"""
Навык: coding
Экспертный помощник по написанию, отладке и ревью кода.

Специализируется на: Python, JS/TS, Rust, Go, SQL.
Всегда добавляет type hints, docstrings, обработку ошибок.
Предлагает тесты и примеры использования.
"""

from typing import Optional


async def run_coding(
    text: str,
    language: str = "python",
    mode: str = "write",
    **kwargs,
) -> dict:
    """
    Помощник по коду.

    Args:
        text:     задача или код для обработки
        language: язык программирования (python, js, rust, go, sql...)
        mode:     write (написать) | review (проверить) | debug (отладить) | explain (объяснить)

    Returns:
        {"ok": True, "result": str, "language": str}
    """
    # Навык усиливает системный промпт Worker — сам вызов идёт через Orchestrator
    # Здесь формируем специализированный контекст для задачи
    mode_prompts = {
        "write":   f"Напиши качественный {language} код для следующей задачи. Добавь type hints, docstring, обработку ошибок и пример использования.",
        "review":  f"Выполни code review следующего {language} кода. Найди: баги, проблемы безопасности, нарушения best practices. Предложи улучшения.",
        "debug":   f"Отладь следующий {language} код. Найди причину ошибки и исправь её. Объясни что было не так.",
        "explain": f"Объясни следующий {language} код подробно: что делает каждая часть, паттерны, алгоритмы.",
    }
    prefix = mode_prompts.get(mode, mode_prompts["write"])
    enhanced = f"{prefix}\n\n{text}"
    return {
        "ok":       True,
        "result":   enhanced,
        "language": language,
        "mode":     mode,
        "skill":    "coding",
        "hint":     "Используй этот текст как улучшенный промпт для Worker агента",
    }


TOOL_SPEC = {
    "coding": {
        "description": "Экспертный помощник по коду: написание, ревью, отладка, объяснение",
        "fn":    run_coding,
        "async": True,
        "args":  {"text": "str", "language": "python|js|rust|go|sql", "mode": "write|review|debug|explain"},
    }
}
