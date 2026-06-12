"""
OCTO — Octopus Cognitive Task Orchestrator
Автоматический установщик первого запуска.

python setup.py
"""

import sys
import os
import subprocess
import shutil
import json
import platform
from pathlib import Path

# ─────────────────────────────────────────────
ROOT = Path(__file__).parent
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
REQUIREMENTS = ROOT / "requirements.txt"
FLAG_FILE = ROOT / ".octo_setup_done"
STATE_DIR = ROOT / "octo" / "state"
SANDBOX_DIR = ROOT / "octo" / "sandbox"
MEMORY_DIR = ROOT / "octo" / "memory"

BANNER = r"""
   ___   __________  ____
  / _ \ / ___/_  __// __ \
 / // // /__  / /  / /_/ /
/____/ \___/ /_/   \____/

  Octopus Cognitive Task Orchestrator
  Setup Wizard v0.1
"""

GROQ_URL = "https://console.groq.com/keys"

# Минимальные версии
MIN_PYTHON = (3, 10)
CORE_PACKAGES = ["httpx", "groq", "python-dotenv"]
OPTIONAL_PACKAGES = ["anthropic", "fastapi", "uvicorn"]
# ─────────────────────────────────────────────


def print_step(n: int, total: int, text: str):
    bar = "█" * n + "░" * (total - n)
    print(f"\n[{bar}] Шаг {n}/{total}: {text}")


def ok(msg=""):    print(f"  ✓  {msg}")
def warn(msg=""):  print(f"  !  {msg}")
def fail(msg=""):  print(f"  ✗  {msg}")
def info(msg=""):  print(f"     {msg}")


# ══════════════════════════════════════════════
# ШАГ 1 — Python версия
# ══════════════════════════════════════════════
def check_python() -> bool:
    v = sys.version_info
    if v < MIN_PYTHON:
        fail(f"Python {v.major}.{v.minor} слишком старый.")
        fail(f"Нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+")
        info("Скачай: https://www.python.org/downloads/")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    return True


# ══════════════════════════════════════════════
# ШАГ 2 — pip + зависимости
# ══════════════════════════════════════════════
def install_dependencies() -> bool:
    print()
    info(f"Файл: {REQUIREMENTS}")

    # Обновить pip молча
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "--upgrade", "pip", "-q"],
        capture_output=True
    )

    # Установить requirements
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS), "-q"],
        capture_output=True, text=True
    )

    if result.returncode != 0:
        warn("pip install завершился с ошибками:")
        # Показать только ошибки
        for line in result.stderr.split("\n"):
            if "error" in line.lower() or "ERROR" in line:
                info(f"  {line}")
        info("Попробуем установить core пакеты по одному...")

        failed = []
        for pkg in CORE_PACKAGES:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg, "-q"],
                capture_output=True
            )
            if r.returncode == 0:
                ok(f"  {pkg}")
            else:
                fail(f"  {pkg}")
                failed.append(pkg)

        if failed:
            fail(f"Не удалось установить: {', '.join(failed)}")
            return False
    else:
        ok("Все зависимости установлены")

    # Проверить core импорты
    missing = []
    for pkg in ["httpx", "dotenv", "groq"]:
        try:
            __import__(pkg.replace("-", "_"))
        except ImportError:
            missing.append(pkg)

    if missing:
        warn(f"Не загружаются: {', '.join(missing)}")
        warn("Возможно нужен: pip install " + " ".join(missing))
        return False

    return True


