"""
Architect Agent — разбивает сложную задачу на подзадачи (DAG).
Используется только для CODING_LARGE и ARCHITECTURE задач.
"""

import os
import json
import time
from ..core import session_keys as _skeys
from ..core.models import (
    TaskRequest, TaskType, ModelConfig, AgentMessage,
    DAGNode, ExecutionGraph
)

ARCHITECT_SYSTEM = """Ты технический архитектор и project manager.
Твоя задача — разбить сложную задачу на конкретные шаги выполнения.

Формат ответа СТРОГО JSON:
{
  "steps": [
    {
      "id": "step_1",
      "type": "coding_small|architecture|text|file_ops",
      "description": "Что именно нужно сделать",
      "depends_on": []
    },
    {
      "id": "step_2",
      "type": "coding_small",
      "description": "...",
      "depends_on": ["step_1"]
    }
  ]
}

Правила:
- Максимум 6 шагов для одной задачи
- Каждый шаг должен быть выполнимым самостоятельно
- depends_on — список id шагов, от которых зависит этот шаг
- Начальные шаги имеют пустой depends_on
"""


# Задачи, которые требуют декомпозиции
NEEDS_DECOMPOSITION = {TaskType.CODING_LARGE, TaskType.ARCHITECTURE}


class ArchitectAgent:

    def __init__(self, model_config: ModelConfig):
        self.model = model_config

    def needs_planning(self, request: TaskRequest) -> bool:
        """Проверяет, нужна ли декомпозиция задачи."""
        if request.task_type not in NEEDS_DECOMPOSITION:
            return False
        complexity = request.context.get("complexity", 1)
        return complexity >= 3

    async def plan(self, request: TaskRequest) -> tuple[AgentMessage, ExecutionGraph]:
        """
        Строит план выполнения (DAG) для сложной задачи.
        """
        user_prompt = f"Разбей эту задачу на шаги:\n\n{request.user_input}"
        start = time.time()

        if self.model.provider == "ollama":
            raw = await self._call_ollama(user_prompt)
        elif self.model.provider == "groq":
            raw = await self._call_groq(user_prompt)
        elif self.model.provider == "anthropic":
            raw = await self._call_anthropic(user_prompt)
        else:
            raw = '{"steps": []}'

        elapsed = time.time() - start
        graph = self._parse_graph(raw, request)

        msg = AgentMessage(
            role="architect",
            content=raw,
            model_used=self.model.model_id,
            metadata={"elapsed": round(elapsed, 2), "steps": len(graph.nodes)},
        )
        return msg, graph

    # ------------------------------------------------------------------

    def _parse_graph(self, raw: str, request: TaskRequest) -> ExecutionGraph:
        graph = ExecutionGraph()
        try:
            # Ищем JSON в ответе (модель может добавить текст вокруг)
            start_idx = raw.find("{")
            end_idx = raw.rfind("}") + 1
            if start_idx == -1:
                return self._fallback_graph(request)

            data = json.loads(raw[start_idx:end_idx])
            steps = data.get("steps", [])

            for step in steps:
                task_type_str = step.get("type", "coding_small")
                try:
                    task_type = TaskType(task_type_str)
                except ValueError:
                    task_type = TaskType.GENERAL

                node = DAGNode(
                    node_id=step["id"],
                    task_type=task_type,
                    description=step.get("description", ""),
                    depends_on=step.get("depends_on", []),
                )
                graph.nodes.append(node)

        except Exception:
            return self._fallback_graph(request)

        return graph

    def _fallback_graph(self, request: TaskRequest) -> ExecutionGraph:
        """Если архитектор не смог спланировать — выполняем как один шаг."""
        graph = ExecutionGraph()
        graph.nodes.append(DAGNode(
            node_id="step_1",
            task_type=request.task_type or TaskType.GENERAL,
            description=request.user_input,
            depends_on=[],
        ))
        return graph

    # ------------------------------------------------------------------
    # Провайдеры
    # ------------------------------------------------------------------

    async def _call_ollama(self, user: str) -> str:
        try:
            import httpx
            payload = {
                "model": self.model.model_id,
                "messages": [
                    {"role": "system", "content": ARCHITECT_SYSTEM},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                "options": {"temperature": 0.1, "num_predict": 2048},
            }
            async with httpx.AsyncClient(timeout=90.0) as client:
                resp = await client.post(
                    "http://localhost:11434/api/chat", json=payload
                )
                resp.raise_for_status()
                return resp.json()["message"]["content"]
        except Exception as e:
            return '{"steps": []}'

    async def _call_groq(self, user: str) -> str:
        try:
            from groq import AsyncGroq
            api_key = _skeys.get_api_key("GROQ_API_KEY")
            if not api_key:
                return '{"steps": []}'
            client = AsyncGroq(api_key=api_key)
            resp = await client.chat.completions.create(
                model=self.model.model_id,
                messages=[
                    {"role": "system", "content": ARCHITECT_SYSTEM},
                    {"role": "user", "content": user},
                ],
                max_tokens=2048,
                temperature=0.1,
            )
            return resp.choices[0].message.content or '{"steps": []}'
        except Exception:
            return '{"steps": []}'

    async def _call_anthropic(self, user: str) -> str:
        try:
            import anthropic
            api_key = _skeys.get_api_key("ANTHROPIC_API_KEY")
            if not api_key:
                return '{"steps": []}'
            client = anthropic.AsyncAnthropic(api_key=api_key)
            resp = await client.messages.create(
                model=self.model.model_id,
                max_tokens=2048,
                system=ARCHITECT_SYSTEM,
                messages=[{"role": "user", "content": user}],
            )
            return resp.content[0].text
        except Exception:
            return '{"steps": []}'
