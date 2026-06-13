"""
SkillLibrary — полный teacher→skill (дистилляция приёмов в SKILL.md).

Идея (из Odysseus, главный дифференциатор FreePalp): когда дешёвая модель
проваливает задачу, а ретрай («учитель» — часто более сильная модель из
фолбэка) её решает, мы дистиллируем рабочую процедуру в файл SKILL.md
(формат, совместимый с Claude Code). В следующий раз релевантный навык
инжектится в промпт воркера ДО первой попытки — и дешёвая модель справляется
сразу. Коррекция НАКАПЛИВАЕТСЯ, а не сгорает с ответом.

Дистилляция детерминированная (без доп. LLM-вызова): надёжно и не жжёт квоту.
Сохраняем только то, что реально прошло (вызывается лишь при score ≥ порога и
пустых детерминированных проверках — гейт уже в оркестраторе).
"""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from typing import Optional

_SKILLS_DIR = Path(__file__).parent.parent / "memory" / "skills"

# Слова-пустышки, не несущие смысла для матчинга навыка по запросу
_STOP = {
    "и", "в", "на", "с", "по", "для", "что", "как", "это", "мне", "ты", "the",
    "a", "an", "to", "of", "in", "on", "for", "me", "my", "please", "пожалуйста",
    "сделай", "сделать", "напиши", "написать", "создай", "создать", "нужно",
    "надо", "можешь", "хочу",
}


def _tokens(text: str) -> list[str]:
    """Значимые слова запроса (рус+англ), без пустышек и коротышей."""
    raw = re.findall(r"[a-zA-Zа-яёА-ЯЁ0-9_]+", (text or "").lower())
    return [w for w in raw if len(w) >= 3 and w not in _STOP]


def _slug(task_type: str, user_input: str) -> str:
    toks = _tokens(user_input)[:4]
    base = task_type + ("_" + "_".join(toks) if toks else "")
    # ascii-safe: транслитерация не нужна — оставляем латиницу/цифры, остальное → _
    s = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
    return (s or task_type)[:60]


def _tool_names(tools_used: list) -> list[str]:
    return [t.get("tool", "") for t in (tools_used or []) if t.get("tool")]


def _tool_seq_display(tools_used: list) -> str:
    parts = []
    for t in tools_used or []:
        name = t.get("tool", "")
        path = (t.get("path") or "")
        if path:
            short = path.replace("\\", "/").rsplit("/", 1)[-1]
            parts.append(f"{name}({short})")
        else:
            parts.append(name)
    return " → ".join(parts) or "ответ текстом, без инструментов"


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