# ══════════════════════════════════════════════
# ШАГ 3 — .env настройка
# ══════════════════════════════════════════════
def setup_env() -> bool:
    if ENV_FILE.exists():
        ok(".env уже существует")
        # Проверить что там есть
        content = ENV_FILE.read_text(encoding="utf-8")
        has_groq = "GROQ_API_KEY=" in content and "gsk_" in content
        has_anthropic = "ANTHROPIC_API_KEY=" in content and "sk-ant-" in content

        if has_groq:
            ok("GROQ_API_KEY — найден")
        else:
            warn("GROQ_API_KEY — не задан (работа на локальных моделях)")

        if has_anthropic:
            ok("ANTHROPIC_API_KEY — найден")

        return True

    # Создать из шаблона
    if ENV_EXAMPLE.exists():
        shutil.copy(ENV_EXAMPLE, ENV_FILE)
        ok(".env создан из .env.example")
    else:
        ENV_FILE.write_text(
            "GROQ_API_KEY=\nANTHROPIC_API_KEY=\n",
            encoding="utf-8"
        )
        ok(".env создан")

    # ── Интерактивный ввод ключей ──────────────
    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │  Настройка API ключей                   │")
    print("  └─────────────────────────────────────────┘")
    print()
    print("  Для облачных моделей (Groq) нужен API ключ.")
    print("  Без него система работает только на Ollama.")
    print()
    print(f"  Получить БЕСПЛАТНО: {GROQ_URL}")
    print("  (500,000 токенов/день бесплатно)")
    print()

    try:
        raw = input("  Вставь GROQ_API_KEY (Enter — пропустить): ").strip().strip('"').strip("'")
    except (KeyboardInterrupt, EOFError):
        raw = ""

    if raw.startswith("gsk_") or raw.startswith("groq_"):
        _set_env_var("GROQ_API_KEY", raw)
        ok("GROQ_API_KEY сохранён!")
    elif raw and len(raw) > 10:
        warn(f"Ключ выглядит необычным (ожидается gsk_...)")
        _set_env_var("GROQ_API_KEY", raw)
        warn("Сохранён как есть — проверь позже в .env")
    else:
        warn("Пропущено. Система будет использовать только Ollama")
        info("Можно добавить позже: открой .env и вставь GROQ_API_KEY=gsk_...")

    # Anthropic опционально
    print()
    try:
        raw_ant = input("  Anthropic API key (необязательно, Enter — пропустить): ").strip().strip('"').strip("'")
    except (KeyboardInterrupt, EOFError):
        raw_ant = ""

    if raw_ant.startswith("sk-ant-"):
        _set_env_var("ANTHROPIC_API_KEY", raw_ant)
        ok("ANTHROPIC_API_KEY сохранён!")

    return True


def _set_env_var(key: str, value: str):
    """Записывает/обновляет переменную в .env файле."""
    content = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    lines = content.splitlines()
    updated = False
    new_lines = []
    for line in lines:
        if line.startswith(f"{key}="):
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(new_lines) + "\n", encoding="utf-8")


# ══════════════════════════════════════════════
# ШАГ 4 — Ollama
# ══════════════════════════════════════════════
def check_ollama() -> bool:
    ollama_path = shutil.which("ollama")

    if not ollama_path:
        warn("Ollama не найден в PATH")
        info("Для локальных моделей установи Ollama:")
        if platform.system() == "Windows":
            info("  https://ollama.ai  → Download for Windows")
        elif platform.system() == "Darwin":
            info("  brew install ollama  или  https://ollama.ai")
        else:
            info("  curl -fsSL https://ollama.ai/install.sh | sh")
        info("Система будет работать без Ollama (только облако)")
        return False

    ok(f"Ollama найден: {ollama_path}")

    # Проверить что сервер запущен
    try:
        import httpx
        resp = httpx.get("http://localhost:11434/api/tags", timeout=3.0)
        if resp.status_code == 200:
            models_data = resp.json()
            installed = [m["name"] for m in models_data.get("models", [])]
            if installed:
                ok(f"Ollama сервер запущен. Моделей: {len(installed)}")
                for m in installed[:5]:
                    info(f"  • {m}")
                return True
            else:
                warn("Ollama запущен, но моделей нет")
                _offer_model_pull()
                return True
    except Exception:
        warn("Ollama установлен, но сервер не запущен")
        info("Запусти: ollama serve")
        info("Затем в другом терминале: ollama pull qwen2.5-coder:7b")
        return False

    return True


def _offer_model_pull():
    """Предлагает скачать модели."""
    models = [
        ("qwen2.5-coder:7b", "~4.7GB", "Лучший для кода"),
        ("mistral:7b",        "~4.1GB", "Быстрый универсальный"),
    ]
    print()
    print("  Доступные модели для установки:")
    for i, (name, size, desc) in enumerate(models, 1):
        print(f"  {i}. {name:25} {size:8}  — {desc}")
    print()

    try:
        choice = input("  Скачать модель? (1/2/Enter — пропустить): ").strip()
    except (KeyboardInterrupt, EOFError):
        return

    if choice in ("1", "2"):
        model_name = models[int(choice) - 1][0]
        info(f"Запускаю: ollama pull {model_name}")
        info("Это может занять несколько минут...")
        result = subprocess.run(["ollama", "pull", model_name])
        if result.returncode == 0:
            ok(f"{model_name} установлен!")
        else:
            warn(f"Ошибка при загрузке {model_name}")


