<p align="center">
  <img src="freepalp/web/static/freepalp-logo.svg" alt="FreePalp" width="520">
</p>

> 🇬🇧 English version: [README.md](README.md)

# FreePalp AI Orchestrator

**Мультиагентный AI-оркестратор на бесплатных моделях: самокоррекция, ReAct loop, автономное использование инструментов.**

```
User Input
    ↓
Task Parser  →  определяет тип задачи (coding / research / text / ...)
    ↓
Router       →  live discovery: 40+ моделей, 8 провайдеров
    ↓
[Architect]  →  планирование сложных задач (DAG)
    ↓
Worker       →  выполняет через LLM + ReAct loop (вызывает инструменты сам)
    ↓
Critic       →  проверяет качество (score 0–1)
    ↓ retry если score < 0.7 (max 3 итерации)
Result
```

## Быстрый старт

### 1. Установка

```bash
pip install -r requirements.txt
```

### 2. Настройка .env

```bash
cp .env.example .env
```

Минимум — достаточно одного ключа:

| Провайдер | Переменная | Ключ |
|-----------|-----------|------|
| **Groq** (рекомендую) | `GROQ_API_KEY` | [console.groq.com](https://console.groq.com/keys) — бесплатно |
| OpenRouter | `OPENROUTER_API_KEY` | [openrouter.ai](https://openrouter.ai) — бесплатно |
| Cerebras | `CEREBRAS_API_KEY` | [inference.cerebras.ai](https://inference.cerebras.ai) — бесплатно |
| Gemini | `GEMINI_API_KEY` | [aistudio.google.com](https://aistudio.google.com/apikey) — бесплатно |
| SambaNova | `SAMBANOVA_API_KEY` | [cloud.sambanova.ai](https://cloud.sambanova.ai) — бесплатно |
| GitHub Models | `GITHUB_TOKEN` | [github.com/settings/tokens](https://github.com/settings/tokens) — бесплатно |
| Ollama (локально) | — | [ollama.ai](https://ollama.ai) — установить локально |

### 3. Запуск

```bash
# CLI — интерактивный режим
python freepalp/app.py

# Одна задача
python freepalp/app.py "напиши async HTTP клиент на Python"

# WebUI (Neo-minimal интерфейс)
python freepalp/app.py --web
```

---

## Что умеет FreePalp

### 🤖 ReAct Loop — агент сам вызывает инструменты

Worker не просто генерирует текст — он **автономно работает** с инструментами:

```
Задача: "найди топ issues в репо microsoft/vscode и создай дайджест"

Worker думает → вызывает github_list_issues() → получает данные
             → вызывает memory_write() → сохраняет результат
             → формирует финальный ответ
```

### 🛠️ 41 инструмент из коробки

| Категория | Инструменты |
|-----------|-------------|
| **Файлы** | read_file, write_file, list_files, delete_file |
| **Shell** | run_command (whitelist) |
| **Web** | web_search, fetch_url |
| **Браузер** | browser_open, browser_click, browser_fill, browser_screenshot, browser_extract, browser_eval |
| **GitHub** | github_get_repo, github_list_issues, github_create_issue, github_get_file, github_create_file, github_list_prs, github_search_code, github_get_commits |
| **Уведомления** | email_send, slack_send, notion_create, notion_search, notion_get_page |
| **Система** | memory_read, memory_write, memory_search, memory_forget, cron_list, cron_add, cron_remove, providers_list, models_list, mcp_list, skills_list, skill_run, metrics_summary |

### 🧠 Трёхуровневая память

- **HOT** — активная память, до 100 строк, инжектируется в каждый промпт
- **WARM** — авто-демоция после 30 дней без обращений
- **COLD** — архив с полнотекстовым поиском (`/memory search <запрос>`)

### 📈 Самоулучшение

- Метрики по каждой задаче (score, tokens, cost, retry rate)
- Авто-цикл улучшения каждые 10 задач
- Версионирование конфига с откатом (`/version rollback`)
- Еженедельный дайджест (cron)

### ✨ Что нового

- **Teacher→skill** — успешный ретрай сохраняется в переиспользуемый `SKILL.md`
  (формат Claude Code) и подставляется в промпт при похожей задаче: дешёвая
  модель справляется сразу. Коррекции накапливаются, а не сгорают.
- **MCP-клиент** — любой [MCP](https://modelcontextprotocol.io)-сервер из
  `config/mcp_servers.json` подключается, и его инструменты появляются у агента
  автоматически (файловая система, GitHub, БД — сотни готовых).
- **Токен-стриминг** — финальный ответ печатается токен-за-токеном в WebUI.
- **Deep Research** — один инструмент делает многоугловой веб-поиск, грузит
  топ-страницы, и агент пишет отчёт с **реальными** источниками (детерминированный
  триггер не даёт выдумывать ссылки из обучения).
- **Артефакты-превью** — HTML/игры, созданные агентом, открываются и играются
  прямо в чате (изолированный iframe).
- **Поиск по истории (FTS5)** — поиск по всем прошлым диалогам: «когда мы
  обсуждали X».
- **Автозапуск Ollama** — поднимается при старте, если была подключена раньше.

---

## CLI Команды

```
/help              — справка
/models            — 40+ моделей (live discovery)
/providers         — все провайдеры: статус, лимиты, ссылки
/cron list         — периодические задачи
/cron add "1д" ... — добавить задачу
/mcp list          — MCP-серверы
/mcp build python  — создать MCP-сервер
/skill list        — навыки
/skill create ...  — создать навык
/memory            — HOT память
/memory stats      — статистика (HOT/WARM/COLD)
/memory search <q> — поиск по архиву
/forget <слово>    — удалить из памяти
/improve           — цикл самоулучшения
/improve status    — статистика метрик
/version rollback  — откат версии
/exit              — выход
```

---

## Структура

```
freepalp/
├── core/
│   ├── orchestrator.py     # Главный контроллер
│   ├── router.py           # Routing + live model discovery
│   ├── model_discovery.py  # 8 провайдеров, 40+ моделей
│   ├── task_parser.py      # Определение типа задачи
│   ├── cron_manager.py     # Периодические задачи
│   ├── mcp_discovery.py    # MCP-серверы
│   ├── mcp_builder.py      # Генератор MCP-серверов
│   ├── prompt_loader.py    # Горячая перезагрузка конфига
│   └── self_improvement/   # Метрики → улучшение → версии
│
├── agents/
│   ├── worker_agent.py     # Worker + ReAct loop
│   ├── critic_agent.py     # Оценка качества (0–1)
│   ├── architect_agent.py  # DAG планирование
│   └── tool_agent.py       # Прокси инструментов (41 tool)
│
├── tools/
│   ├── file_tools.py       # Файловые операции (sandbox)
│   ├── shell_tools.py      # Shell (whitelist)
│   ├── web_tools.py        # Поиск + загрузка страниц
│   ├── browser_tools.py    # Playwright автоматизация
│   ├── github_tools.py     # GitHub API
│   ├── notification_tools.py # Email + Slack + Notion
│   └── system_tools.py     # Системные (память, cron, MCP...)
│
├── skills/
│   ├── coding.py           # Экспертный код-помощник
│   ├── research.py         # Исследование с поиском
│   ├── writing.py          # Тексты, документация
│   ├── data_analysis.py    # pandas, SQL, ML
│   └── architect.py        # Проектирование систем
│
├── memory/
│   ├── hot_memory.md       # Активная память (HOT)
│   ├── corrections.md      # Исправления критика
│   ├── sessions/           # История сессий (JSONL)
│   ├── warm/               # Демотированные записи
│   └── archive/            # Архив (COLD)
│
├── config/
│   ├── prompts.json        # Системные промпты (горячая перезагрузка)
│   ├── models.json         # Fallback конфиг моделей
│   └── versions/           # История версий конфига
│
├── web/                    # Neo-minimal WebUI
│   └── static/
│
├── state/                  # Состояние задач и cron
├── sandbox/                # Изолированная среда
├── app.py                  # CLI entrypoint
└── gateway.py              # FastAPI HTTP Gateway
```

---

## Провайдеры (все бесплатные)

| Провайдер | Модели | Лимиты |
|-----------|--------|--------|
| **Groq** | llama-3.3-70b, llama-3.1-8b, qwen2.5-coder | 30 req/min, 6K TPM |
| **OpenRouter** | 200+ моделей | $0 кредиты при регистрации |
| **Cerebras** | llama-3.1-70b (быстро) | 60 req/min |
| **Gemini** | gemini-1.5-flash, gemini-2.0-flash | 15 req/min |
| **SambaNova** | llama-3.1-405b | 10 req/min |
| **GitHub Models** | GPT-4o, Phi-4, Llama | 150 req/day |
| **Ollama** | любые локальные | без лимитов |

---

## Безопасность

- **Файловые операции** ограничены `sandbox/` — path traversal заблокирован
- **Shell whitelist** — только безопасные команды
- **API ключи** только в `.env`, никогда в логах
- **MAX_ITERATIONS = 3** — защита от бесконечных циклов
- **MAX_TOOL_CALLS = 6** — лимит ReAct шагов за задачу

---

## Поддержать

FreePalp бесплатен (MIT). Если он тебе полезен — поддержка помогает его развивать:

- 💖 **GitHub Sponsors:** https://github.com/sponsors/verdyshd
- 🪙 **USDT (TRC-20):** `<АДРЕС>` <!-- TODO: вставь адрес -->

GitHub Sponsors принимает карты откуда угодно; крипту обнал в Молдове через P2P.

---

_FreePalp v1.1 | MIT License | © Dmitry Verdysh_
