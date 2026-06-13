"""
MCP-клиент — подключение внешних MCP-серверов (Model Context Protocol).

Зачем: вместо захардкоженных инструментов пользователь подключает ЛЮБОЙ
MCP-сервер (файловая система, GitHub, Slack, БД, сотни готовых) одной записью
в конфиге — и его инструменты появляются у воркера автоматически. Это стандарт
де-факто (Claude Code/Desktop, Cursor, MiMo).

Транспорт: stdio (запускаем сервер как подпроцесс, общаемся JSON-RPC 2.0
построчно — newline-delimited JSON, как требует спецификация MCP).

Безопасно по умолчанию: конфиг пуст → ничего не запускается. Фича opt-in.
Сбой сервера не роняет FreePalp — он просто пропускается.

Конфиг: freepalp/config/mcp_servers.json (формат как у Claude Desktop):
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "C:/path"],
      "env": {}
    }
  }
}
"""
from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any, Optional

_CONFIG = Path(__file__).parent.parent / "config" / "mcp_servers.json"
_PROTOCOL_VERSION = "2024-11-05"


class MCPServer:
    """Один подключённый MCP-сервер (персистентный подпроцесс)."""

    def __init__(self, name: str, command: str, args: list, env: dict):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.proc: Optional[subprocess.Popen] = None
        self.tools: list[dict] = []
        self._id = 0
        self._lock = threading.Lock()

    # ── низкоуровневый JSON-RPC по stdio ──────────────────────────────
    def _send(self, obj: dict) -> None:
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def _read_until(self, want_id: int) -> Optional[dict]:
        """Читает строки, пока не встретит ответ с нужным id (пропускает
        нотификации/логи сервера)."""
        while True:
            raw = self.proc.stdout.readline()
            if not raw:
                return None  # сервер закрылся
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # не JSON — служебный вывод, игнорируем
            if msg.get("id") == want_id:
                return msg

    def _request(self, method: str, params: dict, timeout: float = 30.0) -> dict:
        with self._lock:
            self._id += 1
            rid = self._id
            self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            # readline блокирующий; таймаут реализуем через поток-сторож
            result = {}
            err = {}

            def _worker():
                msg = self._read_until(rid)
                if msg is None:
                    err["e"] = "сервер закрыл соединение"
                elif "error" in msg:
                    err["e"] = msg["error"]
                else:
                    result["r"] = msg.get("result", {})

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout)
            if t.is_alive():
                raise TimeoutError(f"MCP {self.name}.{method}: таймаут {timeout}s")
            if err:
                raise RuntimeError(f"MCP {self.name}.{method}: {err['e']}")
            return result.get("r", {})

    def _notify(self, method: str, params: dict) -> None:
        with self._lock:
            self._send({"jsonrpc": "2.0", "method": method, "params": params})

    # ── жизненный цикл ────────────────────────────────────────────────
    def connect(self) -> bool:
        """Запускает сервер, делает handshake, забирает список инструментов."""
        full_env = {**os.environ, **self.env}
        try:
            self.proc = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=full_env, bufsize=0,
            )
        except FileNotFoundError:
            return False
        except Exception:
            return False

        try:
            self._request("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "FreePalp", "version": "1.1.0"},
            }, timeout=20.0)
            self._notify("notifications/initialized", {})
            listed = self._request("tools/list", {}, timeout=20.0)
            self.tools = listed.get("tools", []) or []
            return True
        except Exception:
            self.close()
            return False

    def call(self, tool: str, arguments: dict) -> dict:
        """Вызывает инструмент сервера, нормализует результат под формат FreePalp."""
        try:
            res = self._request("tools/call",
                                 {"name": tool, "arguments": arguments or {}},
                                 timeout=120.0)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        # MCP-результат: {content: [{type:"text", text:...}], isError: bool}
        parts = []
        for c in res.get("content", []):
            if c.get("type") == "text":
                parts.append(c.get("text", ""))
            else:
                parts.append(json.dumps(c, ensure_ascii=False))
        text = "\n".join(parts)
        if res.get("isError"):
            return {"ok": False, "error": text or "MCP tool error"}
        return {"ok": True, "result": text}

    def close(self) -> None:
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass
            self.proc = None


class MCPManager:
    def __init__(self):
        self.servers: dict[str, MCPServer] = {}

    def load_config(self) -> dict:
        if not _CONFIG.exists():
            return {}
        try:
            return json.loads(_CONFIG.read_text(encoding="utf-8")).get("mcpServers", {})
        except Exception:
            return {}

    def connect_all(self) -> dict:
        """Подключает все сервера из конфига и регистрирует их инструменты в
        ALL_TOOLS под именем mcp__<server>__<tool>. Возвращает сводку."""
        from ..agents.tool_agent import ALL_TOOLS
        summary = {"connected": [], "failed": [], "tools": 0}
        cfg = self.load_config()
        for name, spec in cfg.items():
            srv = MCPServer(name, spec.get("command", ""),
                            spec.get("args", []), spec.get("env", {}))
            if not srv.connect():
                summary["failed"].append(name)
                continue
            self.servers[name] = srv
            for tool in srv.tools:
                tname = tool.get("name", "")
                if not tname:
                    continue
                full = f"mcp__{name}__{tname}"
                schema = tool.get("inputSchema", {}) or {}
                args = list((schema.get("properties") or {}).keys())
                ALL_TOOLS[full] = {
                    "fn": self._make_fn(name, tname),
                    "description": (tool.get("description", "")[:200]
                                    + f" [MCP:{name}]"),
                    "args": args,
                    "async": True,
                    "_mcp": True,
                }
                summary["tools"] += 1
            summary["connected"].append(name)
        return summary

    def _make_fn(self, server: str, tool: str):
        import asyncio

        async def _fn(**kwargs):
            srv = self.servers.get(server)
            if not srv:
                return {"ok": False, "error": f"MCP сервер {server} не подключён"}
            # блокирующий call — в отдельный поток, чтобы не вешать event loop
            return await asyncio.to_thread(srv.call, tool, kwargs)

        return _fn

    def status(self) -> dict:
        return {
            "servers": [
                {"name": n, "tools": [t.get("name") for t in s.tools],
                 "tool_count": len(s.tools)}
                for n, s in self.servers.items()
            ],
            "total_servers": len(self.servers),
            "total_tools": sum(len(s.tools) for s in self.servers.values()),
        }

    def close_all(self) -> None:
        for s in self.servers.values():
            s.close()
        self.servers.clear()


_MGR: Optional[MCPManager] = None


def get() -> MCPManager:
    global _MGR
    if _MGR is None:
        _MGR = MCPManager()
    return _MGR
