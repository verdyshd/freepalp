"""
Навык: data_analysis
Анализ данных: pandas, SQL, визуализация, статистика.

Помогает с: анализом датасетов, написанием запросов, построением графиков,
интерпретацией результатов, выявлением трендов и аномалий.
"""

from typing import Optional


async def run_data_analysis(
    text: str,
    analysis_type: str = "explore",
    output: str = "code",
    **kwargs,
) -> dict:
    """
    Помощник по анализу данных.

    Args:
        text:          описание данных, задача или вопрос
        analysis_type: "explore" | "transform" | "visualize" | "sql" | "stats" | "ml"
        output:        "code" (сгенерировать код) | "explanation" (объяснить) | "both"

    Returns:
        {"ok": True, "enhanced_prompt": str}
    """
    type_guide = {
        "explore":   "Исследовательский анализ (EDA): shape, dtypes, describe, null values, distributions, correlations.",
        "transform": "Трансформация данных: очистка, заполнение NaN, нормализация, feature engineering.",
        "visualize": "Визуализация: matplotlib/seaborn/plotly графики с правильными лейблами и цветами.",
        "sql":       "SQL запросы: оптимизированные, с JOIN/GROUP BY/CTE, объяснение плана выполнения.",
        "stats":     "Статистический анализ: гипотезы, p-value, доверительные интервалы, ANOVA.",
        "ml":        "Machine Learning: выбор модели, preprocessing pipeline, cross-validation, метрики.",
    }
    output_guide = {
        "code":        "Предоставь готовый Python код с pandas/numpy/sklearn.",
        "explanation": "Объясни результаты и инсайты понятным языком.",
        "both":        "Сначала Python код, затем объяснение результатов.",
    }

    enhanced = (
        f"Задача по анализу данных.\n\n"
        f"Тип анализа: {type_guide.get(analysis_type, type_guide['explore'])}\n"
        f"Формат ответа: {output_guide.get(output, output_guide['code'])}\n\n"
        f"Используй best practices:\n"
        f"- pandas для табличных данных\n"
        f"- numpy для вычислений\n"
        f"- seaborn/matplotlib для графиков\n"
        f"- type hints и docstrings\n\n"
        f"Задача:\n{text}"
    )

    return {
        "ok":              True,
        "enhanced_prompt": enhanced,
        "analysis_type":   analysis_type,
        "output":          output,
        "skill":           "data_analysis",
    }


TOOL_SPEC = {
    "data_analysis": {
        "description": "Анализ данных: pandas, SQL, визуализация, статистика, ML",
        "fn":    run_data_analysis,
        "async": True,
        "args":  {"text": "str", "analysis_type": "explore|transform|visualize|sql|stats|ml", "output": "code|explanation|both"},
    }
}
