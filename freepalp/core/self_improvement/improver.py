"""
Improver — использует LLM для генерации улучшенных промптов и ключевых слов.

Получает на вход:
  - текущий компонент (промпт/keywords/threshold)
  - статистику проблем (что именно не работает)

Возвращает улучшенную версию компонента.
"""

import os
import re
import json
from typing import Optional
from ...core import prompt_loader


def _strip_think(text: str) -> str:
    """Убирает <think>...</think> блоки reasoning-моделей (qwen3, deepseek-r1 и т.п.).
    Возвращает чистый ответ без рассуждений."""
    if not text:
        return text
    # Удаляем закрытые блоки <think>...</think>
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL | re.IGNORECASE)
    # Если остался незакрытый <think> (обрезан) — берём всё после него или до него
    if "<think>" in text.lower():
        # незакрытый think в начале — берём хвост после последнего тега
        parts = re.split(r"</?think>", text, flags=re.IGNORECASE)
        text = parts[-1]
    return text.strip()


class Improver:
    """
    Генерирует улучшения через тот же LLM что используется для задач.
    Использует Groq если доступен, иначе любой доступный провайдер.
    """

    def __init__(self, model_id: str = "llama-3.3-70b-versatile", provider: str = "groq"):
        self.model_id = model_id
        self.provider = provider

    async def improve_worker_prompt(
        self,
        task_type: str,
        current_prompt: str,
        issues: list[str],
        stats: dict,
    ) -> str:
        """
        Улучшает системный промпт Worker для конкретного типа задачи.
        Возвращает новый промпт.
        """
        issues_text = "\n".join(f"- {i}" for i in issues) if issues else "- (нет конкретных проблем)"
        meta_prompt = f"""Ты эксперт по prompt engineering для AI агентов.

ЗАДАЧА: Улучши системный промпт для Worker агента (тип задачи: {task_type}).

ТЕКУЩИЙ ПРОМПТ:
{current_prompt}

СТАТИСТИКА ПРОБЛЕМ:
- Средний score: {stats.get('avg_score', '?')} (цель: >= 0.85)
- Retry rate: {stats.get('retry_rate', '?')} (цель: < 0.3)
- Задач проанализировано: {stats.get('n_tasks', '?')}

ПОВТОРЯЮЩИЕСЯ ПРОБЛЕМЫ В ОТВЕТАХ:
{issues_text}

ТРЕБОВАНИЯ К НОВОМУ ПРОМПТУ:
1. Исправь причины повторяющихся проблем
2. Сохрани основную роль и тон агента
3. Добавь конкретные инструкции которые предотвратят выявленные проблемы
4. Не делай промпт слишком длинным (максимум 300 слов)
5. Пиши на русском языке

Верни ТОЛЬКО текст нового промпта, без объяснений и markdown обёртки."""

        return await self._call_llm(meta_prompt)

    async def improve_keywords(
        self,
        task_type: str,
        current_keywords: list[str],
        misrouted_examples: list[str],
        stats: dict,
    ) -> list[str]:
        """
        Предлагает новые ключевые слова для улучшения routing accuracy.
        Возвращает расширенный список keywords.
        """
        examples_text = "\n".join(f"- {e}" for e in misrouted_examples[:10]) if misrouted_examples else "- (примеры не собраны)"
        current_kw_text = ", ".join(f'"{k}"' for k in current_keywords[:20])

        meta_prompt = f"""Ты эксперт по классификации текста.

ЗАДАЧА: Улучши список ключевых слов для определения типа задачи "{task_type}".

ТЕКУЩИЕ КЛЮЧЕВЫЕ СЛОВА (первые 20):
{current_kw_text}

СТАТИСТИКА:
- Retry rate: {stats.get('retry_rate', '?')} (задачи этого типа часто получают слабые ответы)
- Задач: {stats.get('n_tasks', '?')}

ПРИМЕРЫ ЗАДАЧ КОТОРЫЕ МОГЛИ БЫТЬ НЕПРАВИЛЬНО РАСПОЗНАНЫ:
{examples_text}

ТРЕБОВАНИЯ:
1. Предложи 10-15 НОВЫХ ключевых слов/фраз (которых ещё нет в списке)
2. Включи русские и английские варианты
3. Фразы должны быть 1-4 слова
4. Фокус на реальных пользовательских запросах

Верни ТОЛЬКО JSON массив новых ключевых слов, например:
["новая фраза 1", "new phrase 2", "ещё одна"]"""

        raw = await self._call_llm(meta_prompt)

        # Парсим JSON ответ
        try:
            # Ищем JSON массив в ответе
            import re
            match = re.search(r'\[.*?\]', raw, re.DOTALL)
            if match:
                new_kw = json.loads(match.group())
                if isinstance(new_kw, list):
                    # Объединяем с текущими, без дубликатов
                    combined = list(current_keywords)
                    for kw in new_kw:
                        if isinstance(kw, str) and kw.strip() and kw.strip() not in combined:
                            combined.append(kw.strip().lower())
                    return combined
        except Exception:
            pass

        return current_keywords  # Возвращаем без изменений если не распарсили

    async def improve_critic_system(
        self,
        current_prompt: str,
        stats: dict,
    ) -> str:
        """
        Улучшает промпт Critic агента для более гранулярной оценки.
        """
        meta_prompt = f"""Ты эксперт по prompt engineering для AI агентов.

ЗАДАЧА: Улучши системный промпт для Critic агента.

ТЕКУЩИЙ ПРОМПТ:
{current_prompt}

ПРОБЛЕМА:
- Глобальный средний score: {stats.get('global_avg', '?')}
- Variance оценок: {stats.get('variance', '?')} (слишком мало — критик не различает качество)

Критик ставит почти одинаковые оценки всем ответам.
Нужно сделать оценку более дифференцированной.

ТРЕБОВАНИЯ:
1. Добавь более чёткие критерии разграничения между 0.7 и 0.9 и 1.0
2. Добавь конкретные примеры что снижает оценку
3. Потребуй от критика указывать ТОЧНУЮ строку/утверждение при каждой проблеме
4. Сохрани строгость и формат PASSED/SCORE/ISSUES/SUGGESTIONS
5. Максимум 400 слов

Верни ТОЛЬКО текст нового промпта."""

        return await self._call_llm(meta_prompt)

    # ------------------------------------------------------------------
    # LLM вызов
    # ------------------------------------------------------------------

    async def _call_llm(self, prompt: str, system_msg: Optional[str] = None) -> str:
        """Вызывает LLM для генерации улучшения. Пробует несколько провайдеров при ошибках.
        system_msg — кастомное системное сообщение (по умолчанию — эксперт по улучшению)."""
        # Порядок попыток: основная модель, затем альтернативы.
        # Mistral — высоко: огромная квота (1 млрд tok/мес) и НЕТ think-блоков.
        # qwen3-32b убран из приоритета — reasoning-модель засоряет вывод <think>.
        _FALLBACK_MODELS = [
            ("groq",      self.model_id),
            ("mistral",   "mistral-large-latest"),
            ("groq",      "llama-3.1-8b-instant"),
            ("groq",      "meta-llama/llama-4-scout-17b-16e-instruct"),
            ("sambanova", "Meta-Llama-3.3-70B-Instruct"),
            ("groq",      "qwen/qwen3-32b"),   # последним — нужен strip think
            ("ollama",    self.model_id),
        ]
        last_error = "unknown"
        for provider, model_id in _FALLBACK_MODELS:
            try:
                if provider == "groq":
                    result = await self._call_groq(prompt, model_id=model_id, system_msg=system_msg)
                elif provider == "mistral":
                    result = await self._call_openai_compat(prompt, model_id=model_id,
                        base_url="https://api.mistral.ai/v1",
                        env_key="MISTRAL_API_KEY", system_msg=system_msg)
                elif provider == "sambanova":
                    result = await self._call_openai_compat(prompt, model_id=model_id,
                        base_url="https://api.sambanova.ai/v1",
                        env_key="SAMBANOVA_API_KEY", system_msg=system_msg)
                elif provider == "ollama":
                    result = await self._call_ollama(prompt, system_msg=system_msg)
                else:
                    continue
                # Вырезаем <think>...</think> блоки reasoning-моделей
                result = _strip_think(result)
                # Проверяем что результат не ошибка
                if result and not result.startswith("[") and len(result) > 30:
                    if provider != self.provider or model_id != self.model_id:
                        print(f"  [Improver] Used fallback: {provider}/{model_id}")
                    return result
                last_error = result[:100] if result else "empty"
            except Exception as e:
                last_error = str(e)[:100]
                continue
        return f"[Ошибка генерации: все провайдеры недоступны. Последняя ошибка: {last_error}]"

    async def _call_groq(self, prompt: str, model_id: str = "", system_msg: Optional[str] = None) -> str:
        from groq import AsyncGroq
        model_id = model_id or self.model_id
        # Try dotenv if key not in env
        api_key = os.environ.get("GROQ_API_KEY", "")
        if not api_key:
            try:
                from dotenv import load_dotenv
                from pathlib import Path
                load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")
                api_key = os.environ.get("GROQ_API_KEY", "")
            except Exception:
                pass
        if not api_key:
            raise RuntimeError("GROQ_API_KEY не задан")
        client = AsyncGroq(api_key=api_key)
        resp = await client.chat.completions.create(
            model=model_id,
            messages=[
                {"role": "system", "content": system_msg or "Ты эксперт по улучшению AI систем. Отвечай точно и по делу."},
                {"role": "user", "content": prompt},
            ],
            max_tokens=2048,
            temperature=0.4,
        )
        content = resp.choices[0].message.content or ""
        if not content:
            raise RuntimeError("Empty response")
        return content

    async def _call_openai_compat(self, prompt: str, model_id: str,
                                   base_url: str, env_key: str,
                                   system_msg: Optional[str] = None) -> str:
        """OpenAI-compatible API (SambaNova, Together, etc.)"""
        import httpx
        api_key = os.environ.get(env_key, "")
        if not api_key:
            from dotenv import load_dotenv
            from pathlib import Path
            load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")
            api_key = os.environ.get(env_key, "")
        if not api_key:
            raise RuntimeError(f"{env_key} не задан")
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}",
                         "Content-Type": "application/json"},
                json={
                    "model": model_id,
                    "messages": [
                        {"role": "system", "content": system_msg or "Ты эксперт по улучшению AI систем. Отвечай точно и по делу."},
                        {"role": "user", "content": prompt},
                    ],
                    "max_tokens": 2048,
                    "temperature": 0.4,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"] or ""

    async def _call_ollama(self, prompt: str, system_msg: Optional[str] = None) -> str:
        import httpx
        msgs = []
        if system_msg:
            msgs.append({"role": "system", "content": system_msg})
        msgs.append({"role": "user", "content": prompt})
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                "http://localhost:11434/api/chat",
                json={
                    "model": self.model_id,
                    "messages": msgs,
                    "stream": False,
                    "options": {"temperature": 0.4},
                },
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]
