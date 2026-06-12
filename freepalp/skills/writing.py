"""
Навык: writing
Профессиональный помощник по текстам: статьи, письма, документация.

Поддерживает: технические тексты, маркетинг, деловая переписка, документация API.
"""

from typing import Optional


async def run_writing(
    text: str,
    style: str = "professional",
    doc_type: str = "article",
    language: str = "ru",
    **kwargs,
) -> dict:
    """
    Помощник по написанию текстов.

    Args:
        text:     тема, черновик или инструкция
        style:    "professional" | "casual" | "technical" | "marketing" | "academic"
        doc_type: "article" | "email" | "readme" | "docs" | "summary" | "social"
        language: "ru" (русский) | "en" (английский)

    Returns:
        {"ok": True, "enhanced_prompt": str}
    """
    style_guide = {
        "professional": "Деловой стиль: чёткий, конкретный, без воды. Структурированный.",
        "casual":       "Дружелюбный тон: живой язык, как будто объясняешь другу.",
        "technical":    "Технический стиль: точные термины, примеры, ссылки на стандарты.",
        "marketing":    "Маркетинговый тон: убедительный, выделяем выгоды, призыв к действию.",
        "academic":     "Академический стиль: нейтральный, со ссылками, точные формулировки.",
    }
    type_guide = {
        "article":  "Статья с заголовком H1, вступлением, основной частью (H2/H3), выводом.",
        "email":    "Email с темой, приветствием, телом письма, подписью.",
        "readme":   "README.md с разделами: Описание, Быстрый старт, Использование, Примеры, Лицензия.",
        "docs":     "Техническая документация с: обзором, API reference, примерами, FAQ.",
        "summary":  "Краткое резюме: ключевые тезисы, главные выводы, следующие шаги.",
        "social":   "Пост для соцсетей: цепляющий старт, основная мысль, хэштеги.",
    }
    lang_note = "Пиши на русском языке." if language == "ru" else "Write in English."

    enhanced = (
        f"Напиши {type_guide.get(doc_type, 'текст')}.\n"
        f"Стиль: {style_guide.get(style, style_guide['professional'])}\n"
        f"{lang_note}\n\n"
        f"Тема/содержание:\n{text}"
    )

    return {
        "ok":              True,
        "enhanced_prompt": enhanced,
        "style":           style,
        "doc_type":        doc_type,
        "language":        language,
        "skill":           "writing",
    }


TOOL_SPEC = {
    "writing": {
        "description": "Профессиональный помощник по текстам: статьи, email, README, документация",
        "fn":    run_writing,
        "async": True,
        "args":  {"text": "str", "style": "professional|casual|technical|marketing", "doc_type": "article|email|readme|docs|summary"},
    }
}
