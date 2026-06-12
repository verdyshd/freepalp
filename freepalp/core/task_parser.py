"""
Task Parser — анализирует пользовательский ввод и определяет тип задачи,
сложность и параметры для роутинга.

Ключевые слова и маркеры сложности загружаются из prompt_loader (prompts.json),
что позволяет системе обновлять их без изменения кода.
"""

import re
from typing import Tuple, List
from .models import TaskRequest, TaskType
from . import prompt_loader


def _get_keywords() -> dict[TaskType, List[str]]:
    """Загружает keywords из prompts.json и маппит строковые ключи на TaskType."""
    raw = prompt_loader.get_task_keywords()
    result = {}
    for key, kws in raw.items():
        try:
            result[TaskType(key)] = kws
        except ValueError:
            pass
    return result


def _get_complexity_markers() -> List[str]:
    return prompt_loader.get_complexity_markers()


def detect_task_type(text: str) -> TaskType:
    """Определяет тип задачи по ключевым словам из prompts.json."""
    text_lower = text.lower()
    task_keywords = _get_keywords()
    complexity_markers = _get_complexity_markers()

    scores: dict[TaskType, int] = {t: 0 for t in TaskType}

    for task_type, keywords in task_keywords.items():
        for kw in keywords:
            if kw in text_lower:
                scores[task_type] += 1

    # Найти тип с максимальным счётом
    best_type = max(scores, key=lambda t: scores[t])
    if scores[best_type] == 0:
        return TaskType.GENERAL

    # Проверка сложности — апгрейд CODING_SMALL → CODING_LARGE
    if best_type == TaskType.CODING_SMALL:
        complexity_count = sum(1 for m in complexity_markers if m in text_lower)
        if complexity_count >= 2:
            return TaskType.CODING_LARGE

    return best_type


def estimate_complexity(text: str) -> int:
    """Оценивает сложность задачи от 1 до 5."""
    score = 1
    text_lower = text.lower()
    complexity_markers = _get_complexity_markers()

    # Длина текста
    word_count = len(text.split())
    if word_count > 50:
        score += 1
    if word_count > 100:
        score += 1

    # Маркеры сложности
    complexity_count = sum(1 for m in complexity_markers if m in text_lower)
    if complexity_count >= 1:
        score += 1
    if complexity_count >= 3:
        score += 1

    return min(score, 5)


def extract_file_references(text: str) -> List[str]:
    """Извлекает упомянутые файлы из текста."""
    # Паттерны: path/to/file.ext или просто file.ext
    pattern = r'[\w/\\.-]+\.\w{1,6}'
    matches = re.findall(pattern, text)
    # Фильтр — только реальные расширения файлов
    valid_exts = {
        '.py', '.js', '.ts', '.json', '.yaml', '.yml',
        '.txt', '.md', '.csv', '.html', '.css', '.sh',
        '.env', '.toml', '.cfg', '.ini'
    }
    return [m for m in matches if any(m.endswith(ext) for ext in valid_exts)]


def parse_task(user_input: str) -> TaskRequest:
    """
    Главная функция: превращает сырой ввод в структурированный TaskRequest.
    """
    task_type = detect_task_type(user_input)
    complexity = estimate_complexity(user_input)
    files = extract_file_references(user_input)

    request = TaskRequest(
        user_input=user_input,
        task_type=task_type,
        context={
            "complexity": complexity,
            "word_count": len(user_input.split()),
        },
        files=files,
    )
    return request
