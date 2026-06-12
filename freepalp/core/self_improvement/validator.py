"""
HeldOutValidator — валидация улучшений на отложенных (held-out) данных.

Идея из SkillOpt (Microsoft Research): цикл Rollout → Reflect → Edit → Validate.
Ключевое: правка промпта принимается ТОЛЬКО если на held-out задачах
(которые НЕ участвовали в анализе) качество не упало.

Это устраняет «оверфиттинг под train» — раньше система правила промпт под
те же задачи, на которых нашла проблему, и не проверяла обобщение.

Процесс:
  1. Берём held-out записи (task_type + preview задачи + старый score)
  2. Для каждой: решаем задачу с КАНДИДАТНЫМ промптом → ответ
  3. Судим ответ по рубрике критика → новый score
  4. Сравниваем avg(new) vs avg(old). Принять если регрессии нет.
"""

from __future__ import annotations
import re
from typing import Optional


class HeldOutValidator:
    """Проверяет кандидата на отложенных задачах через тот же LLM."""

    def __init__(self, improver, max_tasks: int = 3, regression_eps: float = 0.03):
        """
        improver        — экземпляр Improver (для LLM-вызовов)
        max_tasks       — сколько held-out задач прогнать (стоимость vs точность)
        regression_eps  — допустимое падение score без отклонения (шум LLM-судьи)
        """
        self.improver = improver
        self.max_tasks = max_tasks
        self.regression_eps = regression_eps

    async def validate(self, held_out: list[dict], candidate_config: dict,
                       baseline_config: dict | None = None,
                       changed_types: list | None = None) -> dict:
        """Прогоняет held-out задачи: сравнивает СТАРЫЙ и НОВЫЙ промпт.

        Калибровка: и старый, и новый ответ судит ОДИН судья — честное сравнение
        (раньше старый брался из оригинального критика → несопоставимо).

        changed_types — типы задач которые реально изменились: валидация
        приоритетно берёт ИХ (иначе можно протестировать нетронутые типы и
        получить тривиальный pass).

        Возвращает:
          {
            "ran": int, "avg_old": float, "avg_new": float,
            "delta": float, "passed": bool, "regression": bool,
            "details": [{task_type, old, new}], "reason": str
          }
        """
        report = {
            "ran": 0, "avg_old": 0.0, "avg_new": 0.0, "delta": 0.0,
            "passed": False, "regression": False, "details": [], "reason": "",
        }

        # Берём задачи с валидным preview и старым score
        valid = [r for r in held_out
                 if r.get("preview") and not (r.get("critic_score", 0) == 0.0
                                              and r.get("tokens_total", 0) == 0)]
        # Приоритет — задачи изменённых типов (чтобы валидация реально тестировала правки)
        if changed_types:
            ct = set(changed_types)
            primary = [r for r in valid if r.get("task_type") in ct]
            rest    = [r for r in valid if r.get("task_type") not in ct]
            tasks = (primary + rest)[: self.max_tasks]
        else:
            tasks = valid[: self.max_tasks]
        if not tasks:
            report["reason"] = "no held-out tasks to validate"
            report["passed"] = True   # нечего проверять — не блокируем
            return report

        new_prompts = candidate_config.get("worker_prompts", {})
        old_prompts = (baseline_config or {}).get("worker_prompts", {})
        old_scores, new_scores = [], []

        for r in tasks:
            task_type = r["task_type"]
            preview   = r["preview"]
            new_prompt = new_prompts.get(task_type, new_prompts.get("general", ""))
            old_prompt = old_prompts.get(task_type, old_prompts.get("general", ""))

            try:
                # Решаем ОБА промпта и судим одним судьёй (честно)
                new_answer = await self._solve(new_prompt, preview)
                new_score  = await self._judge(preview, new_answer, task_type)
                if old_prompt:
                    old_answer = await self._solve(old_prompt, preview)
                    old_score  = await self._judge(preview, old_answer, task_type)
                else:
                    # Нет старого промпта (создаётся с нуля) — берём оригинальный critic score
                    old_score = float(r.get("critic_score", 0.0))
            except Exception:
                # Ошибка прогона — пропускаем задачу (не штрафуем кандидата)
                continue

            old_scores.append(old_score)
            new_scores.append(new_score)
            report["details"].append({
                "task_type": task_type, "old": round(old_score, 3),
                "new": round(new_score, 3),
            })

        if not new_scores:
            report["reason"] = "all held-out runs failed"
            report["passed"] = True   # не смогли проверить — не блокируем
            return report

        avg_old = sum(old_scores) / len(old_scores)
        avg_new = sum(new_scores) / len(new_scores)
        delta = avg_new - avg_old

        report["ran"]        = len(new_scores)
        report["avg_old"]    = round(avg_old, 3)
        report["avg_new"]    = round(avg_new, 3)
        report["delta"]      = round(delta, 3)
        report["regression"] = delta < -self.regression_eps
        report["passed"]     = not report["regression"]
        report["reason"]     = (
            f"held-out: {avg_old:.2f} -> {avg_new:.2f} (Δ{delta:+.2f}) на {len(new_scores)} задачах"
        )
        return report

    # ──────────────────────────────────────────────────────────────
    # Решение и оценка через LLM
    # ──────────────────────────────────────────────────────────────

    async def _solve(self, worker_prompt: str, task_preview: str) -> str:
        """Решает задачу с кандидатным worker-промптом как system."""
        system = worker_prompt or "Ты полезный AI ассистент. Реши задачу пользователя."
        user = f"Задача:\n{task_preview}\n\nДай качественный, полный ответ."
        return await self.improver._call_llm(user, system_msg=system)

    async def _judge(self, task_preview: str, answer: str, task_type: str) -> float:
        """Оценивает ответ по рубрике критика. Возвращает score 0.0–1.0."""
        judge_prompt = f"""Ты строгий критик качества ответов AI.

ТИП ЗАДАЧИ: {task_type}

ЗАДАЧА:
{task_preview}

ОТВЕТ АГЕНТА:
{answer[:1500]}

Оцени ответ по шкале 0.0–1.0:
- 1.0 — полный, корректный, готовый к использованию
- 0.8 — хороший, но есть мелкие недочёты
- 0.6 — частично решает, упущены важные детали
- 0.4 — слабый, много проблем
- 0.2 — почти бесполезный

Верни ТОЛЬКО число от 0.0 до 1.0, без слов."""

        raw = await self.improver._call_llm(
            judge_prompt,
            system_msg="Ты строгий, объективный критик. Отвечай только числом."
        )
        return self._parse_score(raw)

    @staticmethod
    def _parse_score(raw: str) -> float:
        """Извлекает число 0.0–1.0 из ответа судьи."""
        if not raw:
            return 0.0
        m = re.search(r"(?<![\d.])(0?\.\d+|1\.0|0|1)(?![\d.])", raw)
        if not m:
            return 0.5  # не распознали — нейтрально
        try:
            v = float(m.group(1))
            return max(0.0, min(1.0, v))
        except ValueError:
            return 0.5
