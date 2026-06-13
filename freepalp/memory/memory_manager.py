"""
FreePalp Memory Manager — трёхуровневая система памяти агента.
Вдохновлено: QClaw self-improving skill (HOT/WARM/COLD).

Структура:
  memory/
  ├── hot_memory.md      ← HOT: всегда загружен, ≤100 строк
  ├── corrections.md     ← лог исправлений (последние 50)
  ├── sessions/          ← история сессий (WARM)
  ├── projects/          ← память по проектам (WARM)
  ├── patterns/          ← кандидаты на промоцию в HOT
  ├── warm/              ← WARM: демотированные из HOT
  └── archive/           ← COLD: старые corrections + сессии

Автоматика:
  - HOT  ≤ 100 строк: излишек → warm/demoted_YYYY-MM.md
  - corrections ≤ 50 записей: излишек → archive/corrections_YYYY-MM.md
  - Паттерн повторился 3x → промоция в HOT
  - Heartbeat раз в 24ч → maintenance() чистит и переклассифицирует
"""

from __future__ import annotations
import json
import re
from pathlib import Path
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional

MEMORY_ROOT    = Path(__file__).parent
HOT_FILE       = MEMORY_ROOT / "hot_memory.md"
CORRECTIONS    = MEMORY_ROOT / "corrections.md"
SESSIONS_DIR   = MEMORY_ROOT / "sessions"
PROJECTS_DIR   = MEMORY_ROOT / "projects"
PATTERNS_DIR   = MEMORY_ROOT / "patterns"
WARM_DIR       = MEMORY_ROOT / "warm"
ARCHIVE_DIR    = MEMORY_ROOT / "archive"
HEARTBEAT_FILE = MEMORY_ROOT / "last_heartbeat.json"
VECTOR_INDEX   = MEMORY_ROOT / "vector_index.json"

# ── 4-слойная модель памяти (вдохновлено agentmemory) ──
#   working    — сырые наблюдения текущей сессии (транзитивно)
#   episodic   — «что случилось»: события сессий, найденные баги, исправления
#   semantic   — «что я знаю»: факты, предпочтения, правила (HOT)
#   procedural — «как делать»: воркфлоу, уроки, паттерны решения
LAYER_WORKING    = "working"
LAYER_EPISODIC   = "episodic"
LAYER_SEMANTIC   = "semantic"
LAYER_PROCEDURAL = "procedural"

HOT_MAX_LINES        = 100
CORRECTIONS_MAX      = 50
CORRECTIONS_KEEP     = 30     # после обрезки оставляем столько
HEARTBEAT_INTERVAL_H = 24     # часов между maintenance()


