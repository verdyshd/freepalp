"""
MetricsCollector — собирает метрики каждого выполненного задания.
Evaluator — анализирует накопленные метрики и находит слабые места.

Метрики хранятся в freepalp/memory/metrics.jsonl — одна запись на задачу.

Структура записи:
  {
    "ts": "2026-05-22T12:00:00",
    "task_type": "coding_small",
    "user_input_preview": "напиши функцию...",
    "critic_score": 0.85,
    "iterations": 2,
    "model": "llama3-groq-70b",
    "elapsed": 4.2,
    "issues": ["Missing type hints", "..."],
    "suggestions": ["Add type hints"],
    "routed_correctly": true   # задаётся пост-фактум если известно
  }
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional
from collections import defaultdict

METRICS_FILE = Path(__file__).parent.parent.parent / "memory" / "metrics.jsonl"
MIN_TASKS_FOR_ANALYSIS = 5   # минимум записей для запуска анализа
# Порог-кандидат: тип ниже этого считается слабым (цель 0.90, маржа 0.02).
# Раньше было 0.82 — пропускало review/shell ~0.82-0.83 которые явно ниже цели.
CANDIDATE_SCORE_THRESHOLD = 0.88


class MetricsCollector:
    """Логирует метрику после каждого выполненного задания."""

    def log(
        self,
        task_type: str,
        user_input: str,
        critic_score: float,
        iterations: int,
        model: str,
        elapsed: float,
        issues: list[str],
        suggestions: list[str],
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
    ):
        record = {
            "ts": datetime.now().isoformat(),
            "task_type": task_type,
            "preview": user_input[:80],
            "critic_score": round(critic_score, 3),
            "iterations": iterations,
            "model": model,
            "elapsed": round(elapsed, 2),
            "issues": issues[:5],
            "suggestions": suggestions[:3],
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "tokens_total": tokens_in + tokens_out,
            "cost_usd": round(cost_usd, 6),
        }
        try:
            METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(METRICS_FILE, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


class Evaluator:
    """
    Анализирует накопленные метрики и находит компоненты для улучшения.
    Возвращает список (component, problem, priority, evidence).
    """

    def load_recent(self, n: int = 50) -> list[dict]:
        """Загружает последние N записей метрик."""
        if not METRICS_FILE.exists():
            return []
        records = []
        with open(METRICS_FILE, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        records.append(json.loads(line))
                    except Exception:
                        pass
        return records[-n:]

    def has_enough_data(self, min_tasks: int = MIN_TASKS_FOR_ANALYSIS) -> bool:
        records = self.load_recent(min_tasks)
        return len(records) >= min_tasks

    def analyze(self, records: Optional[list[dict]] = None) -> list[dict]:
        """
        Возвращает список кандидатов на улучшение:
        [{"component": "worker_prompt", "task_type": "architecture",
          "problem": "avg score 0.72 < target 0.85",
          "evidence": [...issues...], "priority": 8}]
        """
        if records is None:
            records = self.load_recent(50)
        if not records:
            return []

        candidates = []

        # Группируем по типу задачи
        by_type: dict[str, list[dict]] = defaultdict(list)
        for r in records:
            by_type[r["task_type"]].append(r)

        for task_type, recs in by_type.items():
            if len(recs) < 3:
                continue

            # Исключаем API-ошибки (score=0 + tokens=0) из анализа качества
            valid_recs = [r for r in recs if not (r["critic_score"] == 0.0 and r.get("tokens_total", 0) == 0)]
            eval_recs = valid_recs if len(valid_recs) >= 2 else recs

            scores = [r["critic_score"] for r in eval_recs]
            avg_score = sum(scores) / len(scores)
            min_score = min(scores)
            retry_rate = sum(1 for r in eval_recs if r["iterations"] > 1) / len(eval_recs)

            # Собираем все проблемы которые встречались
            all_issues: list[str] = []
            for r in recs:
                all_issues.extend(r.get("issues", []))

            # Частые проблемы
            issue_counts: dict[str, int] = defaultdict(int)
            for issue in all_issues:
                # нормализуем: берём первые 60 символов как ключ
                key = issue[:60].lower()
                issue_counts[key] += 1
            top_issues = sorted(issue_counts.items(), key=lambda x: -x[1])[:5]
            top_issue_texts = [k for k, _ in top_issues if _ >= 2]

            # Критерии для предложения улучшения worker prompt
            if avg_score < CANDIDATE_SCORE_THRESHOLD and len(recs) >= 3:
                priority = int((CANDIDATE_SCORE_THRESHOLD - avg_score) * 50)  # 0-10
                candidates.append({
                    "component": "worker_prompt",
                    "task_type": task_type,
                    "problem": f"Средний score {avg_score:.2f} ниже целевого 0.90 (min={min_score:.2f})",
                    "evidence": top_issue_texts,
                    "stats": {
                        "avg_score": round(avg_score, 3),
                        "min_score": round(min_score, 3),
                        "retry_rate": round(retry_rate, 2),
                        "n_tasks": len(recs),
                    },
                    "priority": min(priority, 10),
                })

            # Критерии для предложения улучшения keywords (retry rate высокий)
            if retry_rate > 0.4 and len(recs) >= 5:
                candidates.append({
                    "component": "keywords",
                    "task_type": task_type,
                    "problem": f"Retry rate {retry_rate:.0%} — Worker часто выдаёт слабые ответы",
                    "evidence": top_issue_texts,
                    "stats": {
                        "retry_rate": round(retry_rate, 2),
                        "avg_score": round(avg_score, 3),
                        "n_tasks": len(recs),
                    },
                    "priority": int(retry_rate * 10),
                })

        # Глобальный анализ: проверяем critic_system
        all_scores = [r["critic_score"] for r in records]
        global_avg = sum(all_scores) / len(all_scores) if all_scores else 0
        score_variance = sum((s - global_avg) ** 2 for s in all_scores) / len(all_scores) if all_scores else 0

        # Если критик слишком однообразен (variance < 0.02) — предлагаем улучшить промпт
        if score_variance < 0.02 and len(records) >= 10:
            candidates.append({
                "component": "critic_system",
                "task_type": "all",
                "problem": f"Критик слишком однообразен: variance={score_variance:.4f}, все оценки около {global_avg:.2f}",
                "evidence": [],
                "stats": {
                    "global_avg": round(global_avg, 3),
                    "variance": round(score_variance, 4),
                    "n_tasks": len(records),
                },
                "priority": 5,
            })

        # Сортируем по приоритету
        candidates.sort(key=lambda c: -c["priority"])
        return candidates

    def get_stats_summary(self) -> dict:
        """Быстрая сводка для /improve status."""
        records = self.load_recent(50)
        if not records:
            return {"total": 0, "avg_score": 0, "retry_rate": 0, "total_cost_usd": 0.0, "total_tokens": 0}

        # Исключаем записи с ошибками API (score=0 И tokens=0 — значит провайдер упал)
        valid = [r for r in records if not (r["critic_score"] == 0.0 and r.get("tokens_total", 0) == 0)]
        if not valid:
            valid = records  # fallback — показываем всё
        scores = [r["critic_score"] for r in valid]
        retries = sum(1 for r in valid if r["iterations"] > 1)
        total_cost = sum(r.get("cost_usd", 0.0) for r in records)
        total_tokens = sum(r.get("tokens_total", 0) for r in records)
        # by_type also excludes API error records
        by_type = defaultdict(list)
        for r in valid:
            by_type[r["task_type"]].append(r["critic_score"])

        return {
            "total": len(records),
            "valid_total": len(valid),
            "avg_score": round(sum(scores) / len(scores), 3),
            "retry_rate": round(retries / max(len(valid), 1), 2),
            "total_cost_usd": round(total_cost, 4),
            "total_tokens": total_tokens,
            "by_type": {
                t: round(sum(s) / len(s), 3)
                for t, s in by_type.items()
            },
        }
