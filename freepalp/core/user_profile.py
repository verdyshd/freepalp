"""
User Profile — USER.md кросс-сессионная память пользователя.
Вдохновлено: QClaw qclaw-rules Rule #3 "User Information Auto-Memory".

Хранит: имя, email, предпочтения, рабочий контекст.
Файл: freepalp/memory/USER.md

Отличие от HOT memory:
  HOT memory    = паттерны поведения агента
  USER.md       = информация о конкретном пользователе
"""

from __future__ import annotations
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

USER_MD = Path(__file__).parent.parent / "memory" / "USER.md"

# Шаблон USER.md (как в QClaw)
USER_MD_TEMPLATE = """# USER.md — About You

_Автоматически обновляется FreePalp при обнаружении информации о пользователе._

## Основная информация

- **Имя:** —
- **Как обращаться:** —
- **Часовой пояс:** —
- **Язык:** Russian

## Контакты

<!-- email, телефон -->

## Предпочтения разработки

- **Основной язык:** Python
- **ОС:** Windows
- **IDE:** —

## Рабочий контекст

- **Текущий проект:** Octopus AI Orchestrator (FreePalp)
- **Роль:** —

## Заметки

<!-- Другая важная информация -->
"""

# Паттерны для автоопределения информации (как в QClaw)
DETECTION_PATTERNS = [
    ("email",    re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")),
    ("name",     re.compile(r"(?:меня зовут|я -|my name is|call me)\s+([а-яёА-ЯЁa-zA-Z][а-яёА-ЯЁa-zA-Z\s]{1,20})", re.I)),
    ("lang",     re.compile(r"(?:язык|language|speak|говорю на)\s+([а-яёА-ЯЁa-zA-Z]+)", re.I)),
    ("project",  re.compile(r"(?:проект|project)\s+([а-яёА-ЯЁa-zA-Z0-9_\-\s]{2,30})", re.I)),
]


class UserProfile:
    """
    Читает и обновляет USER.md.
    Автоматически определяет информацию о пользователе из диалога.
    """

    def __init__(self):
        if not USER_MD.exists():
            USER_MD.parent.mkdir(parents=True, exist_ok=True)
            USER_MD.write_text(USER_MD_TEMPLATE, encoding="utf-8")

    def load(self) -> str:
        """Загружает USER.md для включения в промпт агента."""
        return USER_MD.read_text(encoding="utf-8")

    def get_context_for_prompt(self) -> str:
        """Краткий контекст пользователя для системного промпта."""
        content = self.load()
        lines = []
        for line in content.splitlines():
            # Берём только заполненные поля
            if line.startswith("- **") and "—" not in line and line.count(":") >= 1:
                lines.append(line.strip())
        if not lines:
            return ""
        return "Информация о пользователе:\n" + "\n".join(lines[:8])

    def scan_and_update(self, user_message: str) -> list[str]:
        """
        Сканирует сообщение пользователя на наличие личных данных.
        Автоматически обновляет USER.md.
        Возвращает список найденных и сохранённых полей.
        """
        updated = []
        content = self.load()

        for field, pattern in DETECTION_PATTERNS:
            m = pattern.search(user_message)
            if not m:
                continue
            value = m.group(1).strip() if m.lastindex else m.group(0).strip()
            if not value or len(value) < 2:
                continue

            # Проверить что ещё не сохранено
            if value in content:
                continue

            if field == "email":
                content = self._update_field(content, "## Контакты", f"- **Email:** {value}")
                updated.append(f"email: {value}")
            elif field == "name":
                content = self._replace_line(content, "- **Имя:**", f"- **Имя:** {value}")
                updated.append(f"name: {value}")
            elif field == "lang":
                content = self._replace_line(content, "- **Язык:**", f"- **Язык:** {value}")
                updated.append(f"language: {value}")
            elif field == "project":
                content = self._replace_line(
                    content, "- **Текущий проект:**",
                    f"- **Текущий проект:** {value}"
                )
                updated.append(f"project: {value}")

        if updated:
            USER_MD.write_text(content, encoding="utf-8")

        return updated

    def set_field(self, field: str, value: str):
        """Явно устанавливает поле в USER.md."""
        content = self.load()
        content = self._replace_line(content, f"- **{field}:**", f"- **{field}:** {value}")
        USER_MD.write_text(content, encoding="utf-8")

    # ─────────────────────────────────────────────

    def _replace_line(self, content: str, prefix: str, new_line: str) -> str:
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.strip().startswith(prefix):
                lines[i] = new_line
                return "\n".join(lines)
        return content + f"\n{new_line}"

    def _update_field(self, content: str, section: str, new_line: str) -> str:
        """Добавляет строку под секцией."""
        lines = content.splitlines()
        for i, line in enumerate(lines):
            if line.strip() == section:
                lines.insert(i + 1, new_line)
                return "\n".join(lines)
        return content + f"\n{new_line}"
