"""
Worker Agent — выполняет основную задачу.

Поддерживает ReAct loop (Reason + Act):
  LLM думает → решает вызвать инструмент → получает результат → продолжает
  Повторяет до MAX_TOOL_CALLS раз, затем возвращает финальный ответ.

Формат вызова инструмента в ответе LLM:
  ```tool_call
  {"tool": "web_search", "args": {"query": "python asyncio"}}
  ```

Провайдеры: Ollama, Groq, Anthropic, OpenRouter, Cerebras, SambaNova,
             GitHub Models, Gemini, Together, Novita.
"""

import asyncio
import os
import re
import json
import time
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from ..core import session_keys as _skeys
from ..core.models import (
    TaskRequest, AgentMessage, ModelConfig, TaskType
)
from ..core import prompt_loader

if TYPE_CHECKING:
    from .tool_agent import ToolAgent

MAX_TOOL_CALLS  = 6    # максимум вызовов инструментов за одну задачу
_TRACE_FILE     = Path(__file__).parent.parent / "state" / "react_trace.jsonl"
_TRACE_MAX_ROWS = 500  # сколько строк держим в трейс-файле

# Провайдеры, поддерживающие native function calling
_NATIVE_FC_PROVIDERS = frozenset({"github", "groq", "cerebras", "openrouter", "sambanova"})


def _catalog_endpoint(provider: str) -> Optional[tuple[str, str]]:
    """(base_url, env_key) для провайдера, добавленного из каталога models.dev.

    Discovery регистрирует каталожные провайдеры в _OPENAI_COMPAT_PROVIDERS,
    но захардкоженные ENDPOINTS воркера про них не знают — без этого резолва
    любой каталожный провайдер падал в «не поддерживается»."""
    try:
        from ..core.model_discovery import _OPENAI_COMPAT_PROVIDERS
        cfg = _OPENAI_COMPAT_PROVIDERS.get(provider)
        if cfg and cfg.get("base_url") and cfg.get("env_key"):
            return cfg["base_url"], cfg["env_key"]
    except Exception:
        pass
    return None

# Лимит токенов по типу задачи — меньше токенов = быстрее ответ (без потери качества)
# Простые задачи не требуют 4096 токенов; сложные (file_ops, coding_large) — оставляем полный лимит
TASK_MAX_TOKENS: dict[str, int] = {
    "coding_small":  1800,   # функции, декораторы, короткие фрагменты
    "text":          1200,   # объяснения, документация, README
    "shell":         800,    # команды, однострочники
    "review":        1500,   # код-ревью — нужно пройтись по деталям
    "search":        1200,   # поисковые ответы
    "general":       2000,   # разговор, вопросы
    "coding_large":  4096,   # полные классы, модули — не ограничиваем
    "architecture":  3000,   # архитектурные документы
    "file_ops":      4096,   # работа с файлами — не ограничиваем
}


