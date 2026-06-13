"""
FreePalp AI Orchestrator 🐙
CLI интерфейс.

Запуск:
  python app.py                    # интерактивный режим
  python app.py "твоя задача"      # одна задача
  python app.py --models           # список моделей
  python app.py --tool read_file path=main.py   # прямой вызов инструмента
"""

import asyncio
import sys
import os
import argparse
from pathlib import Path

# Добавляем корень проекта в PYTHONPATH
sys.path.insert(0, str(Path(__file__).parent.parent))

# Устанавливаем UTF-8 вывод на Windows
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass

# Загружаем .env если есть
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
except ImportError:
    pass

from freepalp.core.orchestrator import Orchestrator


def _create_skill(name: str, description: str):
    """Генерирует шаблон пользовательского навыка."""
    skills_dir = Path(__file__).parent / "skills"
    skills_dir.mkdir(exist_ok=True)

    skill_path = skills_dir / f"{name}.py"
    if skill_path.exists():
        print(f"  ⚠️  Навык '{name}' уже существует: {skill_path}")
        return

    template = f'''"""
Навык: {name}
{description}

Использование в FreePalp:
  from freepalp.skills.{name} import run_{name}

Подключение к агенту — добавьте вызов в ToolAgent или Orchestrator.
"""

from typing import Optional


async def run_{name}(text: str, **kwargs) -> dict:
    """
    {description}

    Args:
        text: входной текст
        **kwargs: дополнительные параметры

    Returns:
        {{"ok": True, "result": str}}
    """
    # TODO: реализовать логику навыка
    # Пример вызова LLM через Orchestrator:
    # from freepalp.core.orchestrator import Orchestrator
    # orch = Orchestrator()
    # result = await orch.run(f"{{text}}")
    # return {{"ok": True, "result": result.final_answer}}

    return {{"ok": False, "error": "Навык ещё не реализован. Отредактируйте {skills_dir}/{name}.py"}}


# Регистрация в реестре инструментов FreePalp
TOOL_SPEC = {{
    "{name}": {{
        "description": "{description}",
        "fn":          run_{name},
        "async":       True,
        "args":        {{"text": "str"}},
    }}
}}
'''
    skill_path.write_text(template, encoding="utf-8")
    print(f"\n  ✅ Навык создан: {skill_path}")
    print(f"  Отредактируйте файл и реализуйте функцию run_{name}()")
    print(f"  Затем подключите TOOL_SPEC к ToolAgent чтобы использовать в /tool")


BANNER = """
╔══════════════════════════════════════════════════════╗
║   🐙  FreePalp AI Orchestrator  v1.0                ║
║   Worker → ReAct → Critic → Self-Correction Loop    ║
╚══════════════════════════════════════════════════════╝
Введите задачу или команду (/help для справки)
"""


async def run_task(orchestrator: Orchestrator, user_input: str) -> str:
    """Выполняет задачу и выводит результат."""
    print(f"\n{'='*50}")
    print(f"📝 Задача: {user_input[:80]}{'...' if len(user_input) > 80 else ''}")
    print(f"{'='*50}")

    try:
        result = await orchestrator.run(user_input)

        print(f"\n{'='*50}")
        print(f"✅ РЕЗУЛЬТАТ (за {result.elapsed_seconds}с, {result.iterations} итерац.)")
        print(f"{'='*50}\n")
        print(result.final_answer)

        if result.critic_feedback:
            fb = result.critic_feedback
            print(f"\n{'─'*40}")
            print(f"📊 Оценка критика: {fb.score:.2f}/1.0")
            if fb.suggestions:
                print(f"💡 Предложения: {'; '.join(fb.suggestions[:2])}")

        print(f"\n{'─'*40}")
        print(f"🤖 Модель: {result.model_used} | ⏱ {result.elapsed_seconds}с")

        return result.final_answer

    except Exception as e:
        print(f"\n❌ Ошибка: {e}")
        return ""


