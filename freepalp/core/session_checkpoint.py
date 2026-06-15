"""
Чекпойнт диалога (идея из MiMo Code: чекпойнты по % окна, layered rebuild).

Проблема: воркер берёт только последние N сообщений — длинные диалоги теряют
ранний контекст. Решение: всё, что старше «окна последних N», сжимается в
компактный чекпойнт-рекап и инжектится перед свежими сообщениями. Так промпт
остаётся ограниченным при любой длине диалога («бесконечная» сессия), но гист
ранних обменов сохраняется.

СЛОЁНАЯ пересборка (layered rebuild): обмены ближе к окну (свежие-старые)
сохраняются подробнее, древние — короче, самые древние при нехватке места
сворачиваются в счётчик. Так под фиксированным потолком символов выживает
максимум сигнала — приоритет недавнему, а не первому обмену сессии.

Детерминированно (без LLM-вызова) — не жжёт квоту и без задержки, в духе
«меньше веры в LLM, больше детерминированной обвязки».
"""
from __future__ import annotations

KEEP_RECENT = 6          # сколько последних сообщений идут дословно (окно)
_TRIGGER_AT = KEEP_RECENT + 2   # чекпойнт включается, когда сообщений больше
_MAX_CHARS = 1600        # потолок размера чекпойнта (защита окна промпта)

# Слои сжатия по свежести обмена (символов на сообщение в обмене):
_RECENT_OLD = 5          # столько ближайших к окну старых обменов — подробный слой
_MID_OLD = 6             # следующий слой — средний
_LAYER_RECENT = 150      # подробный слой
_LAYER_MID = 85          # средний слой
_LAYER_OLD = 45          # древний слой


def _txt(content) -> str:
    """Контент сообщения → плоский текст (бывает список частей)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                        if isinstance(c, dict) and c.get("type") == "text")
    return str(content or "")


def _exchanges(older: list) -> list:
    """Группирует старые сообщения в обмены: ('ex', спросил, ответ) или ('a', текст)."""
    out = []
    i = 0
    while i < len(older):
        m = older[i]
        if m.get("role", "user") == "user":
            u = _txt(m.get("content"))
            a = ""
            if i + 1 < len(older) and older[i + 1].get("role") == "assistant":
                a = _txt(older[i + 1].get("content"))
                i += 2
            else:
                i += 1
            out.append(("ex", u, a))
        else:
            out.append(("a", _txt(m.get("content")), ""))
            i += 1
    return out


def _per_msg_budget(pos_from_end: int) -> int:
    """Бюджет символов на сообщение по свежести (0 = самый свежий старый обмен)."""
    if pos_from_end < _RECENT_OLD:
        return _LAYER_RECENT
    if pos_from_end < _RECENT_OLD + _MID_OLD:
        return _LAYER_MID
    return _LAYER_OLD


def build(history: list, keep_recent: int = KEEP_RECENT) -> str:
    """Компактный СЛОЁНЫЙ чекпойнт по сообщениям старше окна последних N.
    Пустую строку — если диалог короткий (чекпойнт не нужен)."""
    if not history or len(history) <= _TRIGGER_AT:
        return ""
    older = history[:-keep_recent] if keep_recent else history
    if not older:
        return ""

    ex = _exchanges(older)
    n = len(ex)
    header = ("[Чекпойнт диалога — гист ранних обменов, чтобы не терять контекст "
              f"в длинной сессии; всего обменов ранее: {n}]")
    body_budget = _MAX_CHARS - len(header) - 60   # запас под заголовок/свёртку

    # Идём от самого свежего старого к самому древнему: копим строки, пока есть
    # бюджет. Что не влезло (самое древнее) — свернём в счётчик.
    picked: list[str] = []
    used = 0
    collapsed = 0
    for idx in range(n - 1, -1, -1):
        kind, u, a = ex[idx]
        per = _per_msg_budget(n - 1 - idx)
        u1 = u[:per].replace("\n", " ").strip()
        if kind == "ex":
            a1 = a[:per].replace("\n", " ").strip()
            line = f"• спросил: {u1}" + (f" → ответ: {a1}" if a1 else "")
        else:
            line = f"• ранее: {u1}" if u1 else ""
        if not line:
            continue
        if used + len(line) + 1 > body_budget:
            collapsed = idx + 1   # всё, что осталось левее (древнее), — опускаем
            break
        picked.append(line)
        used += len(line) + 1

    picked.reverse()   # обратно в хронологический порядок
    out_lines: list[str] = []
    if collapsed:
        out_lines.append(f"• …(+{collapsed} ещё более ранних обменов опущено)")
    out_lines.extend(picked)
    return header + "\n" + "\n".join(out_lines)