# ══════════════════════════════════════════════
# ШАГ 5 — Директории и структура
# ══════════════════════════════════════════════
def create_dirs() -> bool:
    dirs = [
        ROOT / "octo" / "state",
        ROOT / "octo" / "sandbox",
        ROOT / "octo" / "memory" / "sessions",
        ROOT / "octo" / "memory" / "projects",
        ROOT / "octo" / "memory" / "corrections",
        ROOT / "octo" / "memory" / "patterns",
        ROOT / "octo" / "logs",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    ok(f"Директории созданы")

    # Создать memory/memory.md (HOT tier как в QClaw self-improving)
    hot_memory = ROOT / "octo" / "memory" / "memory.md"
    if not hot_memory.exists():
        hot_memory.write_text(
            "# OCTO HOT Memory\n\n"
            "_Постоянно загруженная память агента. Максимум 100 строк._\n\n"
            "## Предпочтения пользователя\n\n"
            "## Паттерны\n\n"
            "## Важные правила\n",
            encoding="utf-8"
        )

    # Создать corrections.md
    corrections = ROOT / "octo" / "memory" / "corrections.md"
    if not corrections.exists():
        corrections.write_text(
            "# Corrections Log\n\n"
            "_Последние 50 исправлений и уроков_\n",
            encoding="utf-8"
        )

    return True


# ══════════════════════════════════════════════
# ШАГ 6 — Тесты
# ══════════════════════════════════════════════
def run_tests() -> bool:
    test_file = ROOT / "test_mvp.py"
    if not test_file.exists():
        warn("test_mvp.py не найден — пропускаю тесты")
        return True

    info("Запускаю тесты...")
    result = subprocess.run(
        [sys.executable, str(test_file)],
        capture_output=True, text=True,
        cwd=str(ROOT),
        env={**os.environ, "PYTHONUTF8": "1"}
    )

    if result.returncode == 0:
        ok("Все тесты прошли!")
        # Вывести краткий итог
        for line in result.stdout.split("\n"):
            if "Итого" in line or "PASSED" in line or "FAILED" in line:
                info(f"  {line.strip()}")
        return True
    else:
        warn("Некоторые тесты не прошли:")
        for line in result.stdout.split("\n")[-15:]:
            if line.strip():
                info(f"  {line}")
        return False


# ══════════════════════════════════════════════
# ШАГ 7 — Создать лаунчер
# ══════════════════════════════════════════════
def create_launcher():
    """Создаёт start.bat / start.sh для быстрого запуска."""

    # Windows .bat
    bat = ROOT / "start.bat"
    bat.write_text(
        "@echo off\n"
        "chcp 65001 >nul\n"
        "title OCTO — Octopus AI Orchestrator\n"
        "cd /d \"%~dp0\"\n"
        "\n"
        "if not exist .env (\n"
        "    python setup.py\n"
        "    if errorlevel 1 pause && exit /b 1\n"
        ")\n"
        "\n"
        "set PYTHONUTF8=1\n"
        "python octo\\app.py %*\n"
        "\n"
        "if errorlevel 1 (\n"
        "    echo.\n"
        "    echo [Ошибка запуска. Нажми любую клавишу...]\n"
        "    pause >nul\n"
        ")\n",
        encoding="utf-8"
    )
    ok("start.bat создан")

    # PowerShell launcher
    ps1 = ROOT / "start.ps1"
    ps1.write_text(
        "$env:PYTHONUTF8 = '1'\n"
        "$host.UI.RawUI.WindowTitle = 'OCTO - Octopus AI Orchestrator'\n"
        "Set-Location $PSScriptRoot\n"
        "\n"
        "if (-not (Test-Path '.env')) {\n"
        "    python setup.py\n"
        "    if ($LASTEXITCODE -ne 0) { Read-Host 'Setup failed. Press Enter'; exit 1 }\n"
        "}\n"
        "\n"
        "python octo\\app.py $args\n",
        encoding="utf-8"
    )
    ok("start.ps1 создан")

    # Linux/Mac shell script
    sh = ROOT / "start.sh"
    sh.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        'cd "$(dirname "$0")"\n'
        "\n"
        "if [ ! -f .env ]; then\n"
        "    python3 setup.py\n"
        "fi\n"
        "\n"
        "export PYTHONUTF8=1\n"
        "python3 octo/app.py \"$@\"\n",
        encoding="utf-8"
    )
    try:
        sh.chmod(0o755)
    except Exception:
        pass
    ok("start.sh создан")