async def handle_command(orchestrator: Orchestrator, cmd: str) -> bool:
    """
    Обрабатывает специальные команды CLI.
    Возвращает True если нужно продолжить, False если выход.
    """
    parts = cmd.strip().split()
    command = parts[0].lower()

    if command in ("/exit", "/quit", "/q"):
        print("👋 До свидания!")
        return False

    elif command == "/help":
        print("""
Команды:
  /models              — список доступных моделей
  /tools               — список инструментов
  /tool <name> [k=v]   — вызов инструмента напрямую
  /search <запрос>     — быстрый поиск в интернете
  /file <path>         — прочитать файл из sandbox
  /memory              — показать HOT память агента
  /memory stats        — статистика памяти (HOT/WARM/COLD)
  /memory clean        — принудительная очистка и архивация
  /memory search <q>   — поиск по COLD архиву
  /memory export       — экспорт всей памяти в ZIP
  /forget <слово>      — удалить из HOT памяти строки с этим словом
  /cron list           — список cron-задач
  /cron add "2ч" "cmd" — добавить задачу (интервал + команда)
  /cron remove <id>    — удалить задачу
  /cron run <id>       — запустить задачу вручную
  /mcp list            — MCP-серверы (найденные и доступные)
  /mcp template        — создать шаблон .mcp.json
  /skill list          — пользовательские навыки
  /skill create <имя> <описание>  — создать новый навык
  /history [запрос]    — поиск по истории сессий
  /user                — профиль пользователя (USER.md)
  /user set <поле> <значение>  — обновить профиль
  /improve             — запустить цикл самоулучшения
  /improve status      — статистика метрик и кандидаты на улучшение
  /versions            — список версий конфига
  /version rollback    — откатиться к предыдущей версии
  /exit                — выход

Примеры задач:
  напиши функцию для сортировки списка на Python
  создай Discord бота с командой /ping
  объясни что такое asyncio
  найди документацию по FastAPI
""")

    elif command == "/models":
        models = orchestrator.get_available_models()
        disc = orchestrator.router._discovery_used
        src = "live discovery" if disc else "models.json (static)"
        print(f"\n  Источник: {src}")
        print(f"\n📋 Доступные модели ({len(models)}):")
        for m in models:
            print(f"  ✓ {m['name']:30} {m['tier']:15} [{m['provider']}] {m['model_id']}")

    elif command == "/reload":
        print("\n  Обновляю список моделей...")
        new_models = await orchestrator.router.refresh()
        print(f"  Найдено {len(new_models)} моделей:")
        for m in new_models:
            print(f"    • {m.name:30} [{m.provider}] {m.model_id}")

    elif command == "/providers":
        from freepalp.core.model_discovery import get_providers_status
        providers = get_providers_status()

        configured = [p for p in providers if p["configured"]]
        missing    = [p for p in providers if not p["configured"]]

        print(f"\n{'='*60}")
        print(f"  ПРОВАЙДЕРЫ LLM ({len(configured)} активно / {len(missing)} не настроено)")
        print(f"{'='*60}")

        if configured:
            print(f"\n  [АКТИВНЫЕ]")
            for p in configured:
                free_tag = "БЕСПЛАТНО" if p["free"] else "ПЛАТНЫЙ"
                key_info = f"  ключ: {p['key_preview']}" if p["key_preview"] else "  (local)"
                print(f"\n  ✅ {p['name']} [{free_tag}]{key_info}")
                print(f"     Модели : {p['models'][:70]}")
                print(f"     Лимиты : {p['limits'][:70]}")

        if missing:
            print(f"\n  [НЕ НАСТРОЕНО — добавь в .env чтобы активировать]")
            for p in missing:
                free_tag = "БЕСПЛАТНО" if p["free"] else "ПЛАТНЫЙ"
                env_line = f"  {p['env_key']}=..." if p["env_key"] else "  (установи Ollama)"
                print(f"\n  ○ {p['name']} [{free_tag}]")
                print(f"     .env    :{env_line}")
                print(f"     Ключ   : {p['url']}")
                print(f"     Регистр: {p['signup']}")
                print(f"     Модели : {p['models'][:65]}")
                print(f"     Заметка: {p['notes'][:65]}")

    elif command == "/mcp":
        from freepalp.core.mcp_discovery import discover_mcp_servers, generate_mcp_template, save_mcp_server
        sub = parts[1] if len(parts) > 1 else "list"

        if sub == "list":
            info = discover_mcp_servers()
            configured = info["configured"]
            available  = info["available"]
            sources    = info["sources"]

            print(f"\n{'='*60}")
            print(f"  MCP СЕРВЕРЫ")
            print(f"{'='*60}")

            if configured:
                print(f"\n  [АКТИВНЫЕ] (из: {', '.join(sources) or 'нет'})")
                for s in configured:
                    src = s.get("source", "?")
                    print(f"  ✅ {s['name']:20} — {s.get('description', '')[:40]}")
                    print(f"     команда: {s.get('command', '')[:50]}")
                    print(f"     источник: {src}")
            else:
                print(f"\n  Нет активных MCP-серверов.")
                print(f"  Добавьте .mcp.json в корень проекта или запустите /mcp template")

            ready = [s for s in available if s["ready"]]
            if ready:
                print(f"\n  [ГОТОВЫ К ПОДКЛЮЧЕНИЮ] (есть API ключ, не настроен)")
                for s in ready:
                    print(f"  ○ {s['name']:20} npm: {s['npm_package']}")

            not_ready = [s for s in available if not s["ready"] and not s["configured"]]
            if not_ready:
                print(f"\n  [ДОСТУПНЫЕ] (нужен API ключ или npm)")
                for s in not_ready:
                    env_info = f"  .env: {s['env_key']}=..." if s.get("env_key") else "  (без ключа)"
                    print(f"  ○ {s['name']:20} {env_info}")

        elif sub == "template":
            template = generate_mcp_template()
            template_path = Path(__file__).parent / ".mcp.json"
            template_path.write_text(template, encoding="utf-8")
            print(f"\n  ✅ Шаблон создан: {template_path}")
            print(f"  Отредактируйте .mcp.json и заполните ваши API ключи.")
            print(f"\n  Начало файла:")
            print(template[:500])

        elif sub == "build":
            # /mcp build python <name> <description>
            # /mcp build node   <name> <description>
            from freepalp.core.mcp_builder import build_python_mcp, build_node_mcp
            lang = parts[2] if len(parts) > 2 else "python"
            if len(parts) < 5:
                print("  Использование: /mcp build python|node <имя> <описание>")
                print("  Пример: /mcp build python my_tool 'Мой инструмент для FreePalp'")
            else:
                srv_name = parts[3]
                desc = " ".join(parts[4:])
                if lang in ("node", "js", "nodejs"):
                    result = build_node_mcp(srv_name, desc)
                else:
                    result = build_python_mcp(srv_name, desc)
                if result["ok"]:
                    print(f"\n  ✅ MCP-сервер создан: {result['path']}")
                    print(f"  Файлы:")
                    for f in result["files"]:
                        print(f"    • {f}")
                    print(f"\n  Следующий шаг:")
                    if lang in ("node", "js"):
                        print(f"    cd {result['path']} && npm install && npm start")
                    else:
                        print(f"    pip install fastmcp && python {result['path']}/server.py")
                    print(f"  Затем добавьте в .mcp.json и /mcp list покажет сервер активным")
                else:
                    print(f"  ❌ Ошибка: {result.get('error')}")
        else:
            print("  Команды: /mcp list | /mcp template | /mcp build python|node <имя> <описание>")

    elif command == "/skill":
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "create":
            # /skill create <name> <description>
            if len(parts) < 4:
                print("  Использование: /skill create <имя> <описание>")
                print("  Пример: /skill create translator 'Переводчик текста на любой язык'")
            else:
                skill_name = parts[2].lower().replace(" ", "_")
                description = " ".join(parts[3:])
                _create_skill(skill_name, description)
        elif sub == "list":
            skills_dir = Path(__file__).parent / "skills"
            skill_files = [f for f in skills_dir.glob("*.py") if not f.name.startswith("__")] if skills_dir.exists() else []
            if not skill_files:
                print("  Нет пользовательских навыков. Создайте: /skill create <имя> <описание>")
            else:
                print(f"\n  Навыки ({len(skill_files)}):")
                for f in skill_files:
                    print(f"  • {f.stem}")
        else:
            print("  Команды: /skill list | /skill create <имя> <описание>")

    elif command == "/tools":
        tools = orchestrator.tool_agent.list_tools()
        print(f"\n🔧 Доступные инструменты ({len(tools)}):")
        for t in tools:
            print(f"  • {t}")

    elif command == "/tool" and len(parts) >= 2:
        tool_name = parts[1]
        kwargs = {}
        for kv in parts[2:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                kwargs[k] = v
        print(f"\n🔧 Выполняю {tool_name}({kwargs})...")
        result = await orchestrator.use_tool(tool_name, **kwargs)
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))

    elif command == "/search" and len(parts) >= 2:
        query = " ".join(parts[1:])
        print(f"\n🔍 Поиск: {query}...")
        result = await orchestrator.use_tool("web_search", query=query)
        if result.get("ok"):
            for r in result.get("results", []):
                print(f"\n  📎 {r['title']}")
                print(f"     {r['url']}")
                if r.get("snippet"):
                    print(f"     {r['snippet'][:100]}...")
        else:
            print(f"  ❌ {result.get('error')}")

    elif command == "/file" and len(parts) >= 2:
        path = parts[1]
        result = await orchestrator.use_tool("read_file", path=path)
        if result.get("ok"):
            print(f"\n--- {path} ---\n{result['content']}")
        else:
            print(f"Error: {result.get('error')}")

    elif command == "/memory":
        sub = parts[1] if len(parts) > 1 else ""
        if sub == "stats":
            stats = orchestrator.memory.get_stats()
            hb = stats.get('last_heartbeat')
            if hb:
                from datetime import datetime
                try:
                    delta = datetime.now() - datetime.fromisoformat(hb)
                    hb_str = f"{hb[:16]}  ({int(delta.total_seconds()//3600)}ч назад)"
                except Exception:
                    hb_str = hb
            else:
                hb_str = "не запускался"
            print(f"\n--- Memory Stats ---")
            print(f"  [HOT]  строк        : {stats['hot_lines']:3} / 100")
            print(f"  [HOT]  corrections  : {stats['corrections']:3} / 50")
            print(f"  [WARM] сессий       : {stats['sessions']}")
            print(f"  [WARM] проектов     : {stats['projects']}")
            print(f"  [WARM] demoted файлов: {stats['warm_files']}")
            print(f"  [COLD] архивов      : {stats['archived']}")
            print(f"  Heartbeat           : {hb_str}")
        elif sub == "clean":
            report = orchestrator.memory.maintenance()
            print(f"\n--- Memory Cleanup ---")
            print(f"  HOT  demoted   : {report['hot_trimmed']} строк → warm/")
            print(f"  Corrections    : {report['corrections_archived']} → archive/")
            print(f"  Sessions       : {report['sessions_archived']} → archive/")
        elif sub == "export":
            path = parts[2] if len(parts) >= 3 else None
            out = orchestrator.memory.export_zip(path)
            print(f"\n  Экспорт создан: {out}")
        elif sub == "search":
            if len(parts) < 3:
                print("  Использование: /memory search <запрос>")
            else:
                query = " ".join(parts[2:])
                results = orchestrator.memory.search_cold(query)
                if results:
                    print(f"\n  Найдено {len(results)} совпадений в архиве:")
                    for r in results:
                        print(f"\n  [{r['tier'].upper()}] {r['source']}")
                        print(f"  {r['snippet']}")
                else:
                    print(f"  Ничего не найдено в архиве по запросу: '{query}'")
        else:
            hot = orchestrator.memory.load_hot()
            print(f"\n--- HOT Memory ({len(hot.splitlines())} строк) ---")
            print(hot or "(пусто)")
            corrections = orchestrator.memory.get_recent_corrections(3)
            if corrections:
                print(f"\n--- Последние исправления ---")
                for c in corrections:
                    print(c[:200])

    elif command == "/cron":
        cron = orchestrator.cron
        sub = parts[1] if len(parts) > 1 else "list"

        if sub == "list":
            items = cron.list_crons()
            if not items:
                print("\n  Нет cron-задач.")
            else:
                from datetime import datetime
                now = datetime.now()
                print(f"\n{'─'*60}")
                print(f"  {'ID':10} {'Интервал':10} {'Следующий запуск':20} {'Запусков':8} {'Имя'}")
                print(f"{'─'*60}")
                for c in items:
                    next_r = c.get("next_run", "—")
                    try:
                        dt = datetime.fromisoformat(next_r)
                        delta = dt - now
                        total_s = int(delta.total_seconds())
                        if total_s < 0:
                            next_str = "ПРОСРОЧЕНА"
                        elif total_s < 3600:
                            next_str = f"через {total_s//60}м"
                        elif total_s < 86400:
                            next_str = f"через {total_s//3600}ч"
                        else:
                            next_str = f"через {total_s//86400}д"
                    except Exception:
                        next_str = next_r[:16] if next_r else "—"
                    interval = f"{c['interval_h']}ч"
                    builtin_tag = " [встроенная]" if c.get("builtin") else ""
                    print(f"  {c['id']:10} {interval:10} {next_str:20} {c.get('run_count',0):8} {c['name']}{builtin_tag}")

        elif sub == "add":
            # /cron add "2ч" "my command"  или  /cron add имя 2ч команда
            rest = cmd.strip()[len("/cron add"):].strip()
            import shlex
            try:
                tokens = shlex.split(rest)
            except Exception:
                tokens = rest.split()
            if len(tokens) < 2:
                print("  Использование: /cron add \"имя\" \"интервал\" \"команда\"")
                print("  Пример: /cron add \"Бэкап\" \"1д\" \"backup_db\"")
            else:
                if len(tokens) == 2:
                    name, interval_str, command_str = tokens[0], tokens[0], tokens[1]
                    name = f"Задача {interval_str}"
                else:
                    name, interval_str, command_str = tokens[0], tokens[1], tokens[2]
                result = cron.add(name, interval_str, command_str)
                if result:
                    print(f"  ✅ Задача добавлена: [{result['id']}] {result['name']} каждые {result['interval_h']}ч")
                else:
                    print(f"  ❌ Не удалось распознать интервал: '{interval_str}'")
                    print(f"     Форматы: 30м, 2ч, 1д, 1w, каждый день, каждую неделю")

        elif sub == "remove" and len(parts) >= 3:
            cron_id = parts[2]
            ok = cron.remove(cron_id)
            if ok:
                print(f"  ✅ Задача {cron_id} удалена")
            else:
                print(f"  ❌ Задача {cron_id} не найдена или является встроенной")

        elif sub == "run" and len(parts) >= 3:
            cron_id = parts[2]
            items = cron.list_crons()
            target = next((c for c in items if c["id"] == cron_id), None)
            if not target:
                print(f"  ❌ Задача {cron_id} не найдена")
            else:
                cmd_str = target["command"]
                handlers = {
                    "__memory_cleanup__": orchestrator._cron_memory_cleanup,
                    "__weekly_digest__":  orchestrator._cron_weekly_digest,
                }
                handler = handlers.get(cmd_str)
                if handler:
                    print(f"  ▶ Запускаю: {target['name']}...")
                    await handler()
                    cron.mark_run(cron_id)
                    print(f"  ✅ Выполнено")
                else:
                    print(f"  ⚠️  Нет обработчика для команды: {cmd_str}")
                    print(f"     (Встроенные: __memory_cleanup__, __weekly_digest__)")
        else:
            print("  Команды: /cron list | /cron add | /cron remove <id> | /cron run <id>")

    elif command == "/forget" and len(parts) >= 2:
        keyword = " ".join(parts[1:])
        removed = orchestrator.memory.forget(keyword)
        if removed:
            print(f"\n  Удалено {removed} строк из HOT памяти содержащих: '{keyword}'")
        else:
            print(f"\n  Ничего не найдено в HOT памяти по запросу: '{keyword}'")

    elif command == "/history":
        from freepalp.core.session_logger import search_sessions, get_session_stats
        if len(parts) >= 2:
            query = " ".join(parts[1:])
            results = search_sessions(query)
            if results:
                print(f"\nFound in sessions:")
                for r in results:
                    print(f"  Session: {r['session']}")
                    for m in r['matches'][:2]:
                        print(f"  [{m['role']}] {m['snippet'][:120]}")
            else:
                print(f"  Nothing found for: {query}")
        else:
            stats = get_session_stats()
            print(f"\nSession history: {stats['total_sessions']} sessions, {stats['total_messages']} messages")

    elif command == "/user":
        if len(parts) >= 4 and parts[1] == "set":
            field = parts[2]
            value = " ".join(parts[3:])
            orchestrator.user_profile.set_field(field, value)
            print(f"  Saved: {field} = {value}")
        else:
            profile = orchestrator.user_profile.load()
            print(f"\n--- USER.md ---\n{profile}")

    elif command == "/improve":
        si = orchestrator.self_improvement
        sub = parts[1] if len(parts) > 1 else ""

        if sub == "status":
            status = si.status()
            print(f"\n--- Self-Improvement Status ---")
            print(f"  Active version : v{status['current_version']}")
            print(f"  Versions total : {status['versions_available']}")
            m = status["metrics"]
            print(f"  Tasks analyzed : {m.get('total', 0)}")
            if m.get('total', 0) > 0:
                print(f"  Global avg score : {m.get('avg_score', 0):.3f}")
                print(f"  Retry rate       : {m.get('retry_rate', 0):.0%}")
                total_tokens = m.get('total_tokens', 0)
                total_cost   = m.get('total_cost_usd', 0.0)
                if total_tokens > 0:
                    print(f"  Total tokens     : {total_tokens:,}")
                    print(f"  Total cost (USD) : ${total_cost:.4f}")
                if m.get("by_type"):
                    print(f"  By type:")
                    for t, s in m["by_type"].items():
                        bar = "█" * int(s * 10) + "░" * (10 - int(s * 10))
                        print(f"    {t:15} [{bar}] {s:.3f}")
            print(f"\n  Candidates for improvement: {status['improvement_candidates']}")
            for c in status.get("candidates_preview", []):
                print(f"    • {c}")
            if not status["ready_to_improve"]:
                print(f"\n  Run more tasks to accumulate data, then /improve")
        else:
            print(f"\n🧬 Запускаю цикл самоулучшения...")
            report = await si.run(force=(sub == "force"))
            print(f"\n--- Результат самоулучшения ---")
            if report.get("error"):
                print(f"  ⚠ {report['error']}")
            else:
                print(f"  Предложена версия : v{report.get('version_proposed', '?')}")
                print(f"  Тесты прошли     : {'✓' if report['test_passed'] else '✗'}")
                print(f"  Активирована     : {'✓' if report['version_activated'] else '✗'}")
                if report.get("rollback"):
                    print(f"  Откат применён   : ✓ (тесты не прошли, осталась старая версия)")
                if report.get("changes"):
                    print(f"  Изменения ({len(report['changes'])}):")
                    for ch in report["changes"]:
                        comp = ch["component"]
                        tt = ch.get("task_type", "all")
                        reason = ch.get("reason", "")[:60]
                        print(f"    • {comp}[{tt}]: {reason}")
                if not report["test_passed"] and report.get("test_output"):
                    print(f"\n  Вывод тестов (последние 300 символов):")
                    print(f"  {report['test_output'][-300:]}")

    elif command == "/versions":
        vm = orchestrator.self_improvement.vm
        current = vm.current_version()
        versions = vm.list_versions()
        print(f"\n--- Версии конфига ---")
        print(f"  Активная: v{current}")
        if not versions:
            print(f"  История версий пуста (улучшений ещё не было)")
        else:
            for v in reversed(versions[-10:]):
                status_icon = "▶" if v["version"] == current else " "
                test_icon = "✓" if v.get("test_passed") else ("✗" if v.get("test_passed") is False else "?")
                activated = "активирована" if v.get("status") == "active" else v.get("status", "")
                print(f"  {status_icon} v{v['version']}  test={test_icon}  {activated}")
                if v.get("changes"):
                    print(f"      Изменения: {v['changes'][:70]}")

    elif command == "/version" and len(parts) >= 2:
        sub = parts[1]
        vm = orchestrator.self_improvement.vm
        if sub == "rollback":
            success, msg = vm.rollback()
            if success:
                print(f"  ✓ Откат: {msg}")
                from freepalp.core import prompt_loader
                prompt_loader.reload()
                print(f"  Конфиг перезагружен. Активна: v{vm.current_version()}")
            else:
                print(f"  ✗ Ошибка отката: {msg}")
        elif sub == "activate" and len(parts) >= 3:
            ver = parts[2].lstrip("v")
            ok = vm.activate(ver)
            print(f"  {'✓ Активирована' if ok else '✗ Ошибка активации'} v{ver}")
        else:
            print(f"  Использование: /version rollback  или  /version activate N")

    else:
        print(f"Unknown command: {command}. Try /help")

    return True


