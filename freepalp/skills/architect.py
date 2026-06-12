"""
Навык: architect
Проектирование систем: архитектура, API дизайн, схемы БД, DevOps.

Помогает выбрать правильные паттерны, инструменты и структуры.
Генерирует диаграммы (Mermaid), схемы API (OpenAPI), ER-диаграммы.
"""

from typing import Optional


async def run_architect(
    text: str,
    domain: str = "backend",
    output_format: str = "description",
    **kwargs,
) -> dict:
    """
    Архитектурный советник.

    Args:
        text:          описание системы или задача
        domain:        "backend" | "frontend" | "database" | "api" | "devops" | "microservices"
        output_format: "description" | "mermaid" | "openapi" | "checklist"

    Returns:
        {"ok": True, "enhanced_prompt": str}
    """
    domain_guide = {
        "backend":      "Backend архитектура: сервисы, очереди, кеширование, масштабирование.",
        "frontend":     "Frontend архитектура: компонентная структура, state management, роутинг, SSR/CSR.",
        "database":     "Схема базы данных: таблицы, индексы, связи, нормализация, партиционирование.",
        "api":          "API дизайн: REST/GraphQL endpoints, аутентификация, версионирование, rate limiting.",
        "devops":       "DevOps: CI/CD pipeline, Docker/K8s, мониторинг, алерты, IaC (Terraform).",
        "microservices":"Микросервисная архитектура: сервисы, API Gateway, Service Discovery, Circuit Breaker.",
    }
    format_guide = {
        "description": "Опиши архитектуру словами с обоснованием выбора компонентов.",
        "mermaid":     "Создай диаграмму в формате Mermaid (graph LR или sequenceDiagram).",
        "openapi":     "Сгенерируй OpenAPI 3.0 спецификацию в YAML.",
        "checklist":   "Создай архитектурный чеклист: что нужно сделать и проверить.",
    }

    enhanced = (
        f"Ты опытный Software Architect. Спроектируй решение.\n\n"
        f"Область: {domain_guide.get(domain, domain_guide['backend'])}\n"
        f"Формат: {format_guide.get(output_format, format_guide['description'])}\n\n"
        f"Учитывай:\n"
        f"- Масштабируемость и производительность\n"
        f"- Отказоустойчивость и мониторинг\n"
        f"- Безопасность и авторизация\n"
        f"- Простоту разработки и поддержки\n\n"
        f"Задача:\n{text}"
    )

    return {
        "ok":              True,
        "enhanced_prompt": enhanced,
        "domain":          domain,
        "output_format":   output_format,
        "skill":           "architect",
    }


TOOL_SPEC = {
    "architect": {
        "description": "Проектирование систем: архитектура, API, БД, DevOps, диаграммы Mermaid",
        "fn":    run_architect,
        "async": True,
        "args":  {"text": "str", "domain": "backend|frontend|database|api|devops|microservices", "output_format": "description|mermaid|openapi|checklist"},
    }
}
