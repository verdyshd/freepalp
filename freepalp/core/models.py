"""
Core data models for the QClaw AI Orchestration System.
All shared data structures are defined here.
"""

from enum import Enum
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field
from datetime import datetime
import uuid


class TaskType(Enum):
    CODING_SMALL = "coding_small"
    CODING_LARGE = "coding_large"
    ARCHITECTURE = "architecture"
    TEXT = "text"
    SEARCH = "search"
    FILE_OPS = "file_ops"
    SHELL = "shell"
    REVIEW = "review"
    GENERAL = "general"


class TaskStatus(Enum):
    PENDING = "pending"
    ROUTING = "routing"
    IN_PROGRESS = "in_progress"
    CRITIC_CHECK = "critic_check"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


class ModelTier(Enum):
    LOCAL_SMALL = "local_small"    # 7B  — локальный Ollama
    LOCAL_LARGE = "local_large"    # 13B+ — локальный Ollama
    CLOUD_FAST = "cloud_fast"      # Groq 70B
    CLOUD_HEAVY = "cloud_heavy"    # Claude / GPT-4


@dataclass
class ModelConfig:
    name: str
    tier: ModelTier
    provider: str           # "ollama" | "groq" | "anthropic" | "openai"
    model_id: str
    max_tokens: int = 4096
    temperature: float = 0.3
    available: bool = True
    cost_per_1k: float = 0.0
    context_window: int = 8192   # размер окна контекста (для маршрутизации больших задач)


@dataclass
class TaskRequest:
    user_input: str
    task_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    task_type: Optional[TaskType] = None
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    max_retries: int = 3
    files: List[str] = field(default_factory=list)


@dataclass
class AgentMessage:
    role: str               # "worker" | "critic" | "architect" | "tool"
    content: str
    model_used: str = ""
    tokens_used: int = 0
    iteration: int = 0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CriticFeedback:
    passed: bool
    score: float            # 0.0 — 1.0
    issues: List[str] = field(default_factory=list)
    suggestions: List[str] = field(default_factory=list)
    must_retry: bool = False


@dataclass
class TaskResult:
    task_id: str
    status: TaskStatus
    final_answer: str = ""
    iterations: int = 0
    messages: List[AgentMessage] = field(default_factory=list)
    critic_feedback: Optional[CriticFeedback] = None
    error: Optional[str] = None
    files_created: List[str] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    model_used: str = ""


@dataclass
class DAGNode:
    node_id: str
    task_type: TaskType
    description: str
    depends_on: List[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    result: Optional[TaskResult] = None


@dataclass
class ExecutionGraph:
    graph_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    nodes: List[DAGNode] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())