async def interactive_mode(orchestrator: Orchestrator):
    """Интерактивный режим — REPL."""
    print(BANNER)

    try:
        while True:
            try:
                user_input = input("\n> ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\n👋 До свидания!")
                break

            if not user_input:
                continue

            if user_input.startswith("/"):
                should_continue = await handle_command(orchestrator, user_input)
                if not should_continue:
                    break
            else:
                try:
                    await run_task(orchestrator, user_input)
                except KeyboardInterrupt:
                    # Ctrl+C во время задачи — прерываем только задачу, не сессию
                    print("\n  [STOP] Задача прервана пользователем (Ctrl+C). Сессия продолжается.")
    finally:
        # Graceful shutdown — всегда, даже при исключении или Ctrl+C
        try:
            await orchestrator.stop()
        except Exception:
            pass


async def main():
    parser = argparse.ArgumentParser(
        description="FreePalp AI Orchestrator — Multi-agent AI with ReAct loop",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "task", nargs="?", help="Задача для выполнения (без аргумента — интерактивный режим)"
    )
    parser.add_argument("--models",  action="store_true", help="Список моделей")
    parser.add_argument("--gateway", action="store_true", help="Запустить HTTP Gateway на порту 28800")
    parser.add_argument("--web",     action="store_true", help="Запустить WebUI на порту 28800")
    parser.add_argument("--tool", help="Вызвать инструмент: --tool read_file path=main.py")

    args = parser.parse_args()

    # Запуск WebUI / Gateway
    if args.gateway or args.web:
        from freepalp.gateway import run_gateway
        run_gateway()
        return

    # Инициализация
    print("Initializing FreePalp...")
    orchestrator = Orchestrator()

    # Загружаем дайджест прошлых сессий в HOT память
    try:
        from freepalp.memory.session_memory import get_or_build_digest
        from freepalp.memory.memory_manager import MEMORY_ROOT
        digest = get_or_build_digest()
        if digest:
            from freepalp.gateway import _inject_digest_into_hot
            _inject_digest_into_hot(MEMORY_ROOT / "hot_memory.md", digest)
            print(f"  [Memory] Loaded session digest ({len(digest.splitlines())} lines)")
    except Exception:
        pass

    if args.models:
        models = orchestrator.get_available_models()
        print(f"\nModels ({len(models)}):")
        for m in models:
            print(f"  {m['name']:25} {m['tier']:15} [{m['provider']}]")
        return

    if args.tool:
        parts = args.tool.split()
        tool_name = parts[0]
        kwargs = {}
        for kv in parts[1:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                kwargs[k] = v
        result = await orchestrator.use_tool(tool_name, **kwargs)
        import json
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    if args.task:
        try:
            await run_task(orchestrator, args.task)
        finally:
            await orchestrator.stop()
    else:
        await interactive_mode(orchestrator)  # stop() вызывается внутри


def cli():
    """Точка входа консольной команды `freepalp` (см. pyproject.toml).
    По умолчанию поднимает WebUI; CLI-режим — через подкоманду/флаг task."""
    # Без аргументов или с --web/--gateway → веб-интерфейс (массовый сценарий)
    if len(sys.argv) == 1 or "--web" in sys.argv or "--gateway" in sys.argv:
        from freepalp.gateway import run_gateway
        run_gateway()
    else:
        asyncio.run(main())


if __name__ == "__main__":
    # Check for --web / --gateway BEFORE asyncio.run to avoid nested event loop
    if "--web" in sys.argv or "--gateway" in sys.argv:
        from freepalp.gateway import run_gateway
        run_gateway()
    else:
        asyncio.run(main())