class SkillLibrary:
    def __init__(self, skills_dir: Path = _SKILLS_DIR):
        self.dir = skills_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    # ── Чтение/парсинг ────────────────────────────────────────────────
    def _parse(self, path: Path) -> Optional[dict]:
        try:
            text = path.read_text(encoding="utf-8")
        except Exception:
            return None
        meta: dict = {"_path": path, "body": text}
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.DOTALL)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            meta["body"] = m.group(2)
        return meta

    def all_skills(self) -> list[dict]:
        return [s for p in sorted(self.dir.glob("*.md"))
                if (s := self._parse(p))]

    # ── Сохранение приёма ─────────────────────────────────────────────
    def save_skill(self, task_type: str, user_input: str, tools_used: list,
                   model_name: str, overcame_issue: str = "") -> Optional[Path]:
        """Дистиллирует успешный-после-провала приём в SKILL.md.
        Возвращает путь файла (новый или обновлённый дублёр)."""
        try:
            names = _tool_names(tools_used)
            seq_set = set(names)

            # Дедуп: тот же тип + похожая последовательность инструментов →
            # не плодим дубль, а повышаем счётчик применений у существующего.
            for sk in self.all_skills():
                if sk.get("task_type") != task_type:
                    continue
                prev = set((sk.get("tools", "") or "").split(","))
                prev.discard("")
                if _jaccard(seq_set, prev) >= 0.7 or (not seq_set and not prev):
                    return self._bump_uses(sk)

            slug = _slug(task_type, user_input)
            path = self.dir / f"{slug}.md"
            if path.exists():  # коллизия слага — добавим хвост времени
                path = self.dir / f"{slug}_{int(time.time())%100000}.md"

            kw = " ".join(_tokens(user_input)[:8])
            seq_disp = _tool_seq_display(tools_used)
            title = (user_input.strip().split("\n")[0][:70] or task_type)
            overcame = (overcame_issue or "").strip().split("\n")[0][:200]

            steps = "\n".join(
                f"{i+1}. {n}" for i, n in enumerate(names)
            ) or "1. Дай прямой ответ нужного формата (инструменты не требуются)."

            content = (
                f"---\n"
                f"name: {slug}\n"
                f"description: приём для задач типа «{task_type}» по запросам вроде «{title}»\n"
                f"task_type: {task_type}\n"
                f"keywords: {kw}\n"
                f"tools: {','.join(names)}\n"
                f"source_model: {model_name}\n"
                f"created: {date.today().isoformat()}\n"
                f"uses: 1\n"
                f"---\n\n"
                f"# Приём: {title}\n\n"
                f"## Когда применять\n"
                f"Запрос относится к типу «{task_type}» и похож на: «{title}».\n\n"
                f"## Что мешало раньше\n"
                f"{overcame or 'Дешёвая модель решала не с первой попытки.'}\n\n"
                f"## Рабочая процедура\n"
                f"{steps}\n\n"
                f"## Последовательность инструментов\n"
                f"`{seq_disp}`\n\n"
                f"_Дистиллировано из успешного решения моделью {model_name} "
                f"после провала. Применяй сразу — не повторяй прошлую ошибку._\n"
            )
            path.write_text(content, encoding="utf-8")
            return path
        except Exception:
            return None

    def _bump_uses(self, sk: dict) -> Optional[Path]:
        try:
            path = sk["_path"]
            uses = int(sk.get("uses", "1") or "1") + 1
            text = path.read_text(encoding="utf-8")
            text = re.sub(r"(?m)^uses:\s*\d+\s*$", f"uses: {uses}", text)
            path.write_text(text, encoding="utf-8")
            return path
        except Exception:
            return None

    # ── Подбор релевантных навыков для инжекта в промпт ───────────────
    def find_relevant(self, task_type: str, user_input: str, limit: int = 2) -> str:
        """Возвращает компактный блок процедур для вставки в промпт воркера.
        Матчинг: совпадение task_type (сильный сигнал) + пересечение ключевых
        слов запроса с keywords навыка."""
        skills = self.all_skills()
        if not skills:
            return ""
        qtok = set(_tokens(user_input))

        scored = []
        for sk in skills:
            score = 0.0
            if sk.get("task_type") == task_type:
                score += 2.0
            sk_kw = set((sk.get("keywords", "") or "").split())
            score += 3.0 * _jaccard(qtok, sk_kw)
            score += 0.1 * int(sk.get("uses", "1") or "1")  # популярные чуть выше
            if score >= 2.0:  # либо тип совпал, либо сильное пересечение слов
                scored.append((score, sk))

        if not scored:
            return ""
        scored.sort(key=lambda x: -x[0])

        blocks = []
        for _, sk in scored[:limit]:
            title = (sk.get("name") or "приём")
            seq = ""
            mseq = re.search(r"## Последовательность инструментов\n`([^`]*)`", sk.get("body", ""))
            if mseq:
                seq = mseq.group(1)
            mwhen = re.search(r"## Что мешало раньше\n(.+?)\n", sk.get("body", ""))
            warn = mwhen.group(1).strip() if mwhen else ""
            blocks.append(
                f"• {title}: последовательность {seq}"
                + (f" (раньше мешало: {warn})" if warn else "")
            )
        return ("Накопленные приёмы для похожих задач (применяй сразу):\n"
                + "\n".join(blocks))


# Singleton
_LIB: Optional[SkillLibrary] = None


def get() -> SkillLibrary:
    global _LIB
    if _LIB is None:
        _LIB = SkillLibrary()
    return _LIB
