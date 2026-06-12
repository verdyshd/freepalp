"""
MCP Discovery — сканирует окружение и находит доступные MCP-серверы.

Проверяет:
  1. .env / os.environ на MCP_* переменные
  2. ~/.claude/claude_desktop_config.json (Claude Desktop)
  3. .mcp.json в текущем проекте
  4. mcp_servers.json в config/

Команды CLI:
  /mcp list       — найденные серверы
  /mcp add        — интерактивно добавить конфиг
  /mcp template   — сгенерировать шаблон mcp_servers.json
"""

import json
import os
from pathlib import Path
from typing import Optional

_CONFIG_PATH = Path(__file__).parent.parent / "config" / "mcp_servers.json"
_PROJECT_MCP = Path(__file__).parent.parent / ".mcp.json"


# ──────────────────────────────────────────────────────────────────────────────
# Дефолтные известные MCP-серверы (можно подключить сразу)
# ──────────────────────────────────────────────────────────────────────────────
KNOWN_MCP_SERVERS = [
    {
        "id":          "filesystem",
        "name":        "Filesystem",
        "description": "Чтение/запись файлов через MCP",
        "npm_package": "@modelcontextprotocol/server-filesystem",
        "env_key":     None,
        "command":     "npx @modelcontextprotocol/server-filesystem {allowed_dir}",
    },
    {
        "id":          "github",
        "name":        "GitHub MCP",
        "description": "GitHub API через MCP: репозитории, issues, PR",
        "npm_package": "@modelcontextprotocol/server-github",
        "env_key":     "GITHUB_TOKEN",
        "command":     "npx @modelcontextprotocol/server-github",
    },
    {
        "id":          "brave_search",
        "name":        "Brave Search",
        "description": "Поиск через Brave Search API",
        "npm_package": "@modelcontextprotocol/server-brave-search",
        "env_key":     "BRAVE_API_KEY",
        "command":     "npx @modelcontextprotocol/server-brave-search",
    },
    {
        "id":          "memory",
        "name":        "MCP Memory",
        "description": "Knowledge graph память через MCP",
        "npm_package": "@modelcontextprotocol/server-memory",
        "env_key":     None,
        "command":     "npx @modelcontextprotocol/server-memory",
    },
    {
        "id":          "puppeteer",
        "name":        "Puppeteer Browser",
        "description": "Браузерная автоматизация через MCP",
        "npm_package": "@modelcontextprotocol/server-puppeteer",
        "env_key":     None,
        "command":     "npx @modelcontextprotocol/server-puppeteer",
    },
    {
        "id":          "slack",
        "name":        "Slack MCP",
        "description": "Slack API через MCP",
        "npm_package": "@modelcontextprotocol/server-slack",
        "env_key":     "SLACK_BOT_TOKEN",
        "command":     "npx @modelcontextprotocol/server-slack",
    },
    {
        "id":          "notion",
        "name":        "Notion MCP",
        "description": "Notion API через MCP",
        "npm_package": "@modelcontextprotocol/server-notion",
        "env_key":     "NOTION_API_KEY",
        "command":     "npx @modelcontextprotocol/server-notion",
    },
]


def _load_config_file() -> list[dict]:
    """Загружает config/mcp_servers.json."""
    if not _CONFIG_PATH.exists():
        return []
    try:
        return json.loads(_CONFIG_PATH.read_text("utf-8"))
    except Exception:
        return []


def _load_project_mcp() -> list[dict]:
    """Загружает .mcp.json из корня проекта."""
    if not _PROJECT_MCP.exists():
        return []
    try:
        data = json.loads(_PROJECT_MCP.read_text("utf-8"))
        # Формат .mcp.json: {"mcpServers": {"name": {"command": ..., "args": [...]}}}
        servers = []
        for name, cfg in data.get("mcpServers", {}).items():
            servers.append({
                "id":          name,
                "name":        name,
                "description": "из .mcp.json",
                "command":     cfg.get("command", ""),
                "args":        cfg.get("args", []),
                "env":         cfg.get("env", {}),
                "source":      ".mcp.json",
            })
        return servers
    except Exception:
        return []


