"""Тест failure-mode таргетинга Evaluator (само-улучшение).

Проверяет, что повторяющаяся КОНКРЕТНАЯ причина провала (напр. «не вызвал
write_file») всплывает как worker_prompt-кандидат с высоким приоритетом —
даже когда средний score типа высокий (раньше такие провалы тонули в среднем).
"""
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from freepalp.core.self_improvement.metrics import Evaluator, _failure_mode


def _rec(task_type, score, issues, iters=1):
    return {"task_type": task_type, "critic_score": score, "iterations": iters,
            "issues": issues, "tokens_total": 1000, "model": "test"}


def test_failure_mode_classifier():
    assert _failure_mode("агент не вызвал write_file, код показан текстом") == "code_not_saved"
    assert _failure_mode("Отсутствие реализации функции в файле ev_x.py") == "code_not_saved"
    assert _failure_mode("обычное замечание про стиль") is None
    print("OK: классификатор режимов")


def test_failure_mode_surfaces_as_top_candidate():
    # тип coding_small с ВЫСОКИМ средним (много 0.95), но 3 провала с одной причиной
    records = []
    for _ in range(10):
        records.append(_rec("coding_small", 0.95, ["мелкое замечание"]))
    # 3 провала: «не записал файл», описанные РАЗНЫМИ словами (как в реале)
    records.append(_rec("coding_small", 0.20, ["Пользователь просил создать файл, но агент не вызвал write_file"], iters=2))
    records.append(_rec("coding_small", 0.30, ["Отсутствие реализации функции в файле ev_x.py"], iters=2))
    records.append(_rec("coding_small", 0.25, ["Не предоставлен код, реализующий алгоритм"], iters=2))

    cands = Evaluator().analyze(records)
    assert cands, "должны быть кандидаты"
    top = cands[0]
    assert top["component"] == "worker_prompt", f"топ должен быть worker_prompt, а не {top['component']}"
    assert top["task_type"] == "coding_small"
    assert top["priority"] >= 8, f"failure-mode должен иметь высокий приоритет, а не {top['priority']}"
    assert "code_not_saved" in str(top.get("stats", {})) + top.get("problem", ""), "должен указывать failure-mode"
    print(f"OK: failure-mode всплыл топом (prio={top['priority']})")


def test_no_false_positive_when_clean():
    # ровный набор без провалов — failure-mode кандидата быть не должно
    records = [_rec("coding_small", 0.95, ["мелочь"]) for _ in range(12)]
    cands = Evaluator().analyze(records)
    fm = [c for c in cands if "code_not_saved" in str(c.get("stats", {}))]
    assert not fm, "не должно быть failure-mode кандидата на чистом наборе"
    print("OK: нет ложного срабатывания на чистом наборе")


if __name__ == "__main__":
    test_failure_mode_classifier()
    test_failure_mode_surfaces_as_top_candidate()
    test_no_false_positive_when_clean()
    print("\nВСЕ ТЕСТЫ failure-mode ПРОШЛИ")
