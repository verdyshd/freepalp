"""Обучающий батч на слабые типы: shell (0.685) и review (0.79).
Цель: собрать данные + увидеть текущее качество перед SkillOpt-циклом.
"""
import sys, json, time, urllib.request

sys.stdout.reconfigure(encoding='utf-8', errors='replace')
BASE = "http://localhost:28800"

# Задачи нацелены на shell и review
SESSIONS = [
    # ── shell ──
    "Напиши bash-скрипт который находит все .log файлы старше 7 дней и удаляет их, с подтверждением и логированием.",
    "Bash one-liner: посчитать топ-10 уникальных IP по числу запросов в nginx access.log.",
    "Напиши bash-скрипт для бэкапа PostgreSQL базы с ротацией (хранить последние 5 копий) и проверкой ошибок.",
    "Shell-скрипт: мониторить использование диска и слать алерт если занято больше 90%.",
    "Напиши bash-функцию которая безопасно извлекает .tar.gz/.zip/.tar архивы по расширению.",
    # ── review ──
    "Сделай код-ревью: def divide(a, b): return a / b — найди проблемы и предложи исправления.",
    "Ревью Python: çfunction читает файл через open() без with и без обработки исключений. Что не так?",
    "Код-ревью: SQL запрос 'SELECT * FROM users WHERE name = ' + user_input. Найди уязвимости.",
    "Ревью REST API эндпоинта который принимает пароль в GET-параметре и хранит его в plaintext. Проблемы?",
    "Код-ревью Python функции с вложенными циклами O(n^3) для поиска дубликатов в списке. Оптимизируй.",
]

results = []
for i, msg in enumerate(SESSIONS, 1):
    print(f"[{i}/{len(SESSIONS)}] {msg[:65]}...")
    t0 = time.time()
    payload = json.dumps({"message": msg}).encode()
    req = urllib.request.Request(f"{BASE}/api/chat", data=payload,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            d = json.loads(resp.read())
            tt = d.get('task_type') or d.get('type', '?')
            sc = d.get('score', d.get('critic_score', 0))
            mdl = d.get('model', d.get('model_used', '?'))
            print(f"  score={sc} model={mdl}  ({time.time()-t0:.1f}s)")
            results.append((tt, sc))
    except Exception as ex:
        print(f"  Error: {ex}")
    time.sleep(1)

# Итоги по типам
print("\n=== Итог батча ===")
from collections import defaultdict
agg = defaultdict(list)
for tt, sc in results:
    agg[tt].append(sc)
for tt, scores in sorted(agg.items()):
    avg = sum(scores) / len(scores) if scores else 0
    print(f"  {tt:12s}: n={len(scores)} avg={avg*100:.1f}%")
