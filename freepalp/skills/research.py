"""
Навык: research
Глубокое исследование темы: поиск, синтез, структурирование.

Ищет актуальную информацию, фильтрует источники, составляет резюме.
Автоматически вызывает web_search и browser_extract при необходимости.
"""

from typing import Optional


async def run_research(
    text: str,
    depth: str = "standard",
    format: str = "report",
    **kwargs,
) -> dict:
    """
    Исследование темы.

    Args:
        text:   тема или вопрос для исследования
        depth:  "quick" (быстрый обзор) | "standard" | "deep" (глубокое)
        format: "report" (отчёт) | "summary" (резюме) | "bullets" (пункты) | "comparison" (сравнение)

    Returns:
        {"ok": True, "enhanced_prompt": str, "search_queries": list}
    """
    depth_map = {
        "quick":    "Дай краткий обзор (3-5 предложений) по теме:",
        "standard": "Исследуй тему подробно. Включи: определение, ключевые аспекты, примеры, текущее состояние.",
        "deep":     "Проведи глубокое исследование. Включи: история, текущее состояние, ключевые игроки, технические детали, тренды, критику.",
    }
    format_map = {
        "report":     "Оформи как структурированный отчёт с заголовками.",
        "summary":    "Оформи как краткое резюме на 1-2 абзаца.",
        "bullets":    "Оформи как маркированный список ключевых фактов.",
        "comparison": "Оформи как сравнительную таблицу вариантов.",
    }

    prefix   = depth_map.get(depth, depth_map["standard"])
    fmt      = format_map.get(format, format_map["report"])

    # Генерируем поисковые запросы которые Worker может использовать через web_search
    search_queries = [
        text,
        f"{text} 2024 2025",
        f"{text} tutorial guide",
        f"{text} best practices",
    ]

    enhanced = (
        f"{prefix}\n\nТема: {text}\n\n{fmt}\n\n"
        f"Для актуальных данных используй инструмент web_search с запросами:\n"
        + "\n".join(f"  - {q}" for q in search_queries[:3])
    )

    return {
        "ok":              True,
        "enhanced_prompt": enhanced,
        "search_queries":  search_queries,
        "depth":           depth,
        "format":          format,
        "skill":           "research",
    }


TOOL_SPEC = {
    "research": {
        "description": "Глубокое исследование темы с поиском актуальной информации",
        "fn":    run_research,
        "async": True,
        "args":  {"text": "str", "depth": "quick|standard|deep", "format": "report|summary|bullets|comparison"},
    }
}
