"""
Чекпойнт диалога (идея из MiMo Code: чекпойнты по % окна, layered rebuild).

Проблема: воркер берёт только последние 6 сообщений — длинные диалоги теряют
ранний контекст. Решение: всё, что старше «окна последних N», сжимается в
компактный чекпойнт-рекап и инжектится перед свежими сообщениями. Так промпт
остаётся ограниченным при любой длине диалога («бесконечная» сессия), но гист
ранних обменов сохраняется.

Детерминированно (без LLM-вызова) — не жжёт квоту и без задержки, в духе
«меньше веры в LLM, больше детерминированной обвязки».
"""
from __future__ import annotations

KEEP_RECENT = 6          # сколько последних сообщений идут дословно
_TRIGGER_AT = KEEP_RECENT + 2   # чекпойнт включается, когда сообщений больше
_MAX_CHARS = 1400        # потолок размера чекпойнта (защита окна)
_PER_MSG = 110           # сколько символов берём от каждого сообщения


def _txt(content) -> str:
    """Контент сообщения → плоский текст (бывает список частей)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return str(content or "")


def build(history: list, keep_recent: int = KEEP_RECENT) -> str:
    """Возвращает компактный чекпойнт по сообщениям старше окна последних N.
    Пустую строку — если диалог короткий (чекпойнт не нужен)."""
    if not history or len(history) <= _TRIGGER_AT:
        return ""
    older = history[:-keep_recent] if keep_recent else history
    if not older:
        return ""

    lines: list[str] = []
    i = 0
    while i < len(older):
        m = older[i]
        role = m.get("role", "user")
        if role == "user":
            u = _txt(m.get("content"))[:_PER_MSG].replace("\n", " ").strip()
            a = ""
            if i + 1 < len(older) and older[i + 1].get("role") == "assistant":
                a = _txt(older[i + 1].get("content"))[:_PER_MSG].replace("\n", " ").strip()
                i += 2
            else:
                i += 1
            lines.append(f"• спросил: {u}" + (f" → ответ: {a}" if a else ""))
        else:
            # одиночный assistant без пары
            a = _txt(m.get("content"))[:_PER_MSG].replace("\n", " ").strip()
            if a:
                lines.append(f"• ранее: {a}")
            i += 1

    recap = ("[Чекпойнт диалога — гист ранних обменов, чтобы не терять контекст "
             f"в длинной сессии; всего обменов ранее: {len(older)//2 or len(older)}]\n"
             + "\n".join(lines))
    if len(recap) > _MAX_CHARS:
        recap = recap[:_MAX_CHARS].rsplit("\n", 1)[0] + "\n• …(ещё ранее)"
    return recap
