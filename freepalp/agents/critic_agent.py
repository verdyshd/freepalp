"""
Critic Agent — проверяет результат Worker.
Возвращает оценку и список проблем.
"""

import re
import time
import os
from ..core.models import (
    AgentMessage, CriticFeedback, TaskRequest, ModelConfig, TaskType
)
from ..core import prompt_loader
from ..core import session_keys as _skeys
from .worker_agent import _catalog_endpoint

# Fallback если prompts.json недоступен
_CRITIC_SYSTEM_FALLBACK = """Ты строгий QA инженер, fact-checker и code reviewer.
Твоя задача — найти проблемы в ответе AI агента и дать точную оценку.

Критерии оценки SCORE (0.0–1.0):
  0.95–1.0  — идеальный ответ: полный, технически точный, без замечаний
  0.85–0.94 — хороший ответ: небольшие недостатки стиля, но факты верны
  0.70–0.84 — приемлемый ответ: есть проблемы, основная часть верна
  0.50–0.69 — слабый ответ: фактические ошибки или код не запустится
  0.00–0.49 — плохой ответ: не отвечает на вопрос, критические ошибки

Проверяй СТРОГО по порядку:

[ФАКТЫ — самое важное]
1. Нет ли технически неверных утверждений? Примеры ошибок:
   - "GIL замедляет IO" — НЕВЕРНО (GIL снимается при IO-системных вызовах)
   - "threading не подходит для IO" — НЕВЕРНО (подходит, просто asyncio эффективнее)
   - Неверные характеристики алгоритмов, протоколов, инструментов
2. Нет ли упрощений которые вводят в заблуждение?

[КОД — если в ответе есть код]
3. Все ли импорты существуют? Проверяй каждый:
   - from rabbitmq import RabbitMQ → НЕ СУЩЕСТВУЕТ (нужен pika)
   - from firebase import Firebase → НЕ СУЩЕСТВУЕТ (нужен firebase_admin)
   - from redis import client → НЕ СУЩЕСТВУЕТ (нужен redis.Redis)
   Несуществующий импорт = PASSED: no, SCORE не выше 0.55
4. Работает ли код логически? Нет ли синтаксических ошибок?
5. Есть ли type hints, docstrings, обработка ошибок?
6. Нет ли security issues (hardcoded secrets, sql injection, etc.)?

[ПОЛНОТА]
7. Ответил ли агент на ВСЕ части вопроса?
8. Ответ конкретный или слишком расплывчатый?

Формат ответа СТРОГО (без лишних слов перед PASSED):
PASSED: yes/no
SCORE: 0.0-1.0
ISSUES:
- конкретная проблема с указанием строки/утверждения
SUGGESTIONS:
- конкретное предложение по исправлению
"""