class WorkerAgent:
    """
    Выполняет задачу через LLM с опциональным ReAct loop.
    Если передан tool_agent — агент может вызывать инструменты сам.
    """

    def __init__(self, model_config: ModelConfig, tool_agent: Optional["ToolAgent"] = None):
        self.model                  = model_config
        self.tool_agent             = tool_agent
        self._task_key: str         = "general"
        self._pending_call_id: Optional[str] = None   # native FC: call_id текущего tool call
        self._native_mode: bool     = False            # True если провайдер поддерживает native FC
        self._openai_tools: Optional[list] = None      # схема инструментов для native FC

    async def run(
        self,
        request: TaskRequest,
        iteration: int = 0,
        prev_feedback: Optional[str] = None,
    ) -> AgentMessage:
        """
        Выполняет задачу. Возвращает AgentMessage с результатом.
        prev_feedback — замечания критика (если это retry).
        """
        self._task_key    = request.task_type.value if request.task_type else "general"
        task_key          = self._task_key
        system_prompt     = prompt_loader.get_worker_prompt(task_key)

        # Включаем native function calling если провайдер поддерживает
        self._native_mode = (
            self.tool_agent is not None
            and self.model.provider in _NATIVE_FC_PROVIDERS
        )
        if self._native_mode:
            self._openai_tools = self._build_openai_tools_spec(task_key)

        # Инжектируем описание инструментов в system prompt если есть tool_agent
        if self.tool_agent:
            if not self._native_mode:
                # Текстовый ReAct — инструкции в промпт
                tools_desc = self._build_tools_section(task_key)
                system_prompt += f"\n\n{tools_desc}"
            else:
                # Native FC — короткая подсказка вместо полного текста
                system_prompt += (
                    "\n\nYou have tools available via function calling. "
                    "Call them when you need data. Give the final answer when done."
                )

        # Начальное сообщение от пользователя
        user_prompt = self._build_prompt(request, prev_feedback)

        start = time.time()
        total_tokens_in  = 0
        total_tokens_out = 0
        tools_called: list[str] = []

        # ReAct loop: [LLM] → [tool_call?] → [execute] → [LLM] → ...
        # Начинаем с system prompt, затем история диалога, затем текущий запрос
        messages = [
            {"role": "system", "content": system_prompt},
        ]

        # Инжектируем историю диалога (short-term memory) — последние N обменов
        conv_history = request.context.get("conversation_history", [])
        if conv_history:
            # Берём последние 3 обмена (6 сообщений) для экономии токенов
            for h in conv_history[-6:]:
                role = h.get("role", "user")
                # Нормализуем role: только "user" или "assistant"
                if role not in ("user", "assistant"):
                    role = "user"
                messages.append({"role": role, "content": h["content"]})

        # Текущий запрос пользователя (с agent_memory и feedback)
        # Если есть прикреплённые картинки — формируем multipart user message
        att_images = request.context.get("attachments_images", [])
        if att_images:
            # OpenAI/Groq vision format: content = list of parts
            user_content: list = [{"type": "text", "text": user_prompt}]
            for img in att_images:
                user_content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img['mime']};base64,{img['data']}",
                    },
                })
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user_prompt})

        final_content = ""

        for step in range(MAX_TOOL_CALLS + 1):
            # Вызов LLM
            content, t_in, t_out = await self._call_llm(messages)
            total_tokens_in  += t_in
            total_tokens_out += t_out

            if content.startswith("[Ошибка"):
                final_content = content
                break

            # Ищем tool_call блок
            tool_call = self._extract_tool_call(content)

            if not tool_call or not self.tool_agent:
                # Нет вызова инструмента — это финальный ответ
                # Убираем незавершённые tool_call блоки если есть
                final_content = self._strip_incomplete_tool_calls(content)
                break

            # Логируем вызов
            tool_name = tool_call.get("tool", "?")
            tool_args = tool_call.get("args", {})

            # Loop breaker: тот же инструмент с теми же аргументами 3 раза подряд —
            # агент зациклился (наблюдалось: gemini писал один файл 7 раз).
            import hashlib as _hl
            _sig = _hl.md5((tool_name + repr(sorted(tool_args.items()))).encode()).hexdigest()
            _recent = getattr(self, "_recent_call_sigs", [])
            _recent.append(_sig)
            self._recent_call_sigs = _recent[-3:]
            if len(self._recent_call_sigs) == 3 and len(set(self._recent_call_sigs)) == 1:
                print(f"    [LoopBreaker] {tool_name} повторён 3 раза с теми же аргументами — прерываю цикл")
                messages.append({"role": "user", "content":
                    f"СТОП: ты вызвал {tool_name} три раза подряд с одинаковыми аргументами. "
                    "Инструмент уже выполнен. Не вызывай его снова — дай финальный ответ."})
                self._recent_call_sigs = []
                continue

            tools_called.append({"tool": tool_name, "content": tool_args.get("content", ""),
                                 "path": tool_args.get("path", "")})
            print(f"    [ReAct] CALL {tool_name}({', '.join(f'{k}={repr(v)[:30]}' for k, v in tool_args.items())})")
            self._trace("tool_call", tool_name, tool_args, "")

            # Добавляем ответ LLM в историю — формат зависит от режима
            if self._pending_call_id:
                # Native FC: правильный формат tool_calls
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": self._pending_call_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": json.dumps(tool_args, ensure_ascii=False),
                        }
                    }]
                })
            else:
                # Текстовый ReAct
                messages.append({"role": "assistant", "content": content})

            # Выполняем инструмент
            try:
                result = await self.tool_agent.execute(tool_name, **tool_args)
            except Exception as e:
                result = {"ok": False, "error": str(e)}

            # Форматируем результат для LLM
            result_text = self._format_tool_result(tool_name, result)
            print(f"    [ReAct] RESULT {tool_name}: {result_text[:100]}{'...' if len(result_text) > 100 else ''}")
            self._trace("tool_result", tool_name, {}, result_text[:300])

            # Добавляем результат в историю — формат зависит от режима
            if self._pending_call_id:
                messages.append({
                    "role":         "tool",
                    "tool_call_id": self._pending_call_id,
                    "content":      result_text,
                })
                self._pending_call_id = None
            else:
                messages.append({"role": "user", "content": result_text})

        else:
            # Исчерпан лимит tool calls — просим финальный ответ
            messages.append({
                "role":    "user",
                "content": (
                    "Ты использовал максимальное количество инструментов. "
                    "Теперь дай ФИНАЛЬНЫЙ ответ пользователю на основе всех полученных данных."
                ),
            })
            content, t_in, t_out = await self._call_llm(messages)
            total_tokens_in  += t_in
            total_tokens_out += t_out
            final_content = self._strip_incomplete_tool_calls(content)

        elapsed      = time.time() - start
        total_tokens = total_tokens_in + total_tokens_out
        cost_usd     = (total_tokens / 1000) * self.model.cost_per_1k

        return AgentMessage(
            role="worker",
            content=final_content,
            model_used=self.model.model_id,
            tokens_used=total_tokens,
            iteration=iteration,
            metadata={
                "elapsed":     round(elapsed, 2),
                "tokens_in":   total_tokens_in,
                "tokens_out":  total_tokens_out,
                "cost_usd":    round(cost_usd, 6),
                "tools_called": tools_called,
            },
        )

    # ──────────────────────────────────────────────────────────────────
    # ReAct helpers
    # ──────────────────────────────────────────────────────────────────

    def _build_tools_section(self, task_key: str = "general") -> str:
        """Описание инструментов для системного промпта — только релевантных для task_type."""
        if not self.tool_agent:
            return ""
        from ..agents.tool_agent import ALL_TOOLS
        from ..tools.tools_filter import filter_tools
        tools_to_show = filter_tools(ALL_TOOLS, task_key)
        lines = [
            "=== TOOL USE INSTRUCTIONS ===",
            "You have access to tools. To call a tool you MUST output a tool_call block like this:",
            "",
            "```tool_call",
            '{"tool": "TOOL_NAME", "args": {"param": "value"}}',
            "```",
            "",
            "RULES:",
            "1. Output the tool_call block IMMEDIATELY when you need data — do NOT describe it first.",
            "2. Do NOT say 'I will call...', just output the block directly.",
            "3. After the block you will receive TOOL RESULT [...] and you continue reasoning.",
            "4. Give final answer WITHOUT any tool_call block when done.",
            "",
            "EXAMPLE — if asked to search the web:",
            "```tool_call",
            '{"tool": "web_search", "args": {"query": "python asyncio tutorial"}}',
            "```",
            "",
            "EXAMPLE — if asked to save to memory:",
            "```tool_call",
            '{"tool": "memory_write", "args": {"content": "the note text", "mode": "append"}}',
            "```",
            "",
            f"Available tools ({len(tools_to_show)} relevant for {task_key}):",
        ]
        for name, info in tools_to_show.items():
            args_raw = info.get("args", {})
            if isinstance(args_raw, dict):
                args_str = ", ".join(f"{k}: {v}" for k, v in args_raw.items())
            elif isinstance(args_raw, list):
                args_str = ", ".join(str(a) for a in args_raw)
            else:
                args_str = str(args_raw)
            lines.append(f"  • {name}({args_str}) — {info['description']}")
        return "\n".join(lines)

    # ──────────────────────────────────────────────────────────────────
    # JSONL-трейсинг ReAct-шагов (для постфактум анализа)
    # ──────────────────────────────────────────────────────────────────

    def _trace(self, step: str, tool: str = "", args: Optional[dict] = None, summary: str = "") -> None:
        """Записывает шаг ReAct в JSONL. Тихо проглатывает I/O ошибки."""
        try:
            entry = json.dumps({
                "ts":      time.strftime("%Y-%m-%dT%H:%M:%S"),
                "model":   self.model.name,
                "task":    self._task_key,
                "step":    step,
                "tool":    tool,
                "args":    args or {},
                "summary": summary[:300],
            }, ensure_ascii=False)
            _TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
            with open(_TRACE_FILE, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
            # Обрезаем до _TRACE_MAX_ROWS
            lines = _TRACE_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > _TRACE_MAX_ROWS:
                _TRACE_FILE.write_text(
                    "\n".join(lines[-_TRACE_MAX_ROWS:]) + "\n", encoding="utf-8"
                )
        except Exception:
            pass

    # ──────────────────────────────────────────────────────────────────
    # Native Function Calling — схема инструментов для OpenAI API
    # ──────────────────────────────────────────────────────────────────

    def _build_openai_tools_spec(self, task_key: str) -> list[dict]:
        """Строит список инструментов в формате OpenAI function calling API."""
        from ..agents.tool_agent import ALL_TOOLS
        from ..tools.tools_filter import filter_tools
        tools = filter_tools(ALL_TOOLS, task_key)
        specs = []
        for name, info in tools.items():
            args_raw = info.get("args", {})
            if isinstance(args_raw, list):
                props: dict = {k: {"type": "string"} for k in args_raw}
            elif isinstance(args_raw, dict):
                props = {}
                for k, v in args_raw.items():
                    props[k] = {"type": "string", "description": str(v)}
            else:
                props = {}
            specs.append({
                "type": "function",
                "function": {
                    "name":        name,
                    "description": info.get("description", ""),
                    "parameters": {
                        "type":       "object",
                        "properties": props,
                    },
                },
            })
        return specs

    def _extract_tool_call(self, content: str) -> Optional[dict]:
        """Извлекает первый tool_call JSON из ответа LLM."""
        # Проверяем маркер native FC
        if content.startswith("__NATIVE_TOOL__"):
            try:
                return json.loads(content[len("__NATIVE_TOOL__"):])
            except Exception:
                pass
        # Ищем блок ```tool_call ... ```
        pattern = r"```tool_call\s*\n(.*?)\n?```"
        match   = re.search(pattern, content, re.DOTALL | re.IGNORECASE)
        if not match:
            return None
        raw = match.group(1).strip()
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and "tool" in data:
                return data
        except json.JSONDecodeError:
            # Пробуем найти JSON в сыром тексте (LLM иногда пишет неверный JSON)
            try:
                # Ищем {"tool": ...} в тексте
                json_match = re.search(r'\{[^}]*"tool"\s*:[^}]+\}', raw, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(0))
            except Exception:
                pass
        return None

    def _format_tool_result(self, tool_name: str, result: dict) -> str:
        """Форматирует результат инструмента для передачи в LLM."""
        if result.get("ok") is False:
            error = result.get("error", "неизвестная ошибка")
            return f"TOOL RESULT [{tool_name}]: ОШИБКА — {error}"

        # Убираем "ok" из вывода, форматируем остальное
        data = {k: v for k, v in result.items() if k != "ok"}

        # Ограничиваем размер результата
        result_str = json.dumps(data, ensure_ascii=False, indent=2)
        if len(result_str) > 4000:
            result_str = result_str[:4000] + "\n... (обрезано, слишком длинный результат)"

        return f"TOOL RESULT [{tool_name}]:\n{result_str}"

    def _strip_incomplete_tool_calls(self, content: str) -> str:
        """Удаляет незавершённые/лишние tool_call блоки из финального ответа."""
        # Убираем все ```tool_call блоки из финального ответа
        cleaned = re.sub(r"```tool_call.*?```", "", content, flags=re.DOTALL | re.IGNORECASE)
        return cleaned.strip()

    # ──────────────────────────────────────────────────────────────────
    # Prompt builder
    # ──────────────────────────────────────────────────────────────────

    def _build_prompt(self, request: TaskRequest, prev_feedback: Optional[str]) -> str:
        prompt = request.user_input

        agent_memory = request.context.get("agent_memory", "")
        if agent_memory and agent_memory.strip():
            prompt = f"[Контекст агента]\n{agent_memory.strip()}\n\n[Задача]\n{prompt}"

        if request.context.get("extra_context"):
            prompt = f"Контекст:\n{request.context['extra_context']}\n\nЗадача:\n{prompt}"

        if request.files:
            prompt += f"\n\nУпомянутые файлы: {', '.join(request.files)}"

        # Прикреплённые текстовые файлы — инжектируем в конец промпта
        att_text = request.context.get("attachments_text", "")
        if att_text:
            prompt += f"\n\n--- ПРИКРЕПЛЁННЫЕ ФАЙЛЫ ---\n{att_text}"

        # Прикреплённые картинки — добавляем описание (base64 передаётся отдельно в messages)
        att_images = request.context.get("attachments_images", [])
        if att_images:
            names = ", ".join(a["name"] for a in att_images)
            prompt += f"\n\n[К сообщению прикреплены изображения: {names}]"

        if prev_feedback:
            prompt += (
                f"\n\n---\nКРИТИК ВЫЯВИЛ ПРОБЛЕМЫ:\n{prev_feedback}"
                f"\n\nИСПРАВЬ это в своём ответе."
            )

        return prompt

    # ──────────────────────────────────────────────────────────────────
    # Единый роутер LLM — принимает messages[]
    # ──────────────────────────────────────────────────────────────────

    def _effective_max_tokens(self) -> int:
        """Возвращает лимит токенов для текущего типа задачи (min из конфига и task-лимита)."""
        task_limit = TASK_MAX_TOKENS.get(self._task_key, self.model.max_tokens)
        return min(self.model.max_tokens, task_limit)

    async def _call_llm(self, messages: list[dict]) -> tuple[str, int, int]:
        """Роутит вызов к нужному провайдеру."""
        if self.model.provider == "ollama":
            return await self._call_ollama(messages)
        elif self.model.provider == "groq":
            return await self._call_groq(messages)
        elif self.model.provider == "anthropic":
            return await self._call_anthropic(messages)
        elif self.model.provider in ("openrouter", "cerebras", "together", "novita", "sambanova", "github", "mistral"):
            return await self._call_openai_compat(messages)
        elif self.model.provider == "gemini":
            return await self._call_gemini(messages)
        elif _catalog_endpoint(self.model.provider):
            # Провайдер добавлен динамически из каталога models.dev
            return await self._call_openai_compat(messages)
        else:
            return f"[Провайдер {self.model.provider} не поддерживается]", 0, 0

    # ──────────────────────────────────────────────────────────────────
    # Провайдеры (теперь принимают messages[])
    # ──────────────────────────────────────────────────────────────────

    async def _call_ollama(self, messages: list[dict]) -> tuple[str, int, int]:
        try:
            import httpx
            payload = {
                "model":    self.model.model_id,
                "messages": messages,
                "stream":   False,
                "options":  {
                    "temperature": self.model.temperature,
                    "num_predict": self._effective_max_tokens(),
                },
            }
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post("http://localhost:11434/api/chat", json=payload)
                resp.raise_for_status()
                data      = resp.json()
                tokens_in  = data.get("prompt_eval_count", 0)
                tokens_out = data.get("eval_count", 0)
                return data["message"]["content"], tokens_in, tokens_out
        except Exception as e:
            return f"[Ошибка Ollama: {e}]", 0, 0

    async def _call_groq(self, messages: list[dict]) -> tuple[str, int, int]:
        try:
            from groq import AsyncGroq
            api_key = _skeys.get_api_key("GROQ_API_KEY")
            if not api_key:
                return "[Ошибка: GROQ_API_KEY не задан. Добавь его в .env]", 0, 0

            client = AsyncGroq(api_key=api_key)
            resp = await asyncio.wait_for(
                client.chat.completions.create(
                    model=self.model.model_id,
                    messages=messages,
                    max_tokens=self._effective_max_tokens(),
                    temperature=self.model.temperature,
                ),
                timeout=90.0,
            )
            tokens_in  = resp.usage.prompt_tokens if resp.usage else 0
            tokens_out = resp.usage.completion_tokens if resp.usage else 0
            return resp.choices[0].message.content or "", tokens_in, tokens_out
        except asyncio.TimeoutError:
            return "[Ошибка Groq: timeout 90s — провайдер недоступен]", 0, 0
        except Exception as e:
            return f"[Ошибка Groq: {e}]", 0, 0

    async def _call_anthropic(self, messages: list[dict]) -> tuple[str, int, int]:
        try:
            import anthropic
            api_key = _skeys.get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                return "[Ошибка: ANTHROPIC_API_KEY не задан]", 0, 0

            # Anthropic: system отдельно, messages без system
            system_content = ""
            chat_messages  = []
            for m in messages:
                if m["role"] == "system":
                    system_content = m["content"]
                else:
                    chat_messages.append(m)

            client = anthropic.AsyncAnthropic(api_key=api_key)
            resp = await client.messages.create(
                model=self.model.model_id,
                max_tokens=self._effective_max_tokens(),
                system=system_content,
                messages=chat_messages,
            )
            tokens_in  = resp.usage.input_tokens if resp.usage else 0
            tokens_out = resp.usage.output_tokens if resp.usage else 0
            return resp.content[0].text, tokens_in, tokens_out
        except Exception as e:
            return f"[Ошибка Anthropic: {e}]", 0, 0

    async def _call_gemini(self, messages: list[dict]) -> tuple[str, int, int]:
        """Google Gemini через google-generativeai SDK."""
        try:
            import google.generativeai as genai
            api_key = _skeys.get_api_key("GEMINI_API_KEY")
            if not api_key:
                return "[Ошибка: GEMINI_API_KEY не задан]", 0, 0

            genai.configure(api_key=api_key)

            # Извлекаем system и строим историю
            system_content = ""
            history        = []
            last_user      = ""
            for m in messages:
                if m["role"] == "system":
                    system_content = m["content"]
                elif m["role"] == "user":
                    if history and history[-1]["role"] == "user":
                        history[-1]["parts"] = [history[-1]["parts"][0] + "\n" + m["content"]]
                    else:
                        history.append({"role": "user", "parts": [m["content"]]})
                    last_user = m["content"]
                elif m["role"] == "assistant":
                    history.append({"role": "model", "parts": [m["content"]]})

            model_obj = genai.GenerativeModel(
                model_name=self.model.model_id,
                system_instruction=system_content,
            )

            if len(history) > 1:
                # Многоходовой диалог
                chat  = model_obj.start_chat(history=history[:-1])
                resp  = await chat.send_message_async(
                    last_user,
                    generation_config={
                        "max_output_tokens": self._effective_max_tokens(),
                        "temperature":       self.model.temperature,
                    },
                )
            else:
                resp = await model_obj.generate_content_async(
                    last_user,
                    generation_config={
                        "max_output_tokens": self._effective_max_tokens(),
                        "temperature":       self.model.temperature,
                    },
                )

            text       = resp.text or ""
            tokens_in  = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
            tokens_out = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
            return text, tokens_in, tokens_out
        except Exception as e:
            return f"[Ошибка Gemini: {e}]", 0, 0

    async def _call_openai_compat(self, messages: list[dict]) -> tuple[str, int, int]:
        """OpenAI-совместимый вызов для OpenRouter / Cerebras / Together / Novita / SambaNova / GitHub.

        Пробует native function calling если:
          - self._native_mode is True
          - self._openai_tools is not None
        При native FC результат кодируется как '__NATIVE_TOOL__{json}'.
        """
        ENDPOINTS = {
            "openrouter": ("https://openrouter.ai/api/v1",          "OPENROUTER_API_KEY"),
            "cerebras":   ("https://api.cerebras.ai/v1",            "CEREBRAS_API_KEY"),
            "together":   ("https://api.together.xyz/v1",           "TOGETHER_API_KEY"),
            "novita":     ("https://api.novita.ai/v3/openai",       "NOVITA_API_KEY"),
            "sambanova":  ("https://api.sambanova.ai/v1",           "SAMBANOVA_API_KEY"),
            "github":     ("https://models.inference.ai.azure.com", "GITHUB_TOKEN"),
            "mistral":    ("https://api.mistral.ai/v1",             "MISTRAL_API_KEY"),
        }
        base_url, env_key = ENDPOINTS.get(self.model.provider, ("", ""))
        if not base_url:
            # Провайдер из каталога models.dev — endpoint берём оттуда
            dyn = _catalog_endpoint(self.model.provider)
            if dyn:
                base_url, env_key = dyn
            else:
                return f"[Провайдер {self.model.provider}: endpoint неизвестен]", 0, 0
        try:
            from openai import AsyncOpenAI
            api_key = _skeys.get_api_key(env_key)
            if not api_key:
                return f"[Ошибка: {env_key} не задан]", 0, 0

            extra_headers = {}
            if self.model.provider == "openrouter":
                extra_headers["HTTP-Referer"] = "https://freepalp-ai.local"

            client = AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=90.0, max_retries=0)

            # ── Native function calling ──────────────────────────────
            if self._native_mode and self._openai_tools:
                try:
                    resp = await client.chat.completions.create(
                        model=self.model.model_id,
                        messages=messages,
                        max_tokens=self._effective_max_tokens(),
                        temperature=self.model.temperature,
                        tools=self._openai_tools,
                        tool_choice="auto",
                        extra_headers=extra_headers,
                    )
                    tokens_in  = resp.usage.prompt_tokens  if resp.usage else 0
                    tokens_out = resp.usage.completion_tokens if resp.usage else 0
                    choice = resp.choices[0]

                    # Модель выбрала вызов инструмента
                    if choice.finish_reason == "tool_calls" and choice.message.tool_calls:
                        tc = choice.message.tool_calls[0]
                        self._pending_call_id = tc.id
                        try:
                            args = json.loads(tc.function.arguments or "{}")
                        except json.JSONDecodeError:
                            args = {}
                        encoded = json.dumps({"tool": tc.function.name, "args": args},
                                             ensure_ascii=False)
                        return f"__NATIVE_TOOL__{encoded}", tokens_in, tokens_out

                    # Обычный текстовый ответ
                    self._pending_call_id = None
                    return choice.message.content or "", tokens_in, tokens_out

                except Exception as fc_err:
                    # Если модель не поддерживает FC — fallback на текстовый режим
                    err_s = str(fc_err)
                    if "tool" in err_s.lower() or "function" in err_s.lower() or "400" in err_s:
                        self._native_mode = False  # отключаем для этой сессии
                        self._pending_call_id = None
                    else:
                        raise  # другая ошибка — пробрасываем

            # ── Текстовый режим (fallback) ───────────────────────────
            resp = await client.chat.completions.create(
                model=self.model.model_id,
                messages=messages,
                max_tokens=self._effective_max_tokens(),
                temperature=self.model.temperature,
                extra_headers=extra_headers,
            )
            tokens_in  = resp.usage.prompt_tokens  if resp.usage else 0
            tokens_out = resp.usage.completion_tokens if resp.usage else 0
            return resp.choices[0].message.content or "", tokens_in, tokens_out

        except asyncio.CancelledError:
            return f"[Ошибка {self.model.provider}: timeout/cancelled — провайдер недоступен]", 0, 0
        except Exception as e:
            err = str(e)
            if "429" in err or "rate limit" in err.lower() or "too many" in err.lower():
                return f"[Ошибка {self.model.provider}: rate-limit 429 — провайдер перегружен]", 0, 0
            if "timeout" in err.lower() or "timed out" in err.lower():
                return f"[Ошибка {self.model.provider}: timeout — провайдер недоступен]", 0, 0
            return f"[Ошибка {self.model.provider}: {e}]", 0, 0
