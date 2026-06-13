"""
FreePalp Memory Consolidation — психологическая модель памяти человека.

Уровни памяти (по долговечности):
  Кратковременная → HOT score 1–4   (новые факты, детали сессии)
  Среднесрочная   → HOT score 5–7   (подтверждённые паттерны, предпочтения)
  Долгосрочная    → HOT score 8–10  (закреплённые знания, постоянная память)
  Угасающая       → WARM             (score упал < 3 → вытеснение из HOT)
  Архив           → COLD             (WARM > 30 дней без обращения)

Алгоритм консолидации (аналог «сна» мозга):
  1. Читаем последние 7 сессий — извлекаем ключевые факты и темы
  2. Считаем score каждой HOT-записи:
       - Упомянуто в недавних сессиях → подкрепление +2
       - Не упомянуто → угасание ×0.85/день (кривая Эббингауза)
  3. score ≥ 8 → постоянная (никогда не вытесняется)
     score 3–7 → рабочая HOT память
     score < 3 → WARM (вытеснение)
  4. WARM-записи без обращения 30+ дней → COLD

Формат HOT-записи:
  - [context] текст [★7.2] [2026-05-23]
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from .memory_manager import (
    HOT_FILE, WARM_DIR, ARCHIVE_DIR,
    SESSIONS_DIR, HOT_MAX_LINES,
)

# ────────────────────────────────────────────────────────────────────────────
# Константы
# ────────────────────────────────────────────────────────────────────────────

DECAY_RATE          = 0.85   # множитель score за каждый день без подкрепления
REINFORCE_BONUS     = 2.0    # прибавка score при повторном упоминании
PERMANENT_THRESHOLD = 8.0    # score ≥ этого → запись постоянная
DEMOTE_THRESHOLD    = 3.0    # score < этого → вытеснение в WARM
INITIAL_SCORE       = 4.0    # начальный score новой записи
SESSION_WINDOW_DAYS = 7      # сколько дней сессий анализируем
WARM_EXPIRE_DAYS    = 30     # WARM → COLD после этого числа дней

# Теги типов памяти (только для отображения в /memory)
MEMORY_LABEL = {
    (8.0, 10.1): "★★★ постоянная",
    (5.0,  8.0): "★★  среднесрочная",
    (3.0,  5.0): "★   кратковременная",
    (0.0,  3.0): "☆   угасает",
}


def _get_label(score: float) -> str:
    for (lo, hi), label in MEMORY_LABEL.items():
        if lo <= score < hi:
            return label
    return "☆"


# ────────────────────────────────────────────────────────────────────────────
# Парсинг / сериализация HOT-записей со score
# ────────────────────────────────────────────────────────────────────────────

_SCORE_RE  = re.compile(r'\[★([\d.]+)\]')
_DATE_RE   = re.compile(r'\[(\d{4}-\d{2}-\d{2})\]$')


def parse_hot_line(line: str) -> dict:
    """
    Парсит строку HOT-памяти.
    Возвращает dict: text, score, date, raw, is_entry
    """
    is_entry = line.startswith("- ")

    score_m = _SCORE_RE.search(line)
    date_m  = _DATE_RE.search(line)

    score = float(score_m.group(1)) if score_m else None
    date  = date_m.group(1) if date_m else None

    # Чистый текст (без метаданных score/date)
    clean = line
    if score_m:
        clean = clean[:score_m.start()].rstrip()
    if date_m and not score_m:
        clean = clean[:date_m.start()].rstrip()

    return {
        "raw":      line,
        "text":     clean,
        "score":    score,
        "date":     date,
        "is_entry": is_entry,
    }


def serialize_hot_line(text: str, score: float, today: str) -> str:
    """Сериализует запись с метаданными."""
    # Убираем старые метаданные если есть
    clean = _SCORE_RE.sub("", text).rstrip()
    clean = _DATE_RE.sub("", clean).rstrip()
    return f"{clean} [★{score:.1f}] [{today}]"


# ────────────────────────────────────────────────────────────────────────────
# Извлечение фактов из сессий
# ────────────────────────────────────────────────────────────────────────────

def _extract_session_keywords(days: int = SESSION_WINDOW_DAYS) -> set[str]:
    """
    Читает последние N дней JSONL-сессий.
    Возвращает множество ключевых слов/фраз из сообщений пользователя.
    Используется для подкрепления HOT-записей.
    """
    if not SESSIONS_DIR.exists():
        return set()

    cutoff = datetime.now() - timedelta(days=days)
    keywords: set[str] = set()

    for f in sorted(SESSIONS_DIR.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            if datetime.fromtimestamp(f.stat().st_mtime) < cutoff:
                continue
            for raw in f.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue

                # Извлекаем текст из сообщений
                text = ""
                if rec.get("type") == "message":
                    msg = rec.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict):
                                text += c.get("text", "") + " "
                    elif isinstance(content, str):
                        text = content

                # Извлекаем значимые слова (длина > 3, не служебные)
                STOPWORDS = {
                    "это", "как", "что", "для", "или", "при", "все",
                    "его", "она", "они", "есть", "was", "the", "and",
                    "for", "not", "you", "are", "but", "with", "this",
                    "that", "from", "have", "has", "can", "будет",
                    "нет", "уже", "если", "чем", "когда", "который",
                }
                words = re.findall(r'[а-яёА-ЯЁa-zA-Z][а-яёА-ЯЁa-zA-Z0-9_\-]{3,}', text)
                for w in words:
                    wl = w.lower()
                    if wl not in STOPWORDS and len(wl) > 3:
                        keywords.add(wl)
        except Exception:
            continue

    return keywords


# ────────────────────────────────────────────────────────────────────────────
# Главный движок консолидации
# ────────────────────────────────────────────────────────────────────────────

class ConsolidationEngine:
    """
    Психологически-обоснованная консолидация памяти.

    Запускается ежедневно из Orchestrator._cron_memory_cleanup().
    """

    def __init__(self):
        self.today = datetime.now().strftime("%Y-%m-%d")
        self.now   = datetime.now()
        self.report: dict = {
            "reinforced": 0,
            "decayed":    0,
            "promoted":   0,
            "demoted":    0,
            "new_facts":  0,
        }

    def run(self) -> dict:
        """
        Полный цикл консолидации.
        Возвращает отчёт об изменениях.
        """
        if not HOT_FILE.exists():
            return self.report

        # 1. Читаем ключевые слова из недавних сессий
        session_kw = _extract_session_keywords()

        # 2. Читаем HOT-память
        raw_lines = HOT_FILE.read_text(encoding="utf-8").splitlines()
        parsed    = [parse_hot_line(l) for l in raw_lines]

        # 3. Обновляем score каждой записи
        updated_lines = []
        demote_lines  = []

        for p in parsed:
            if not p["is_entry"]:
                # Заголовки, секции, пустые — оставляем как есть
                updated_lines.append(p["text"])
                continue

            # Инициализируем score если нет
            score = p["score"] if p["score"] is not None else INITIAL_SCORE
            entry_date = p["date"] or self.today

            # Считаем дней с последнего обращения
            try:
                last = datetime.strptime(entry_date, "%Y-%m-%d")
                days_since = max(0, (self.now - last).days)
            except ValueError:
                days_since = 0

            # Проверяем подкрепление: есть ли ключевые слова записи в сессиях?
            entry_text_lower = p["text"].lower()
            words_in_entry = set(
                w for w in re.findall(r'[а-яёА-ЯЁa-zA-Z]{4,}', entry_text_lower)
            )
            reinforced = bool(words_in_entry & session_kw)

            if reinforced:
                # Подкрепление — запись "вспоминалась" в недавних сессиях
                new_score = min(10.0, score + REINFORCE_BONUS)
                self.report["reinforced"] += 1
                entry_date = self.today
            else:
                # Угасание по кривой Эббингауза: score × 0.85 ^ days
                if score < PERMANENT_THRESHOLD and days_since > 0:
                    decay = DECAY_RATE ** days_since
                    new_score = round(score * decay, 2)
                    if new_score < score - 0.1:
                        self.report["decayed"] += 1
                else:
                    new_score = score  # постоянные не угасают

            new_score = round(max(0.0, min(10.0, new_score)), 1)

            # Вытеснение: score < порога → WARM
            if new_score < DEMOTE_THRESHOLD and score >= PERMANENT_THRESHOLD - 0.1:
                pass  # Постоянные никогда не вытесняем (double-check)
            elif new_score < DEMOTE_THRESHOLD:
                demote_lines.append(serialize_hot_line(p["text"], new_score, self.today))
                self.report["demoted"] += 1
                continue

            updated_lines.append(
                serialize_hot_line(p["text"], new_score, self.today)
            )

        # 4. Записываем обновлённый HOT (без вытесненных)
        self._write_hot(updated_lines)

        # 5. Вытесненные → WARM
        if demote_lines:
            self._write_to_warm(demote_lines)

        # 6. WARM → COLD (старые записи)
        self._expire_warm_to_cold()

        return self.report

    # ────────────────────────────────────────────────────────────────────────
    # Публичные методы для внешнего вызова
    # ────────────────────────────────────────────────────────────────────────

    def add_fact(self, context: str, fact: str, score: float = INITIAL_SCORE) -> bool:
        """
        Добавляет новый факт в HOT память с заданным score.
        Если похожий факт уже есть — подкрепляет его.
        Возвращает True если добавлен как новый.
        """
        if not HOT_FILE.exists():
            return False

        lines = HOT_FILE.read_text(encoding="utf-8").splitlines()
        fact_lower = fact.lower()[:50]

        # Проверяем дубли
        for i, line in enumerate(lines):
            if fact_lower in line.lower():
                # Подкрепляем существующий
                p = parse_hot_line(line)
                existing_score = p["score"] or INITIAL_SCORE
                new_score = min(10.0, existing_score + REINFORCE_BONUS)
                lines[i] = serialize_hot_line(p["text"], new_score, self.today)
                HOT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
                return False  # не новый

        # Новый факт
        entry = f"- [{context}] {fact}"
        new_line = serialize_hot_line(entry, score, self.today)
        with open(HOT_FILE, "a", encoding="utf-8") as f:
            f.write(new_line + "\n")
        self.report["new_facts"] += 1
        return True

    def reinforce(self, keywords: list[str]) -> int:
        """
        Подкрепляет HOT-записи, содержащие любое из keywords.
        Вызывается при каждом запросе (из Orchestrator).
        Возвращает кол-во подкреплённых записей.
        """
        if not HOT_FILE.exists() or not keywords:
            return 0

        lines = HOT_FILE.read_text(encoding="utf-8").splitlines()
        count = 0
        kw_lower = [k.lower() for k in keywords if len(k) > 3]

        for i, line in enumerate(lines):
            p = parse_hot_line(line)
            if not p["is_entry"]:
                continue
            line_lower = p["text"].lower()
            if any(kw in line_lower for kw in kw_lower):
                score = p["score"] or INITIAL_SCORE
                # Маленький бонус при каждом обращении
                new_score = min(10.0, score + 0.3)
                lines[i] = serialize_hot_line(p["text"], new_score, self.today)
                count += 1

        if count:
            HOT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return count

    def get_memory_stats(self) -> dict:
        """Статистика по уровням памяти."""
        if not HOT_FILE.exists():
            return {}

        counts = {"permanent": 0, "long_term": 0, "short_term": 0, "fading": 0}
        scores = []

        for line in HOT_FILE.read_text(encoding="utf-8").splitlines():
            p = parse_hot_line(line)
            if not p["is_entry"]:
                continue
            s = p["score"] or INITIAL_SCORE
            scores.append(s)
            if s >= PERMANENT_THRESHOLD:
                counts["permanent"] += 1
            elif s >= 5.0:
                counts["long_term"] += 1
            elif s >= DEMOTE_THRESHOLD:
                counts["short_term"] += 1
            else:
                counts["fading"] += 1

        return {
            **counts,
            "total":     sum(counts.values()),
            "avg_score": round(sum(scores) / len(scores), 2) if scores else 0,
            "max_score": round(max(scores), 1) if scores else 0,
        }

    # ────────────────────────────────────────────────────────────────────────
    # Вспомогательные методы
    # ────────────────────────────────────────────────────────────────────────

    def _write_hot(self, lines: list[str]) -> None:
        """Записывает обновлённые строки в HOT-файл (сохраняет лимит)."""
        # Разделяем заголовки и записи
        headers = [l for l in lines if not l.startswith("- ") or not _SCORE_RE.search(l)]
        entries = [l for l in lines if l.startswith("- ") and _SCORE_RE.search(l)]

        # Сортируем записи по score убыванию (важное — вверху)
        def _sort_key(line: str) -> float:
            m = _SCORE_RE.search(line)
            return -float(m.group(1)) if m else 0.0

        entries.sort(key=_sort_key)

        # Применяем лимит HOT (100 строк), но постоянные никогда не режем
        permanent = [l for l in entries if _get_score(l) >= PERMANENT_THRESHOLD]
        rest      = [l for l in entries if _get_score(l) < PERMANENT_THRESHOLD]

        max_rest = HOT_MAX_LINES - len(headers) - len(permanent)
        if max_rest < 0:
            max_rest = 0

        final_entries = permanent + rest[:max_rest]

        # Вытесняем лишние в WARM
        overflow = rest[max_rest:]
        if overflow:
            self._write_to_warm(overflow)
            self.report["demoted"] += len(overflow)

        content = "\n".join(headers + [""] + final_entries).strip() + "\n"
        HOT_FILE.write_text(content, encoding="utf-8")

    def _write_to_warm(self, lines: list[str]) -> None:
        """Вытесняет записи в WARM (среднесрочная память)."""
        WARM_DIR.mkdir(parents=True, exist_ok=True)
        month = self.now.strftime("%Y-%m")
        warm_file = WARM_DIR / f"demoted_{month}.md"
        with open(warm_file, "a", encoding="utf-8") as f:
            f.write(f"\n# Demoted {self.today}\n")
            f.write("\n".join(lines) + "\n")

    def _expire_warm_to_cold(self) -> int:
        """
        WARM-записи старше WARM_EXPIRE_DAYS → COLD.
        Возвращает кол-во архивированных записей.
        """
        if not WARM_DIR.exists():
            return 0

        cutoff  = self.now - timedelta(days=WARM_EXPIRE_DAYS)
        expired = 0
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        for f in list(WARM_DIR.glob("demoted_*.md")):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    month = mtime.strftime("%Y-%m")
                    arch  = ARCHIVE_DIR / f"warm_expired_{month}.md"
                    with open(arch, "a", encoding="utf-8") as af:
                        af.write(f"\n{'='*50}\n# Archived from WARM {self.today}\n")
                        af.write(f.read_text(encoding="utf-8"))
                    f.unlink()
                    expired += 1
            except Exception:
                pass

        return expired


def _get_score(line: str) -> float:
    m = _SCORE_RE.search(line)
    return float(m.group(1)) if m else 0.0


# ────────────────────────────────────────────────────────────────────────────
# Удобная точка входа
# ────────────────────────────────────────────────────────────────────────────

def run_consolidation() -> dict:
    """Запускает полный цикл консолидации. Вызывается из cron."""
    engine = ConsolidationEngine()
    return engine.run()


def reinforce_hot(keywords: list[str]) -> int:
    """Быстрое подкрепление HOT при каждом запросе."""
    engine = ConsolidationEngine()
    return engine.reinforce(keywords)


def add_to_long_term(context: str, fact: str, permanent: bool = False) -> bool:
    """
    Добавляет факт в долгосрочную память.
    permanent=True → score 9 (никогда не вытесняется).
    """
    score = 9.0 if permanent else INITIAL_SCORE
    engine = ConsolidationEngine()
    return engine.add_fact(context, fact, score)
