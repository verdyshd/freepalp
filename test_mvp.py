"""
Тесты FreePalp без реальных LLM вызовов — проверяем логику системы.
Запуск: python test_mvp.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

# UTF-8 вывод на Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


def test_task_parser():
    """Тест определения типа задачи."""
    from freepalp.core.task_parser import detect_task_type
    from freepalp.core.models import TaskType

    cases = [
        ("напиши функцию сортировки на Python", TaskType.CODING_SMALL),
        ("создай Discord бота с базой данных", TaskType.CODING_LARGE),
        ("спроектируй архитектуру микросервисов", TaskType.ARCHITECTURE),
        ("найди документацию FastAPI", TaskType.SEARCH),
        ("прочитай файл config.json", TaskType.FILE_OPS),
    ]

    passed = 0
    for text, expected in cases:
        result = detect_task_type(text)
        ok = result == expected
        status = "✅" if ok else "❌"
        print(f"  {status} '{text[:40]}' → {result.value} (ожидалось: {expected.value})")
        if ok:
            passed += 1

    print(f"\n  Результат: {passed}/{len(cases)}")
    return passed >= 4  # допускаем 1 промах


def test_router():
    """Тест конфигурации роутера (без реальных API вызовов)."""
    from freepalp.core.router import Router
    from freepalp.core.models import TaskType, ModelTier

    router = Router()

    # Проверяем статическую конфигурацию моделей (до discovery)
    static_models = router.models
    print(f"  📋 Статических моделей: {len(static_models)}")
    for m in static_models[:5]:
        print(f"    • {m.name} ({m.tier.value}) [{m.provider}]")

    assert len(static_models) > 0, "Нет статических моделей в конфиге"

    # Проверяем tier mapping
    tiers_found = {m.tier for m in static_models}
    print(f"  ✅ Tiers в конфиге: {[t.value for t in tiers_found]}")

    # Проверяем routing_rules и fallback_chain
    rules = router.routing_rules
    chain = router.fallback_chain
    print(f"  ✅ routing_rules: {len(rules)} правил")
    print(f"  ✅ fallback_chain: {chain}")

    # Проверяем специальные модели
    crit_tier = router.critic_model_tier
    arch_tier  = router.architect_model_tier
    print(f"  ✅ critic_model_tier={crit_tier}, architect_model_tier={arch_tier}")

    return len(static_models) > 0


def test_file_tools():
    """Тест файловых инструментов."""
    from freepalp.tools.file_tools import write_file, read_file, list_files, delete_file

    # Запись
    result = write_file("test_dir/hello.txt", "Hello, FreePalp!")
    assert result["ok"], f"Запись не удалась: {result}"
    print(f"  ✅ write_file → {result['path']}")

    # Чтение
    result = read_file("test_dir/hello.txt")
    assert result["ok"] and result["content"] == "Hello, FreePalp!"
    print(f"  ✅ read_file → '{result['content']}'")

    # Список файлов
    result = list_files("test_dir")
    assert result["ok"]
    print(f"  ✅ list_files → {[f['name'] for f in result['files']]}")

    # Блокировка path traversal
    result = read_file("../../../etc/passwd")
    assert not result["ok"]
    print(f"  ✅ path traversal blocked: {result['error'][:50]}")

    # Удаление
    result = delete_file("test_dir/hello.txt")
    assert result["ok"]
    print(f"  ✅ delete_file → OK")

    return True


def test_tool_agent():
    """Тест ToolAgent."""
    async def _run():
        from freepalp.agents.tool_agent import ToolAgent
        agent = ToolAgent()

        # Список инструментов
        tools = agent.list_tools()
        print(f"  ✅ Инструментов: {len(tools)}")
        print(f"     {', '.join(tools[:8])}...")

        assert len(tools) >= 10, f"Слишком мало инструментов: {len(tools)}"

        # Вызов через JSON
        result = await agent.execute_from_json(
            '{"tool": "write_file", "args": {"path": "test_agent.txt", "content": "agent_test"}}'
        )
        assert result["ok"], f"Ошибка: {result}"
        print(f"  ✅ execute_from_json → OK")

        # Неизвестный инструмент
        result = await agent.execute("unknown_tool")
        assert not result["ok"]
        print(f"  ✅ unknown tool blocked: {result['error'][:50]}")

        # Cleanup
        from freepalp.tools.file_tools import delete_file
        delete_file("test_agent.txt")

    asyncio.run(_run())
    return True


def test_critic_parser():
    """Тест парсера ответа критика."""
    from freepalp.agents.critic_agent import CriticAgent
    from freepalp.core.models import ModelConfig, ModelTier

    dummy_model = ModelConfig(
        name="test",
        tier=ModelTier.LOCAL_SMALL,
        provider="ollama",
        model_id="test:7b",
    )
    critic = CriticAgent(dummy_model)

    # Тест парсинга — плохой ответ
    raw_bad = """PASSED: no