# ══════════════════════════════════════════════
# ШАГ 8 — Статус провайдеров
# ══════════════════════════════════════════════
def show_providers_status():
    """Показывает финальный статус всех провайдеров."""
    print()
    print("  ┌─────────────────────────────────────────┐")
    print("  │  Статус провайдеров                     │")
    print("  └─────────────────────────────────────────┘")

    # Загрузить .env
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

    providers = [
        ("Ollama (local)",   _check_ollama_status,   "Без ключей"),
        ("Groq (cloud 70B)", _check_groq_status,     GROQ_URL),
        ("Anthropic",        _check_anthropic_status, "console.anthropic.com"),
    ]

    for name, check_fn, url in providers:
        status, detail = check_fn()
        symbol = "✓" if status else "○"
        line = f"  {symbol}  {name:22} {detail}"
        if not status:
            line += f"\n       → {url}"
        print(line)


def _check_ollama_status():
    try:
        import httpx
        resp = httpx.get("http://localhost:11434/api/tags", timeout=2.0)
        models = resp.json().get("models", [])
        return True, f"запущен, {len(models)} моделей"
    except Exception:
        return False, "не запущен (ollama serve)"


def _check_groq_status():
    key = os.environ.get("GROQ_API_KEY", "")
    # Исключить placeholder из .env.example
    is_real = (
        (key.startswith("gsk_") or key.startswith("groq_"))
        and len(key) > 20
        and "xxx" not in key
    )
    if is_real:
        return True, f"ключ задан ({key[:8]}...)"
    return False, "нет ключа"


def _check_anthropic_status():
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    is_real = key.startswith("sk-ant-") and len(key) > 20 and "xxx" not in key
    if is_real:
        return True, f"ключ задан ({key[:12]}...)"
    return False, "нет ключа (опционально)"


# ══════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════
def main():
    print(BANNER)

    # Если уже настроено
    if FLAG_FILE.exists() and "--force" not in sys.argv:
        print("  Setup уже был выполнен ранее.")
        print("  Для повторного запуска: python setup.py --force")
        print()
        show_providers_status()
        print()
        print("  Запуск: python octo/app.py")
        return True

    steps = [
        ("Проверка Python",         check_python),
        ("Установка зависимостей",  install_dependencies),
        ("Настройка .env",          setup_env),
        ("Проверка Ollama",         check_ollama),
        ("Создание директорий",     create_dirs),
        ("Запуск тестов",           run_tests),
        ("Создание лаунчеров",      create_launcher),
    ]
    TOTAL = len(steps)

    all_ok = True
    for i, (name, fn) in enumerate(steps, 1):
        print_step(i, TOTAL, name)
        try:
            result = fn()
            if result is False:
                all_ok = False
                warn(f"Шаг '{name}' не прошёл — продолжаем")
        except KeyboardInterrupt:
            print("\n\n  Прервано пользователем")
            return False
        except Exception as e:
            warn(f"Ошибка на шаге '{name}': {e}")
            all_ok = False

    # Показать статус провайдеров
    show_providers_status()

    # Итог
    print()
    print("  " + "═" * 44)
    if all_ok:
        FLAG_FILE.write_text("setup complete", encoding="utf-8")
        print("  ✓  Setup завершён!")
        print()
        print("  Запуск:")
        if platform.system() == "Windows":
            print("    start.bat                   ← интерактивный режим")
            print("    start.bat \"твоя задача\"      ← одна задача")
        else:
            print("    ./start.sh")
        print()
        print("    python octo/app.py")
    else:
        print("  !  Setup завершён с предупреждениями")
        print("     Проверь сообщения выше и запусти setup.py снова")
        print()
        print("  Всё равно попробуй запустить:")
        print("    python octo/app.py")
    print("  " + "═" * 44)

    return all_ok


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
