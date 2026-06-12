"""
CronManager — планировщик периодических задач FreePalp.

Хранит задачи в state/crons.json.
Запускается при каждом старте Orchestrator — проверяет просроченные задачи.

Команды:
  /cron list                        — список задач
  /cron add "каждые Xм/ч/д" "cmd"  — добавить задачу
  /cron remove <id>                 — удалить
  /cron run <id>                    — запустить вручную

Встроенные задачи (создаются автоматически):
  - weekly_digest   — еженедельный дайджест метрик
  - memory_cleanup  — ежедневная чистка памяти
"""

import json
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Callable, Awaitable

CRONS_FILE = Path(__file__).parent.parent / "state" / "crons.json"

# Встроенные задачи — создаются при первом запуске
BUILTIN_CRONS = [
    {
        "id":          "memory_cleanup",
        "name":        "Ежедневная чистка памяти",
        "interval_h":  24,
        "command":     "__memory_cleanup__",
        "builtin":     True,
    },
    {
        "id":          "weekly_digest",
        "name":        "Еженедельный дайджест",
        "interval_h":  168,   # 7 дней
        "command":     "__weekly_digest__",
        "builtin":     True,
    },
]


def _parse_interval(text: str) -> Optional[int]:
    """
    Парсит строку интервала в часы.
    Форматы: "каждые 30м", "каждые 2ч", "каждый день", "каждую неделю",
             "30m", "2h", "1d", "1w"
    """
    text = text.lower().strip()
    import re

    # Русские варианты
    if "неделю" in text or "неделя" in text or "1w" in text:
        return 168
    if "день" in text or "дней" in text or "1d" in text:
        return 24

    m = re.search(r"(\d+)\s*[мmминут]", text)
    if m:
        return max(1, int(m.group(1)) // 60)   # переводим минуты в часы (мин 1ч)

    m = re.search(r"(\d+)\s*[чhч]", text)
    if m:
        return int(m.group(1))

    m = re.search(r"(\d+)\s*[дd]", text)
    if m:
        return int(m.group(1)) * 24

    m = re.search(r"(\d+)\s*[wн]", text)
    if m:
        return int(m.group(1)) * 168

    return None


class CronManager:

    def __init__(self):
        CRONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_builtins()

    # ──────────────────────────────────────────────────────────────────
    # Публичный API
    # ──────────────────────────────────────────────────────────────────

    def list_crons(self) -> list[dict]:
        return self._load()

    def add(self, name: str, interval_str: str, command: str) -> Optional[dict]:
        """Добавляет новую задачу. Возвращает созданную запись или None."""
        hours = _parse_interval(interval_str)
        if not hours:
            return None
        cron = {
            "id":         str(uuid.uuid4())[:8],
            "name":       name,
            "interval_h": hours,
            "command":    command,
            "builtin":    False,
            "created_at": datetime.now().isoformat(),
            "last_run":   None,
            "next_run":   datetime.now().isoformat(),   # запустить при первой возможности
            "run_count":  0,
        }
        crons = self._load()
        crons.append(cron)
        self._save(crons)
        return cron

    def remove(self, cron_id: str) -> bool:
        crons = self._load()
        before = len(crons)
        crons = [c for c in crons if c["id"] != cron_id or c.get("builtin")]
        if len(crons) < before:
            self._save(crons)
            return True
        return False

    async def tick(self, handlers: dict) -> list[str]:
        """
        Проверяет просроченные задачи и запускает их.
        handlers — словарь {command: async_callable}
        Возвращает список выполненных задач.
        """
        crons = self._load()
        now = datetime.now()
        executed = []

        for cron in crons:
            next_run_str = cron.get("next_run")
            if not next_run_str:
                continue
            try:
                next_run = datetime.fromisoformat(next_run_str)
            except Exception:
                continue

            if now < next_run:
                continue

            # Пора запустить
            cmd = cron["command"]
            handler = handlers.get(cmd)
            if handler:
                try:
                    await handler()
                    executed.append(cron["name"])
                except Exception as e:
                    print(f"  [Cron] Ошибка '{cron['name']}': {e}")

            # Обновляем next_run
            cron["last_run"]  = now.isoformat()
            cron["next_run"]  = (now + timedelta(hours=cron["interval_h"])).isoformat()
            cron["run_count"] = cron.get("run_count", 0) + 1

        if executed:
            self._save(crons)

        return executed

    def mark_run(self, cron_id: str):
        """Вручную пометить задачу как только что выполненную."""
        crons = self._load()
        now = datetime.now()
        for c in crons:
            if c["id"] == cron_id:
                c["last_run"] = now.isoformat()
                c["next_run"] = (now + timedelta(hours=c["interval_h"])).isoformat()
                c["run_count"] = c.get("run_count", 0) + 1
                break
        self._save(crons)

    # ──────────────────────────────────────────────────────────────────
    # Внутренние методы
    # ──────────────────────────────────────────────────────────────────

    def _load(self) -> list[dict]:
        if not CRONS_FILE.exists():
            return []
        try:
            return json.loads(CRONS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []

    def _save(self, crons: list[dict]):
        CRONS_FILE.write_text(
            json.dumps(crons, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )

    def _ensure_builtins(self):
        """Создаёт встроенные задачи если их нет."""
        crons = self._load()
        existing_ids = {c["id"] for c in crons}
        added = False
        now = datetime.now()

        for b in BUILTIN_CRONS:
            if b["id"] not in existing_ids:
                crons.append({
                    **b,
                    "created_at": now.isoformat(),
                    "last_run":   None,
                    "next_run":   (now + timedelta(hours=b["interval_h"])).isoformat(),
                    "run_count":  0,
                })
                added = True

        if added:
            self._save(crons)