SCORE: 0.4
ISSUES:
- Нет обработки ошибок
- Отсутствуют type hints
SUGGESTIONS:
- Добавь try/except
- Добавь аннотации типов"""

    fb = critic._parse_response(raw_bad)
    assert not fb.passed
    assert fb.score == 0.4
    assert len(fb.issues) == 2
    assert fb.must_retry  # score < 0.6
    print(f"  ✅ bad response: passed={fb.passed}, score={fb.score}, issues={len(fb.issues)}, must_retry={fb.must_retry}")

    # Тест парсинга — хороший ответ
    raw_good = """PASSED: yes
SCORE: 0.95
ISSUES:
SUGGESTIONS:
- Minor style improvement"""

    fb2 = critic._parse_response(raw_good)
    assert fb2.passed
    assert fb2.score == 0.95
    assert not fb2.must_retry
    print(f"  ✅ good response: passed={fb2.passed}, score={fb2.score}, must_retry={fb2.must_retry}")

    return True


def test_memory_manager():
    """Тест трёхуровневой системы памяти (HOT/WARM/COLD)."""
    from freepalp.memory.memory_manager import MemoryManager

    mgr = MemoryManager()

    # HOT память
    hot = mgr.load_hot()
    print(f"  HOT memory: {len(hot.splitlines())} строк")

    # Лог исправления
    mgr.log_correction(
        context="coding_small",
        problem="Нет обработки ошибок",
        lesson="Всегда добавляй try/except в IO операции"
    )
    corrections = mgr.get_recent_corrections(5)
    assert len(corrections) >= 1
    print(f"  ✅ log_correction: OK ({len(corrections)} записей)")

    # Сессия
    mgr.save_session("test_mvp_001", "Написана функция сортировки", "coding_small")
    stats = mgr.get_stats()
    assert stats["sessions"] >= 1
    print(f"  ✅ save_session: OK")

    # Проект
    mgr.add_to_project("test_project", "Используем FastAPI для роутинга")
    proj_mem = mgr.load_project_memory("test_project")
    assert "FastAPI" in proj_mem
    print(f"  ✅ project memory: OK")

    # Статистика
    print(f"  Stats: sessions={stats['sessions']}")

    return True


# ------------------------------------------------------------------
# Запуск всех тестов
# ------------------------------------------------------------------

def run_all():
    tests = [
        ("Task Parser",     test_task_parser),
        ("Router",          test_router),
        ("File Tools",      test_file_tools),
        ("Tool Agent",      test_tool_agent),
        ("Critic Parser",   test_critic_parser),
        ("Memory Manager",  test_memory_manager),
    ]

    print("\n" + "="*50)
    print("  FreePalp MVP Tests")
    print("="*50)

    passed = 0
    for name, fn in tests:
        print(f"\n[TEST] {name}:")
        try:
            ok = fn()
            if ok:
                passed += 1
                print(f"  -> PASSED")
            else:
                print(f"  -> FAILED")
        except Exception as e:
            print(f"  -> ERROR: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'='*50}")
    print(f"  Итого: {passed}/{len(tests)} тестов прошли")
    print("="*50)
    return passed == len(tests)


if __name__ == "__main__":
    success = run_all()
    sys.exit(0 if success else 1)