def _load_claude_desktop() -> list[dict]:
    """Загружает ~/.claude/claude_desktop_config.json (Claude Desktop)."""
    home = Path.home()
    candidates = [
        home / ".claude" / "claude_desktop_config.json",
        home / "AppData" / "Roaming" / "Claude" / "claude_desktop_config.json",
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    ]
    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text("utf-8"))
                servers = []
                for name, cfg in data.get("mcpServers", {}).items():
                    servers.append({
                        "id":          name,
                        "name":        name,
                        "description": "из Claude Desktop config",
                        "command":     cfg.get("command", ""),
                        "args":        cfg.get("args", []),
                        "env":         cfg.get("env", {}),
                        "source":      str(p),
                    })
                return servers
            except Exception:
                pass
    return []


def _scan_env() -> list[dict]:
    """Ищет MCP_SERVER_* переменные окружения."""
    servers = []
    for key, val in os.environ.items():
        if key.startswith("MCP_SERVER_"):
            name = key[len("MCP_SERVER_"):].lower()
            servers.append({
                "id":          name,
                "name":        name,
                "description": f"из env {key}",
                "command":     val,
                "source":      "env",
            })
    return servers


def discover_mcp_servers() -> dict:
    """
    Собирает все MCP-серверы из всех источников.
    Возвращает:
      {
        "configured": [...],   # из конфигов/env (готовы к запуску)
        "available":  [...],   # известные, но не настроены
        "sources":    [...]    # откуда взяли
      }
    """
    sources = []

    # Собираем сконфигурированные
    configured = []

    env_servers = _scan_env()
    if env_servers:
        configured.extend(env_servers)
        sources.append("env")

    project_servers = _load_project_mcp()
    if project_servers:
        configured.extend(project_servers)
        sources.append(".mcp.json")

    desktop_servers = _load_claude_desktop()
    if desktop_servers:
        configured.extend(desktop_servers)
        sources.append("claude_desktop_config")

    config_servers = _load_config_file()
    if config_servers:
        configured.extend(config_servers)
        sources.append("config/mcp_servers.json")

    # Убираем дубли по id
    seen = set()
    unique_configured = []
    for s in configured:
        if s["id"] not in seen:
            seen.add(s["id"])
            unique_configured.append(s)

    # Известные, но не настроенные
    available = []
    for k in KNOWN_MCP_SERVERS:
        env_key = k.get("env_key")
        is_configured = k["id"] in seen
        has_key = (not env_key) or bool(os.getenv(env_key))
        available.append({
            **k,
            "configured":  is_configured,
            "has_api_key": has_key,
            "ready":       has_key and not is_configured,
        })

    return {
        "configured": unique_configured,
        "available":  available,
        "sources":    sources,
    }


def generate_mcp_template() -> str:
    """Генерирует шаблон .mcp.json для проекта."""
    template = {
        "mcpServers": {}
    }
    for s in KNOWN_MCP_SERVERS:
        env_key = s.get("env_key")
        entry: dict = {
            "command": "npx",
            "args":    ["-y", s["npm_package"]],
        }
        if env_key:
            entry["env"] = {env_key: f"${{{env_key}}}"}
        template["mcpServers"][s["id"]] = entry

    return json.dumps(template, indent=2, ensure_ascii=False)


def save_mcp_server(server: dict):
    """Сохраняет конфиг MCP-сервера в config/mcp_servers.json."""
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    servers = _load_config_file()
    # Заменяем если уже есть с таким id
    servers = [s for s in servers if s.get("id") != server.get("id")]
    servers.append(server)
    _CONFIG_PATH.write_text(json.dumps(servers, indent=2, ensure_ascii=False), "utf-8")