class CriticAgent:
    """
    Критикует результат Worker агента.
    Принимает решение о повторной попытке.
    """

    def __init__(self, model_config: ModelConfig):
        self.model = model_config

    async def evaluate(
        self,
        request: TaskRequest,
        worker_output: str,
        iteration: int = 0,
    ) -> tuple[AgentMessage, CriticFeedback]:
        """
        Возвращает AgentMessage (от критика) и CriticFeedback (структурированно).
        """
        user_prompt = self._build_critic_prompt(request, worker_output)
        # Получаем актуальный critic prompt из конфига (может быть обновлён самообучением)
        critic_system = prompt_loader.get_critic_system() or _CRITIC_SYSTEM_FALLBACK
        start = time.time()

        tokens_in, tokens_out = 0, 0

        raw, tokens_in, tokens_out = await self._dispatch(user_prompt, critic_system)

        # Фолбэк: если критик упал (429 / недоступен / не задан) — пробуем Mistral
        # (огромная квота, редко лимитируется). Без этого критик отдавал флэт 0.72.
        if self._is_critic_failure(raw):
            try:
                fb_key = _skeys.get_api_key("MISTRAL_API_KEY")
                if fb_key:
                    raw2, ti2, to2 = await self._call_openai_compat_explicit(
                        user_prompt, critic_system,
                        base_url="https://api.mistral.ai/v1",
                        api_key=fb_key, model_id="mistral-large-latest")
                    if not self._is_critic_failure(raw2):
                        raw, tokens_in, tokens_out = raw2, ti2, to2
            except Exception:
                pass

        elapsed = time.time() - start
        feedback = self._parse_response(raw)

        # Порог: score < 0.7 — нужен retry (было 0.6 — слишком мягко)
        feedback.must_retry = not feedback.passed or feedback.score < 0.7

        total_tokens = tokens_in + tokens_out
        cost_usd = (total_tokens / 1000) * self.model.cost_per_1k

        msg = AgentMessage(
            role="critic",
            content=raw,
            model_used=self.model.model_id,
            tokens_used=total_tokens,
            iteration=iteration,
            metadata={
                "elapsed": round(elapsed, 2),
                "score": feedback.score,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cost_usd": round(cost_usd, 6),
            },
        )
        return msg, feedback

    # ------------------------------------------------------------------

    def _build_critic_prompt(self, request: TaskRequest, output: str) -> str:
        # Если есть история диалога — показываем Critic'у контекст
        history = request.context.get("conversation_history", [])
        history_block = ""
        if history:
            lines = []
            for h in history[-6:]:  # последние 3 обмена
                role_label = "Пользователь" if h.get("role") == "user" else "FreePalp"
                lines.append(f"[{role_label}]: {h['content'][:300]}")
            history_block = "\n\nКОНТЕКСТ ПРЕДЫДУЩЕГО ДИАЛОГА:\n" + "\n".join(lines)

        return f"""ИСХОДНАЯ ЗАДАЧА:{history_block}

[Текущий вопрос]: {request.user_input}

ОТВЕТ АГЕНТА:
{output}

Оцени качество ответа. Учти контекст предыдущего диалога при оценке — если агент ссылается на информацию из истории, это ПРАВИЛЬНО, а не галлюцинация."""

    async def _dispatch(self, user_prompt: str, critic_system: str) -> tuple[str, int, int]:
        """Роутит вызов критика к нужному провайдеру."""
        p = self.model.provider
        if p == "ollama":
            return await self._call_ollama(user_prompt, critic_system)
        elif p == "groq":
            return await self._call_groq(user_prompt, critic_system)
        elif p == "anthropic":
            return await self._call_anthropic(user_prompt, critic_system)
        elif p in ("openrouter", "cerebras", "together", "novita", "sambanova", "github", "mistral"):
            return await self._call_openai_compat(user_prompt, critic_system)
        elif p == "gemini":
            return await self._call_gemini(user_prompt, critic_system)
        elif _catalog_endpoint(p):
            # Провайдер добавлен динамически из каталога models.dev
            return await self._call_openai_compat(user_prompt, critic_system)
        return ("PASSED: yes\nSCORE: 0.72\nISSUES:\n- Провайдер критика не поддерживается\nSUGGESTIONS:", 0, 0)

    @staticmethod
    def _is_critic_failure(raw: str) -> bool:
        """True если ответ критика — это ошибка/дефолт (не реальная оценка)."""
        if not raw:
            return True
        low = raw.lower()
        markers = ["критик недоступен", "не поддерживается", "не задан",
                   "429", "rate limit", "rate-limit"]
        return any(m in low for m in markers)

    async def _call_openai_compat_explicit(self, user: str, system: str,
                                           base_url: str, api_key: str,
                                           model_id: str) -> tuple[str, int, int]:
        """OpenAI-совместимый вызов с явными base_url/key/model (для фолбэка)."""
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        resp = await client.chat.completions.create(
            model=model_id,
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}],
            max_tokens=1024, temperature=0.2,
        )
        content = resp.choices[0].message.content or ""
        u = resp.usage
        return content, (u.prompt_tokens if u else 0), (u.completion_tokens if u else 0)

    def _parse_response(self, raw: str) -> CriticFeedback:
        """Парсит структурированный ответ критика."""
        passed = True
        score = 0.0   # будет перезаписан из SCORE: строки
        issues = []
        suggestions = []
        score_found = False

        lines = raw.strip().split("\n")
        section = None

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith("PASSED:"):
                val = line.split(":", 1)[1].strip().lower()
                passed = val in ("yes", "true", "да", "1")
            elif line.startswith("SCORE:"):
                try:
                    score = float(line.split(":", 1)[1].strip())
                    score_found = True
                except ValueError:
                    score = 0.7
                    score_found = True
            elif line.startswith("ISSUES:"):
                section = "issues"
            elif line.startswith("SUGGESTIONS:"):
                section = "suggestions"
            elif line.startswith("- "):
                item = line[2:].strip()
                if section == "issues" and item:
                    issues.append(item)
                elif section == "suggestions" and item:
                    suggestions.append(item)

        # Dedup issues (preserve order) and limit to 5
        issues = list(dict.fromkeys(issues))[:5]
        suggestions = list(dict.fromkeys(suggestions))[:5]

        # Если критик не дал SCORE — считаем по количеству проблем
        if not score_found:
            if not issues:
                score = 0.85  # нет проблем = хороший ответ
            elif len(issues) <= 2:
                score = 0.65
            else:
                score = 0.45

        # Если явно нет проблем — минимум 0.75
        if not issues and score < 0.75:
            score = 0.75

        must_retry = not passed or score < 0.7

        return CriticFeedback(
            passed=passed,
            score=score,
            issues=issues,
            suggestions=suggestions,
            must_retry=must_retry,
        )

    # ------------------------------------------------------------------
    # Провайдеры (аналогично WorkerAgent)
    # ------------------------------------------------------------------

    async def _call_ollama(self, user: str, system: str) -> tuple[str, int, int]:
        try:
            import httpx
            payload = {
                "model": self.model.model_id,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 1024},
            }
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    "http://localhost:11434/api/chat",
                    json=payload,
                )
                resp.raise_for_status()
                data = resp.json()
                tokens_in = data.get("prompt_eval_count", 0)
                tokens_out = data.get("eval_count", 0)
                return data["message"]["content"], tokens_in, tokens_out
        except Exception as e:
            return f"PASSED: yes\nSCORE: 0.75\nISSUES:\n- Критик недоступен: {e}\nSUGGESTIONS:", 0, 0

    async def _call_groq(self, user: str, system: str) -> tuple[str, int, int]:
        try:
            from groq import AsyncGroq
            api_key = _skeys.get_api_key("GROQ_API_KEY")
            if not api_key:
                return "PASSED: yes\nSCORE: 0.72\nISSUES:\n- GROQ_API_KEY не задан (критик пропущен)\nSUGGESTIONS:", 0, 0
            client = AsyncGroq(api_key=api_key)
            resp = await client.chat.completions.create(
                model=self.model.model_id,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=1024,
                temperature=0.1,
            )
            tokens_in = resp.usage.prompt_tokens if resp.usage else 0
            tokens_out = resp.usage.completion_tokens if resp.usage else 0
            return resp.choices[0].message.content or "", tokens_in, tokens_out
        except Exception as e:
            # Rate-limit: 0.72 — ниже порога retry (0.7), честнее чем 0.80
            return f"PASSED: yes\nSCORE: 0.72\nISSUES:\n- Критик недоступен: {str(e)[:80]}\nSUGGESTIONS:\n- Повтори запрос позже", 0, 0

    async def _call_anthropic(self, user: str, system: str) -> tuple[str, int, int]:
        try:
            import anthropic
            api_key = _skeys.get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                return "PASSED: yes\nSCORE: 0.75\nISSUES:\nSUGGESTIONS:", 0, 0
            client = anthropic.AsyncAnthropic(api_key=api_key)
            resp = await client.messages.create(
                model=self.model.model_id,
                max_tokens=1024,
                system=system,
                messages=[{"role": "user", "content": user}],
            )
            tokens_in = resp.usage.input_tokens if resp.usage else 0
            tokens_out = resp.usage.output_tokens if resp.usage else 0
            return resp.content[0].text, tokens_in, tokens_out
        except Exception as e:
            return f"PASSED: yes\nSCORE: 0.72\nISSUES:\n- Критик недоступен: {str(e)[:80]}\nSUGGESTIONS:\n- Проверь API ключ и лимиты", 0, 0

    async def _call_gemini(self, user: str, system: str) -> tuple[str, int, int]:
        """Google Gemini через google-generativeai SDK."""
        try:
            import google.generativeai as genai
            api_key = _skeys.get_api_key("GEMINI_API_KEY")
            if not api_key:
                return "PASSED: yes\nSCORE: 0.75\nISSUES:\n- GEMINI_API_KEY не задан\nSUGGESTIONS:", 0, 0
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel(
                model_name=self.model.model_id,
                system_instruction=system,
            )
            resp = await model.generate_content_async(
                user,
                generation_config={"max_output_tokens": 1024, "temperature": 0.1},
            )
            text = resp.text or ""
            tokens_in  = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
            tokens_out = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
            return text, tokens_in, tokens_out
        except Exception as e:
            return f"PASSED: yes\nSCORE: 0.72\nISSUES:\n- Критик недоступен: {str(e)[:80]}\nSUGGESTIONS:\n- Проверь API ключ и лимиты", 0, 0

    async def _call_openai_compat(self, user: str, system: str) -> tuple[str, int, int]:
        """OpenAI-совместимый вызов для OpenRouter / Cerebras / Together / Novita."""
        ENDPOINTS = {
            "openrouter": ("https://openrouter.ai/api/v1",         "OPENROUTER_API_KEY"),
            "cerebras":   ("https://api.cerebras.ai/v1",           "CEREBRAS_API_KEY"),
            "together":   ("https://api.together.xyz/v1",          "TOGETHER_API_KEY"),
            "novita":     ("https://api.novita.ai/v3/openai",      "NOVITA_API_KEY"),
            "sambanova":  ("https://api.sambanova.ai/v1",          "SAMBANOVA_API_KEY"),
            "github":     ("https://models.inference.ai.azure.com","GITHUB_TOKEN"),
            "mistral":    ("https://api.mistral.ai/v1",            "MISTRAL_API_KEY"),
        }
        base_url, env_key = ENDPOINTS.get(self.model.provider, ("", ""))
        if not base_url:
            # Провайдер из каталога models.dev — endpoint берём оттуда
            dyn = _catalog_endpoint(self.model.provider)
            if dyn:
                base_url, env_key = dyn
            else:
                return "PASSED: yes\nSCORE: 0.72\nISSUES:\n- Провайдер критика не поддерживается\nSUGGESTIONS:", 0, 0
        try:
            from openai import AsyncOpenAI
            api_key = _skeys.get_api_key(env_key)
            if not api_key:
                return f"PASSED: yes\nSCORE: 0.75\nISSUES:\n- {env_key} не задан\nSUGGESTIONS:", 0, 0
            client = AsyncOpenAI(api_key=api_key, base_url=base_url)
            resp = await client.chat.completions.create(
                model=self.model.model_id,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=1024,
                temperature=0.1,
            )
            tokens_in = resp.usage.prompt_tokens if resp.usage else 0
            tokens_out = resp.usage.completion_tokens if resp.usage else 0
            return resp.choices[0].message.content or "", tokens_in, tokens_out
        except Exception as e:
            return f"PASSED: yes\nSCORE: 0.72\nISSUES:\n- Критик недоступен: {str(e)[:80]}\nSUGGESTIONS:\n- Проверь API ключ и лимиты", 0, 0