class MemoryManager:
    """
    Управляет трёхуровневой памятью FreePalp.
    HOT  → всегда в контексте агента
    WARM → загружается по запросу / проекту
    COLD → архив (поиск через /history)
    """

    def __init__(self):
        self._ensure_dirs()
        self._vstore = None   # ленивая инициализация векторного индекса

    # ──────────────────────────────────────────────────────────────────
    # 4-слойная семантическая память (Working/Episodic/Semantic/Procedural)
    # ──────────────────────────────────────────────────────────────────

    @property
    def vstore(self):
        """Ленивый доступ к векторному индексу (создаётся при первом обращении)."""
        if self._vstore is None:
            try:
                from .vector_store import VectorStore
                import os
                # Gemini-эмбеддинги если есть ключ — иначе локальный TF-IDF
                use_gemini = bool(os.environ.get("GEMINI_API_KEY", ""))
                self._vstore = VectorStore(path=VECTOR_INDEX, use_gemini=use_gemini)
            except Exception as e:
                print(f"  [Memory] VectorStore init error: {e}")
                self._vstore = None
        return self._vstore

    def remember(self, text: str, layer: str = LAYER_SEMANTIC, meta: dict = None) -> str:
        """Добавляет запись в семантический индекс с указанием слоя.
        Возвращает id записи (или '' если индекс недоступен)."""
        vs = self.vstore
        if vs is None:
            return ""
        try:
            eid = vs.add(text, layer=layer, meta=meta or {})
            vs.save()
            return eid
        except Exception:
            return ""

    def recall(self, query: str, k: int = 5, layer: str = None) -> list[dict]:
        """Семантический поиск по памяти (по смыслу, устойчив к словоформам).
        layer=None → поиск по всем слоям; иначе по конкретному слою."""
        vs = self.vstore
        if vs is None:
            return []
        try:
            return vs.search(query, k=k, layer=layer)
        except Exception:
            return []

    def hybrid_search(self, query: str, max_results: int = 10) -> list[dict]:
        """Гибридный поиск: векторный (по смыслу) + keyword (search_cold).
        Объединяет результаты, векторные идут первыми с пометкой source='vector'."""
        results = []
        seen = set()

        # 1. Векторный поиск по всем слоям
        for r in self.recall(query, k=max_results):
            key = r["text"][:80]
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "snippet": r["text"][:200],
                "tier":    r["layer"],
                "score":   r["score"],
                "source":  f"vector/{r['layer']}",
            })

        # 2. Keyword поиск по архиву (дополняет)
        for r in self.search_cold(query, max_results=max_results):
            key = r["snippet"][:80]
            if key in seen:
                continue
            seen.add(key)
            results.append(r)

        return results[:max_results]

    # ──────────────────────────────────────────────────────────────────
    # HOT tier
    # ──────────────────────────────────────────────────────────────────

    def load_hot(self) -> str:
        """Загружает HOT память (всегда включается в промпт агента).
        Служебные теги [★N] [дата] и HTML-маркеры отфильтровываются — агент видит чистый текст.
        """
        import re as _re
        _meta_re = _re.compile(r'\s*\[★[\d.]+\]\s*\[\d{4}-\d{2}-\d{2}\]|\s*\[★[\d.]+\]|\s*<!--.*?-->', _re.DOTALL)

        if not HOT_FILE.exists():
            return ""
        lines = HOT_FILE.read_text(encoding="utf-8").splitlines()
        result = []
        for l in lines:
            if not l.strip():
                continue
            if l.strip().startswith("<!--"):
                continue
            # Убираем теги score и date
            clean = _meta_re.sub("", l).rstrip()
            if clean.strip():
                result.append(clean)
        return "\n".join(result[:HOT_MAX_LINES])

    def add_to_hot(self, context: str, note: str, permanent: bool = False):
        """Добавляет запись в HOT память через систему score.
        permanent=True → score 9 (долгосрочная, никогда не вытесняется).
        Если похожая запись уже есть — подкрепляет её (score +2).
        Дедупликация: не добавляет строки, схожие с уже существующими.
        """
        if not HOT_FILE.exists():
            self._init_hot_file()

        # Дедупликация — нормализуем и сравниваем первые 80 символов
        note_norm = note.lower().strip()
        key = note_norm[:80]
        existing_text = HOT_FILE.read_text(encoding="utf-8")
        for line in existing_text.splitlines():
            if key in line.lower():
                return  # уже есть — пропускаем

        try:
            from .consolidation import add_to_long_term
            add_to_long_term(context, note, permanent=permanent)
        except Exception:
            # Fallback: простая запись без score
            with open(HOT_FILE, "a", encoding="utf-8") as f:
                f.write(f"- [{context}] {note}\n")
        self._enforce_hot_limit()
        # Семантический слой: HOT-факты = «что я знаю»
        self.remember(f"{note}", layer=LAYER_SEMANTIC, meta={"context": context})

    def forget(self, keyword: str) -> int:
        """
        Удаляет строки из HOT памяти содержащие keyword.
        Возвращает кол-во удалённых строк. (/forget X)
        """
        if not HOT_FILE.exists():
            return 0
        lines = HOT_FILE.read_text(encoding="utf-8").splitlines()
        kw = keyword.lower()
        kept   = [l for l in lines if kw not in l.lower()]
        removed = len(lines) - len(kept)
        if removed:
            HOT_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")
        return removed

    # ──────────────────────────────────────────────────────────────────
    # Corrections (лог исправлений критика)
    # ──────────────────────────────────────────────────────────────────

    def log_correction(self, context: str, problem: str, lesson: str):
        """
        Логирует исправление (когда Critic нашёл проблему).
        После записи: обрезает до 50, старые → archive/
        Паттерн повторился 3x → автопромоция в HOT.
        Дубликат (тот же context+problem+lesson уже есть среди последних записей) — пропускаем.
        """
        for prev in self.get_recent_corrections(20):
            prev_context  = ""
            prev_problem  = ""
            prev_lesson   = ""
            for line in prev.splitlines():
                if line.startswith("CONTEXT:"):
                    prev_context = line.split(":", 1)[1].strip()
                elif line.startswith("PROBLEM:"):
                    prev_problem = line.split(":", 1)[1].strip()
                elif line.startswith("LESSON:"):
                    prev_lesson = line.split(":", 1)[1].strip()
            if (prev_context, prev_problem, prev_lesson) == (context, problem, lesson):
                return

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        entry = (
            f"\n---\n"
            f"DATE: {timestamp}\n"
            f"CONTEXT: {context}\n"
            f"PROBLEM: {problem}\n"
            f"LESSON: {lesson}\n"
        )
        with open(CORRECTIONS, "a", encoding="utf-8") as f:
            f.write(entry)

        self._trim_corrections()
        self._evaluate_for_hot(context, lesson)
        # Эпизодический слой: «что случилось» (событие + проблема)
        self.remember(f"{context}: {problem}", layer=LAYER_EPISODIC,
                      meta={"type": "correction"})
        # Процедурный слой: «как делать» (извлечённый урок)
        if lesson:
            self.remember(lesson, layer=LAYER_PROCEDURAL,
                          meta={"context": context})

    def get_recent_corrections(self, n: int = 10) -> list[str]:
        """Возвращает последние N исправлений."""
        if not CORRECTIONS.exists():
            return []
        content = CORRECTIONS.read_text(encoding="utf-8")
        entries = [e.strip() for e in content.split("---") if e.strip()]
        return entries[-n:]

    # ──────────────────────────────────────────────────────────────────
    # WARM tier — сессии и проекты
    # ──────────────────────────────────────────────────────────────────

    def save_session(self, session_id: str, summary: str, task_type: str):
        """Сохраняет краткое резюме сессии (WARM)."""
        SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
        f = SESSIONS_DIR / f"{session_id}.md"
        date = datetime.now().strftime("%Y-%m-%d %H:%M")
        f.write_text(
            f"# Session {session_id}\nDate: {date}\nType: {task_type}\n\n## Summary\n{summary}\n",
            encoding="utf-8"
        )
        # Эпизодический слой: резюме сессии = «что случилось»
        if summary:
            self.remember(summary, layer=LAYER_EPISODIC,
                          meta={"session": session_id, "task_type": task_type})

    def load_project_memory(self, project_name: str) -> str:
        """Загружает WARM память по проекту."""
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        f = PROJECTS_DIR / f"{project_name}.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def add_to_project(self, project_name: str, note: str):
        """Добавляет заметку к проекту (WARM)."""
        PROJECTS_DIR.mkdir(parents=True, exist_ok=True)
        f = PROJECTS_DIR / f"{project_name}.md"
        ts = datetime.now().strftime("%Y-%m-%d")
        with open(f, "a", encoding="utf-8") as fp:
            fp.write(f"\n[{ts}] {note}\n")

    def load_warm_demoted(self) -> str:
        """Загружает недавно демотированные HOT записи (WARM поиск)."""
        if not WARM_DIR.exists():
            return ""
        files = sorted(WARM_DIR.glob("demoted_*.md"), reverse=True)
        if not files:
            return ""
        return files[0].read_text(encoding="utf-8")

    # ──────────────────────────────────────────────────────────────────
    # COLD tier — поиск по архиву
    # ──────────────────────────────────────────────────────────────────

    def search_cold(self, query: str, max_results: int = 10) -> list[dict]:
        """
        Поиск по COLD архиву (archive/ + warm/).
        Возвращает список совпадений с указанием источника.
        """
        results = []
        query_lower = query.lower()
        words = [w for w in query_lower.split() if len(w) > 2]

        search_dirs = [
            (ARCHIVE_DIR, "cold"),
            (WARM_DIR,    "warm"),
        ]

        for search_dir, tier in search_dirs:
            if not search_dir.exists():
                continue
            for f in sorted(search_dir.glob("*.md"), reverse=True):
                try:
                    content = f.read_text(encoding="utf-8")
                    lines = content.splitlines()
                    for i, line in enumerate(lines):
                        line_lower = line.lower()
                        if not line.strip():
                            continue
                        score = sum(1 for w in words if w in line_lower)
                        if score == 0:
                            continue
                        # Контекст: ±1 строка
                        ctx_start = max(0, i - 1)
                        ctx_end   = min(len(lines), i + 2)
                        snippet   = " | ".join(l.strip() for l in lines[ctx_start:ctx_end] if l.strip())
                        results.append({
                            "file":    f.name,
                            "tier":    tier,
                            "line":    i + 1,
                            "score":   score,
                            "snippet": snippet[:200],
                            "source":  f"{tier}/{f.name}:{i+1}",
                        })
                except Exception:
                    pass

        results.sort(key=lambda x: -x["score"])
        return results[:max_results]

    def export_zip(self, output_path: Optional[str] = None) -> str:
        """
        Экспортирует всю память в ZIP архив.
        Возвращает путь к созданному файлу.
        """
        import zipfile
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = Path(output_path) if output_path else MEMORY_ROOT.parent / "sandbox" / f"memory_export_{ts}.zip"
        out.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in MEMORY_ROOT.rglob("*"):
                if f.is_file() and f.suffix in (".md", ".json", ".jsonl"):
                    zf.write(f, f.relative_to(MEMORY_ROOT.parent))

        return str(out)

    # ──────────────────────────────────────────────────────────────────
    # Heartbeat — авто-обслуживание раз в 24ч
    # ──────────────────────────────────────────────────────────────────

    def heartbeat(self):
        """
        Запускается при каждом старте Orchestrator.
        Если прошло > 24ч с последнего обслуживания — запускает maintenance().
        """
        now = datetime.now()
        if HEARTBEAT_FILE.exists():
            try:
                data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
                last = datetime.fromisoformat(data.get("last_run", "2000-01-01"))
                if (now - last).total_seconds() < HEARTBEAT_INTERVAL_H * 3600:
                    return  # ещё не время
            except Exception:
                pass

        self.maintenance()
        HEARTBEAT_FILE.write_text(
            json.dumps({"last_run": now.isoformat(), "version": "1.0"}, ensure_ascii=False),
            encoding="utf-8"
        )

    def touch_hot_entries(self, keywords: list[str]):
        """
        Помечает записи HOT как 'использованные сейчас'.
        Вызывается из Orchestrator когда HOT память попала в запрос.
        """
        tf = PATTERNS_DIR / "hot_access.json"
        PATTERNS_DIR.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        if tf.exists():
            try:
                data = json.loads(tf.read_text(encoding="utf-8"))
            except Exception:
                pass
        now = datetime.now().isoformat()
        for kw in keywords:
            key = kw[:60].lower()
            data[key] = now
        tf.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def maintenance(self) -> dict:
        """
        Полная консолидация памяти по модели человека:
        - Подкрепление HOT-записей из недавних сессий (кривая Эббингауза)
        - Угасание неиспользуемых записей (score × 0.85/день)
        - Вытеснение слабых записей (score < 3) → WARM
        - WARM > 30 дней без обращения → COLD
        - corrections: обрезать до 50, старые → archive/
        - sessions: старше 90 дней → archive/
        Возвращает отчёт о проделанной работе.
        """
        report = {
            "reinforced":           0,
            "decayed":              0,
            "promoted":             0,
            "demoted":              0,
            "corrections_archived": 0,
            "sessions_archived":    0,
        }

        # Консолидация памяти (человеческая модель)
        try:
            from .consolidation import run_consolidation
            cons_report = run_consolidation()
            report.update(cons_report)
        except Exception as e:
            print(f"  [Memory] Consolidation error: {e}")
            # Fallback на старый механизм
            report["demoted"] = self._demote_stale_hot(days=30)

        report["corrections_archived"] = self._trim_corrections()
        report["sessions_archived"]    = self._archive_old_sessions(days=90)

        return report

    async def shutdown(self):
        """Graceful shutdown — вызывается из Orchestrator.stop().
        Сохраняет timestamp последнего выключения и запускает лёгкую очистку если нужно.
        """
        try:
            now = datetime.now()
            # Обновляем heartbeat файл с timestamp выключения
            data: dict = {}
            if HEARTBEAT_FILE.exists():
                try:
                    data = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8"))
                except Exception:
                    pass
            data["last_shutdown"] = now.isoformat()
            HEARTBEAT_FILE.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except Exception:
            pass

    async def cleanup(self):
        """Принудительная очистка памяти — используется cron-задачей __memory_cleanup__."""
        self.maintenance()

    # ──────────────────────────────────────────────────────────────────
    # Stats
    # ──────────────────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        hot_lines   = len(HOT_FILE.read_text(encoding="utf-8").splitlines()) if HOT_FILE.exists() else 0
        corrections = len(self.get_recent_corrections(999))
        sessions    = len(list(SESSIONS_DIR.glob("*.md")))   if SESSIONS_DIR.exists()  else 0
        projects    = len(list(PROJECTS_DIR.glob("*.md")))   if PROJECTS_DIR.exists()  else 0
        archived    = len(list(ARCHIVE_DIR.glob("*.md")))    if ARCHIVE_DIR.exists()   else 0
        warm        = len(list(WARM_DIR.glob("*.md")))       if WARM_DIR.exists()      else 0

        last_hb = None
        if HEARTBEAT_FILE.exists():
            try:
                last_hb = json.loads(HEARTBEAT_FILE.read_text(encoding="utf-8")).get("last_run")
            except Exception:
                pass

        # Слои семантической памяти
        layers = {}
        vs = self.vstore
        if vs is not None:
            try:
                layers = {
                    "working":    vs.count(LAYER_WORKING),
                    "episodic":   vs.count(LAYER_EPISODIC),
                    "semantic":   vs.count(LAYER_SEMANTIC),
                    "procedural": vs.count(LAYER_PROCEDURAL),
                    "total":      vs.count(),
                }
            except Exception:
                pass

        return {
            "hot_lines":    hot_lines,
            "corrections":  corrections,
            "sessions":     sessions,
            "projects":     projects,
            "warm_files":   warm,
            "archived":     archived,
            "last_heartbeat": last_hb,
            "layers":       layers,
        }

    # ──────────────────────────────────────────────────────────────────
    # Внутренние методы
    # ──────────────────────────────────────────────────────────────────

    def _evaluate_for_hot(self, context: str, lesson: str):
        """Если паттерн повторился 3+ раз — промотировать в HOT."""
        PATTERNS_DIR.mkdir(parents=True, exist_ok=True)
        pf = PATTERNS_DIR / "candidate_patterns.json"
        patterns: dict = {}
        if pf.exists():
            try:
                patterns = json.loads(pf.read_text(encoding="utf-8"))
            except Exception:
                pass

        key = f"{context}:{lesson[:50]}"
        if key not in patterns:
            patterns[key] = {"lesson": lesson, "context": context, "count": 0}
        patterns[key]["count"] += 1

        if patterns[key]["count"] >= 3:
            self.add_to_hot(context, lesson)
            del patterns[key]

        pf.write_text(json.dumps(patterns, ensure_ascii=False, indent=2), encoding="utf-8")

    def _enforce_hot_limit(self) -> int:
        """
        Обрезает HOT до HOT_MAX_LINES.
        Лишние строки → warm/demoted_YYYY-MM.md
        Возвращает кол-во демотированных строк.
        """
        if not HOT_FILE.exists():
            return 0
        lines = HOT_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) <= HOT_MAX_LINES:
            return 0

        header  = [l for l in lines if l.startswith("#") or l.startswith("_")]
        entries = [l for l in lines if l.startswith("-")]
        other   = [l for l in lines if l not in header and l not in entries and l.strip()]

        if len(entries) + len(header) <= HOT_MAX_LINES:
            return 0

        keep_count = HOT_MAX_LINES - len(header) - len(other)
        demoted  = entries[:-keep_count] if keep_count > 0 else entries
        kept     = entries[-keep_count:] if keep_count > 0 else []

        # Сохраняем демотированные в WARM
        if demoted:
            WARM_DIR.mkdir(parents=True, exist_ok=True)
            month = datetime.now().strftime("%Y-%m")
            warm_file = WARM_DIR / f"demoted_{month}.md"
            with open(warm_file, "a", encoding="utf-8") as f:
                f.write(f"\n# Demoted {datetime.now():%Y-%m-%d %H:%M}\n")
                f.write("\n".join(demoted) + "\n")

        HOT_FILE.write_text(
            "\n".join(header + other + kept) + "\n",
            encoding="utf-8"
        )
        return len(demoted)

    def _trim_corrections(self) -> int:
        """
        Оставляет последние CORRECTIONS_KEEP записей.
        Старые → archive/corrections_YYYY-MM.md
        Возвращает кол-во архивированных.
        """
        if not CORRECTIONS.exists():
            return 0
        content = CORRECTIONS.read_text(encoding="utf-8")
        entries = [e.strip() for e in content.split("---") if e.strip()]

        if len(entries) <= CORRECTIONS_MAX:
            return 0

        old    = entries[:-CORRECTIONS_KEEP]
        kept   = entries[-CORRECTIONS_KEEP:]

        # Архивируем старые
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        month = datetime.now().strftime("%Y-%m")
        arch_file = ARCHIVE_DIR / f"corrections_{month}.md"
        with open(arch_file, "a", encoding="utf-8") as f:
            f.write(f"\n# Archived {datetime.now():%Y-%m-%d %H:%M}\n")
            f.write("\n---\n".join(old))

        CORRECTIONS.write_text(
            "# Corrections Log\n\n_Последние записи_\n\n---\n\n" +
            "\n\n---\n\n".join(kept),
            encoding="utf-8"
        )
        return len(old)

    def _demote_stale_hot(self, days: int = 30) -> int:
        """
        Записи HOT не задействованные N дней → warm/demoted_YYYY-MM.md
        Возвращает кол-во демотированных строк.
        """
        if not HOT_FILE.exists():
            return 0

        # Загружаем статистику доступа
        tf = PATTERNS_DIR / "hot_access.json"
        access_data: dict = {}
        if tf.exists():
            try:
                access_data = json.loads(tf.read_text(encoding="utf-8"))
            except Exception:
                pass

        cutoff = datetime.now() - timedelta(days=days)
        lines = HOT_FILE.read_text(encoding="utf-8").splitlines()
        kept, demoted = [], []

        for line in lines:
            if not line.startswith("-"):
                kept.append(line)
                continue
            # Ищем запись в access_data по первым 40 символам строки
            key = line[2:42].lower() if len(line) > 2 else ""
            last_access_str = None
            for ak, av in access_data.items():
                if ak in key or key in ak:
                    last_access_str = av
                    break

            if last_access_str:
                try:
                    last_access = datetime.fromisoformat(last_access_str)
                    if last_access < cutoff:
                        demoted.append(line)
                    else:
                        kept.append(line)
                    continue
                except Exception:
                    pass
            # Нет данных о доступе — оставляем
            kept.append(line)

        if demoted:
            WARM_DIR.mkdir(parents=True, exist_ok=True)
            month = datetime.now().strftime("%Y-%m")
            warm_file = WARM_DIR / f"demoted_{month}.md"
            with open(warm_file, "a", encoding="utf-8") as f:
                f.write(f"\n# Demoted (stale {days}d) {datetime.now():%Y-%m-%d}\n")
                f.write("\n".join(demoted) + "\n")
            HOT_FILE.write_text("\n".join(kept) + "\n", encoding="utf-8")

        return len(demoted)

    def _archive_old_sessions(self, days: int = 90) -> int:
        """Сессии старше N дней → archive/sessions_YYYY-MM.md"""
        if not SESSIONS_DIR.exists():
            return 0
        cutoff = datetime.now() - timedelta(days=days)
        archived = 0
        ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

        for f in list(SESSIONS_DIR.glob("*.md")):
            try:
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    month = mtime.strftime("%Y-%m")
                    arch = ARCHIVE_DIR / f"sessions_{month}.md"
                    with open(arch, "a", encoding="utf-8") as af:
                        af.write(f"\n{'='*40}\n")
                        af.write(f.read_text(encoding="utf-8"))
                    f.unlink()
                    archived += 1
            except Exception:
                pass
        return archived

    def _init_hot_file(self):
        HOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        HOT_FILE.write_text(
            "# FreePalp HOT Memory\n"
            "_Всегда загружен. Максимум 100 строк._\n\n"
            "## Правила агента\n\n"
            "## Предпочтения\n\n"
            "## Паттерны\n",
            encoding="utf-8"
        )

    def _ensure_dirs(self):
        for d in [SESSIONS_DIR, PROJECTS_DIR, PATTERNS_DIR, WARM_DIR, ARCHIVE_DIR]:
            d.mkdir(parents=True, exist_ok=True)
        if not HOT_FILE.exists():
            self._init_hot_file()
        if not CORRECTIONS.exists():
            CORRECTIONS.write_text(
                "# Corrections Log\n\n_Последние 50 исправлений_\n",
                encoding="utf-8"
            )
