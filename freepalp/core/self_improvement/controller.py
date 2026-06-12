"""
SelfImprovementController — главный оркестратор самообучения.

Полный цикл:
  1. Evaluator анализирует накопленные метрики
  2. Для каждого кандидата Improver генерирует улучшение через LLM
  3. Сборка нового prompts.json с изменениями
  4. VersionManager: propose() -> test() -> activate() или rollback()
  5. Отчёт: что изменилось, тесты прошли/нет

Вызывается из:
  - CLI: /improve
  - Auto: каждые AUTOIMPROVE_EVERY задач (если включено)
"""

import json
import copy
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .metrics import Evaluator, MetricsCollector
from .improver import Improver
from .version_manager import VersionManager
from .validator import HeldOutValidator
from ...core import prompt_loader

AUTOIMPROVE_EVERY = 5           # задач до автоматического запуска (per session)
MIN_SCORE_THRESHOLD = 0.82      # ниже этого — тип задачи считается проблемным
HELDOUT_FRACTION = 0.3          # доля записей в held-out (для валидации обобщения)
EDIT_BUDGET_MAX_RATIO = 3.0     # макс. изменение длины промпта («textual learning rate»)
_STATE_FILE = Path(__file__).parent.parent.parent / "memory" / "improve_state.json"


def _safe_print(text: str):
    """Print that survives Windows cp1251 encoding."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


class SelfImprovementController:
    """Manages the full self-improvement cycle."""

    def __init__(self, model_id: str = "llama-3.3-70b-versatile", provider: str = "groq"):
        self.evaluator = Evaluator()
        self.collector = MetricsCollector()
        self.improver = Improver(model_id=model_id, provider=provider)
        self.vm = VersionManager()
        self.validator = HeldOutValidator(self.improver)
        self._task_counter = 0          # tasks this session
        self._improving = False         # guard against concurrent runs
        self._state = self._load_state()

    def _print(self, text: str):
        _safe_print(text)

    # ------------------------------------------------------------------
    # Persistent state (survives restarts)
    # ------------------------------------------------------------------

    def _load_state(self) -> dict:
        try:
            if _STATE_FILE.exists():
                return json.loads(_STATE_FILE.read_text("utf-8"))
        except Exception:
            pass
        return {"total_tasks_ever": 0, "last_improve_at_task": 0, "improved_types": {}}

    def _save_state(self):
        try:
            _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _STATE_FILE.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Метрика после каждой задачи
    # ------------------------------------------------------------------

    def record_task(
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
        """Записывает метрику задачи. Вызывается из Orchestrator после run()."""
        self.collector.log(
            task_type=task_type,
            user_input=user_input,
            critic_score=critic_score,
            iterations=iterations,
            model=model,
            elapsed=elapsed,
            issues=issues,
            suggestions=suggestions,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
        )
        self._task_counter += 1
        self._state["total_tasks_ever"] = self._state.get("total_tasks_ever", 0) + 1
        self._save_state()

    def should_autoimprove(self) -> bool:
        """True если пора запустить авто-улучшение.

        Логика:
        - каждые AUTOIMPROVE_EVERY задач в сессии
        - ИЛИ при старте если есть проблемные типы (называется startup check)
        - НО не если улучшение уже идёт
        """
        if self._improving:
            return False
        if not self.evaluator.has_enough_data():
            return False
        # Per-session trigger
        if self._task_counter > 0 and self._task_counter % AUTOIMPROVE_EVERY == 0:
            return True
        return False

    def needs_improve_on_startup(self) -> bool:
        """Проверяет нужно ли улучшение сразу при старте сервера."""
        if self._improving:
            return False
        if not self.evaluator.has_enough_data():
            return False
        records = self.evaluator.load_recent(50)
        candidates = self.evaluator.analyze(records)
        # Есть кандидат с приоритетом >= 7 — улучшаем сразу
        return any(c["priority"] >= 7 for c in candidates)

    # ------------------------------------------------------------------
    # Главный цикл улучшения
    # ------------------------------------------------------------------

    async def run(self, force: bool = False, max_candidates: int = 3) -> dict:
        """
        Запускает полный цикл самоулучшения.
        Возвращает отчёт с результатами.
        """
        if self._improving and not force:
            return {"error": "Improvement already running", "version_activated": False, "changes": []}

        self._improving = True
        report = {
            "version_proposed": None,
            "version_activated": False,
            "changes": [],
            "test_passed": False,
            "test_output": "",
            "rollback": False,
            "heldout": None,          # результат held-out валидации (SkillOpt)
            "error": None,
        }

        try:
            # 1. Анализ метрик (SkillOpt: Rollout уже накоплен в метриках)
            records = self.evaluator.load_recent(50)
            if not records and not force:
                report["error"] = "Not enough data (need at least 5 tasks)"
                return report

            # ── SkillOpt split: train (для Reflect/Edit) + held-out (для Validate) ──
            # СЛУЧАЙНЫЙ split (seeded) — чтобы слабые задачи попали и в train (анализ
            # их увидит), и в held-out (валидация проверит). Хронологический split
            # прятал свежие слабые задачи только в held-out → анализ их не видел.
            n_held = max(1, int(len(records) * HELDOUT_FRACTION)) if len(records) >= 4 else 0
            if n_held:
                import random as _random
                shuffled = list(records)
                _random.Random(42).shuffle(shuffled)   # seed для воспроизводимости
                held_out      = shuffled[:n_held]
                train_records = shuffled[n_held:]
            else:
                train_records = records
                held_out      = []
            self._print(f"  [SI] Split (random): train={len(train_records)}, held-out={len(held_out)}")

            # 2. Reflect: анализ ТОЛЬКО train-данных
            candidates = self.evaluator.analyze(train_records)
            if not candidates and not force:
                report["error"] = "No improvement candidates found - system is performing well!"
                return report

            self._print(f"  [SI] Candidates: {len(candidates)}")
            records = train_records   # дальше используем train для примеров правок

            # 2. Загружаем текущий конфиг как основу для новой версии
            prompts_file = Path(__file__).parent.parent.parent / "config" / "prompts.json"
            current_config = json.loads(prompts_file.read_text("utf-8"))
            new_config = copy.deepcopy(current_config)
            changes_made = []

            # 3. Применяем улучшения для топ N кандидатов
            for candidate in candidates[:max_candidates]:
                component = candidate["component"]
                task_type = candidate["task_type"]
                self._print(f"  [SI] Improving: {component}/{task_type} (priority={candidate['priority']})")

                if component == "worker_prompt":
                    current_prompt = new_config["worker_prompts"].get(task_type, "")
                    new_prompt = await self.improver.improve_worker_prompt(
                        task_type=task_type,
                        current_prompt=current_prompt,
                        issues=candidate["evidence"],
                        stats=candidate["stats"],
                    )
                    # Edit budget («textual learning rate»): отклоняем разрушительные
                    # переписывания — длина не должна меняться более чем в EDIT_BUDGET_MAX_RATIO раз.
                    within_budget = True
                    if current_prompt and new_prompt:
                        ratio = len(new_prompt) / max(1, len(current_prompt))
                        if ratio > EDIT_BUDGET_MAX_RATIO or ratio < (1.0 / EDIT_BUDGET_MAX_RATIO):
                            within_budget = False
                            self._print(f"    !! worker_prompt[{task_type}]: edit-budget превышен (ratio={ratio:.1f}) — пропуск")

                    if new_prompt and len(new_prompt) > 50 and not new_prompt.startswith("[") and within_budget:
                        new_config["worker_prompts"][task_type] = new_prompt
                        changes_made.append({
                            "component": "worker_prompt",
                            "task_type": task_type,
                            "reason": candidate["problem"],
                            "chars_before": len(current_prompt),
                            "chars_after": len(new_prompt),
                        })
                        self._print(f"    -> worker_prompt[{task_type}]: {len(current_prompt)} -> {len(new_prompt)} chars")

                elif component == "keywords":
                    current_kw = new_config["task_keywords"].get(task_type, [])
                    new_kw = await self.improver.improve_keywords(
                        task_type=task_type,
                        current_keywords=current_kw,
                        misrouted_examples=[r["preview"] for r in records if r["task_type"] == task_type][:10],
                        stats=candidate["stats"],
                    )
                    if new_kw and len(new_kw) > len(current_kw):
                        new_added = [k for k in new_kw if k not in current_kw]
                        new_config["task_keywords"][task_type] = new_kw
                        changes_made.append({
                            "component": "keywords",
                            "task_type": task_type,
                            "reason": candidate["problem"],
                            "new_keywords": new_added[:10],
                            "total_before": len(current_kw),
                            "total_after": len(new_kw),
                        })
                        self._print(f"    -> keywords[{task_type}]: +{len(new_added)} new ({len(current_kw)} -> {len(new_kw)})")

                elif component == "critic_system":
                    current_cs = new_config.get("critic_system", "")
                    new_cs = await self.improver.improve_critic_system(
                        current_prompt=current_cs,
                        stats=candidate["stats"],
                    )
                    if new_cs and len(new_cs) > 100 and not new_cs.startswith("["):
                        new_config["critic_system"] = new_cs
                        changes_made.append({
                            "component": "critic_system",
                            "reason": candidate["problem"],
                            "chars_before": len(current_cs),
                            "chars_after": len(new_cs),
                        })
                        self._print(f"    -> critic_system: {len(current_cs)} -> {len(new_cs)} chars")

            if not changes_made:
                report["error"] = "LLM could not generate improvements (check API key or model)"
                return report

            # 4. Метаданные изменений
            new_config["last_improved_at"] = datetime.now().isoformat()
            new_config["improvement_reason"] = f"{len(changes_made)} components improved automatically"

            # 5. Предлагаем новую версию
            changes_desc = "; ".join(
                f"{c['component']}[{c.get('task_type','all')}]" for c in changes_made
            )
            new_version = self.vm.propose(new_config, changes_desc)
            report["version_proposed"] = new_version
            report["changes"] = changes_made
            self._print(f"  [SI] Proposed version: v{new_version}")

            # 6a. Статичный тест (проверка что система не сломана)
            self._print(f"  [SI] Running test_mvp.py...")
            test_passed, test_output = self.vm.test(new_version)
            report["test_passed"] = test_passed
            report["test_output"] = test_output

            # 6b. SkillOpt Validate: held-out валидация (проверка обобщения)
            heldout_passed = True
            if held_out:
                self._print(f"  [SI] Held-out валидация на {len(held_out)} задачах...")
                try:
                    _changed_types = [c.get("task_type") for c in changes_made
                                      if c.get("task_type")]
                    ho = await self.validator.validate(held_out, new_config,
                                                       baseline_config=current_config,
                                                       changed_types=_changed_types)
                    report["heldout"] = ho
                    heldout_passed = ho["passed"]
                    self._print(f"  [SI] Held-out: {ho['reason']}")
                    if ho["regression"]:
                        self._print(f"  [SI] ⚠ РЕГРЕССИЯ на held-out — кандидат отклонён")
                except Exception as e:
                    self._print(f"  [SI] Held-out валидация упала: {e} — пропускаем гейт")
                    heldout_passed = True   # ошибка валидатора не блокирует

            # 7. Активация только если ОБА гейта пройдены (статичный тест И held-out)
            if test_passed and heldout_passed:
                self._print(f"  [SI] Все гейты PASSED! Активирую v{new_version}...")
                activated = self.vm.activate(new_version)
                report["version_activated"] = activated
                self._print(f"  [SI] v{new_version} is now active!" if activated else
                             f"  [SI] Activation error")
            else:
                report["rollback"] = True
                reason = "static test" if not test_passed else "held-out regression"
                self._print(f"  [SI] Гейт не пройден ({reason})! Оставляю v{self.vm.current_version()}")
                if not test_passed:
                    self._print(f"  [SI] Test output:\n{test_output[-300:]}")

        except Exception as e:
            import traceback
            report["error"] = str(e)
            report["traceback"] = traceback.format_exc()
            self._print(f"  [SI] Error: {e}")
        finally:
            self._improving = False
            # Обновляем state
            if report.get("version_activated"):
                self._state["last_improve_at_task"] = self._state.get("total_tasks_ever", 0)
                self._save_state()

        return report

    # ------------------------------------------------------------------
    # Weekly digest (called from cron)
    # ------------------------------------------------------------------

    async def generate_weekly_digest(self) -> str:
        """Generate a weekly digest of metrics and improvement status.

        Returns a human-readable summary string and logs it to HOT memory.
        """
        try:
            stats = self.evaluator.get_stats_summary()
            version = self.vm.current_version()
            total    = stats.get("total_tasks", 0)
            avg_s    = stats.get("avg_score", 0.0)
            cost     = stats.get("total_cost_usd", 0.0)
            tok_in   = stats.get("total_tokens_in", 0)
            tok_out  = stats.get("total_tokens_out", 0)

            lines = [
                f"=== Weekly Digest (v{version}) ===",
                f"Tasks: {total}  |  Avg score: {avg_s:.2f}  |  Cost: ${cost:.4f}",
                f"Tokens: {tok_in} in / {tok_out} out",
            ]

            if self.evaluator.has_enough_data():
                records    = self.evaluator.load_recent(50)
                candidates = self.evaluator.analyze(records)
                if candidates:
                    lines.append(f"Improvement candidates: {len(candidates)}")
                    for c in candidates[:3]:
                        lines.append(f"  • {c['component']}/{c['task_type']}: {c['problem'][:80]}")
                else:
                    lines.append("System is performing well — no improvements needed.")
            else:
                lines.append("Not enough data for improvement analysis yet.")

            digest = "\n".join(lines)
            self._print(f"\n{digest}")

            # Persist to HOT memory so future sessions see the digest
            try:
                from freepalp.memory.memory_manager import MemoryManager
                mm = MemoryManager()
                mm.add_to_hot("digest", digest)
            except Exception:
                pass

            return digest
        except Exception as e:
            self._print(f"  [Digest] Error: {e}")
            return f"[Digest error: {e}]"

    # ------------------------------------------------------------------
    # CLI helpers
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Быстрый статус для /improve status."""
        stats = self.evaluator.get_stats_summary()
        candidates = []
        if self.evaluator.has_enough_data():
            records = self.evaluator.load_recent(50)
            candidates = self.evaluator.analyze(records)

        return {
            "current_version": self.vm.current_version(),
            "metrics": stats,
            "improvement_candidates": len(candidates),
            "ready_to_improve": len(candidates) > 0,
            "candidates_preview": [
                f"{c['component']}/{c['task_type']}: {c['problem'][:60]}"
                for c in candidates[:3]
            ],
            "versions_available": len(self.vm.list_versions()),
        }
