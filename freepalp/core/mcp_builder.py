"""
MCP Builder — генератор шаблонов MCP-серверов.

Создаёт готовый MCP-сервер из описания инструмента.
Поддерживает Python (fastmcp) и Node.js (@modelcontextprotocol/sdk).

Команды:
  /mcp build python <name> <description>  — Python MCP-сервер
  /mcp build node   <name> <description>  — Node.js MCP-сервер
"""

from pathlib import Path
from datetime import datetime


def build_python_mcp(
    name: str,
    description: str,
    tools: list[dict] | None = None,
    output_dir: Path | None = None,
) -> dict:
    """
    Генерирует Python MCP-сервер (использует fastmcp).

    Args:
        name:        имя сервера (snake_case)
        description: описание сервера
        tools:       список инструментов [{"name": str, "description": str, "args": dict}]
        output_dir:  директория для сохранения (по умолчанию: мcp_servers/<name>)

    Returns:
        {"ok": True, "path": str, "files": [...]}
    """
    if tools is None:
        tools = [
            {
                "name":        "example_tool",
                "description": "Пример инструмента — замените на вашу логику",
                "args":        {"input": "str"},
            }
        ]

    safe_name = name.lower().replace(" ", "_").replace("-", "_")
    out = output_dir or Path(__file__).parent.parent / "mcp_servers" / safe_name
    out.mkdir(parents=True, exist_ok=True)

    # Генерируем server.py
    tool_defs = []
    for tool in tools:
        tool_name = tool["name"].lower().replace(" ", "_")
        args_str  = ", ".join(f'{k}: {v}' for k, v in tool.get("args", {}).items())
        args_doc  = "\n        ".join(f"{k}: {v}" for k, v in tool.get("args", {}).items())
        tool_defs.append(f'''
@mcp.tool()
async def {tool_name}({args_str}) -> str:
    """
    {tool.get("description", "")}

    Args:
        {args_doc if args_doc else "нет аргументов"}
    """
    # TODO: реализуйте логику
    return f"{{tool_name}} вызван с аргументами: {{{", ".join(tool.get("args", {}).keys())}}}"
''')

    server_content = f'''"""
MCP Server: {name}
{description}

Сгенерировано FreePalp MCP Builder: {datetime.now().strftime("%Y-%m-%d %H:%M")}

Запуск:
  pip install fastmcp
  python server.py

Подключение в .mcp.json:
  "mcpServers": {{
    "{safe_name}": {{
      "command": "python",
      "args": ["{out / "server.py"}"]
    }}
  }}
"""

import asyncio
from fastmcp import FastMCP

mcp = FastMCP("{name}")

{"".join(tool_defs)}

if __name__ == "__main__":
    mcp.run()
'''

    # requirements.txt
    requirements = "fastmcp>=0.4.0\n"

    # README.md
    readme = f"""# {name} MCP Server

{description}

## Установка

```bash
pip install fastmcp
```

## Запуск

```bash
python server.py
```

## Инструменты

{"".join(f"- **{t['name']}**: {t.get('description', '')}" + chr(10) for t in tools)}

## Подключение к Claude

Добавьте в `.mcp.json`:

```json
{{
  "mcpServers": {{
    "{safe_name}": {{
      "command": "python",
      "args": ["{out / "server.py"}"]
    }}
  }}
}}
```

---
_Создано FreePalp MCP Builder {datetime.now().strftime("%Y-%m-%d")}_
"""

    files_created = []
    for fname, content in [
        ("server.py",       server_content),
        ("requirements.txt", requirements),
        ("README.md",        readme),
    ]:
        fpath = out / fname
        fpath.write_text(content, encoding="utf-8")
        files_created.append(str(fpath))

    return {
        "ok":           True,
        "path":         str(out),
        "files":        files_created,
        "server":       safe_name,
        "tools_count":  len(tools),
    }


def build_node_mcp(
    name: str,
    description: str,
    tools: list[dict] | None = None,
    output_dir: Path | None = None,
) -> dict:
    """
    Генерирует Node.js MCP-сервер (@modelcontextprotocol/sdk).

    Args:
        name:        имя сервера
        description: описание
        tools:       список инструментов
        output_dir:  директория

    Returns:
        {"ok": True, "path": str, "files": [...]}
    """
    if tools is None:
        tools = [
            {
                "name":        "example_tool",
                "description": "Пример инструмента",
                "args":        {"input": "string"},
            }
        ]

    safe_name = name.lower().replace(" ", "-")
    out = output_dir or Path(__file__).parent.parent / "mcp_servers" / safe_name
    out.mkdir(parents=True, exist_ok=True)

    # Генерируем tool schemas
    tool_schemas = []
    tool_handlers = []
    for tool in tools:
        t_name = tool["name"].lower().replace(" ", "_")
        props = {
            k: {"type": v if isinstance(v, str) else "string", "description": f"{k} parameter"}
            for k, v in tool.get("args", {}).items()
        }
        required = list(tool.get("args", {}).keys())

        tool_schemas.append(f'''  {{
    name: "{t_name}",
    description: "{tool.get("description", "")}",
    inputSchema: {{
      type: "object",
      properties: {{"{'", "'.join(f'{k}": {{"type": "string"}}' for k in tool.get("args", {}).keys())}"}},
      required: {required},
    }},
  }},''')

        args_str = ", ".join(f'{{{k}}}' for k in tool.get("args", {}).keys())
        tool_handlers.append(f'''  if (name === "{t_name}") {{
    // TODO: реализуйте логику
    return {{ content: [{{ type: "text", text: `{t_name} called with {args_str}` }}] }};
  }}''')

    server_content = f'''/**
 * MCP Server: {name}
 * {description}
 *
 * Сгенерировано FreePalp MCP Builder: {datetime.now().strftime("%Y-%m-%d %H:%M")}
 */

import {{ Server }} from "@modelcontextprotocol/sdk/server/index.js";
import {{ StdioServerTransport }} from "@modelcontextprotocol/sdk/server/stdio.js";
import {{ CallToolRequestSchema, ListToolsRequestSchema }} from "@modelcontextprotocol/sdk/types.js";

const server = new Server(
  {{ name: "{safe_name}", version: "1.0.0" }},
  {{ capabilities: {{ tools: {{}} }} }}
);

server.setRequestHandler(ListToolsRequestSchema, async () => ({{
  tools: [
{chr(10).join(tool_schemas)}
  ],
}}));

server.setRequestHandler(CallToolRequestSchema, async (request) => {{
  const {{ name, arguments: args }} = request.params;

{chr(10).join(tool_handlers)}

  throw new Error(`Unknown tool: ${{name}}`);
}});

const transport = new StdioServerTransport();
await server.connect(transport);
'''

    package_json = {
        "name":    safe_name,
        "version": "1.0.0",
        "description": description,
        "type":    "module",
        "main":    "server.js",
        "scripts": {"start": "node server.js"},
        "dependencies": {
            "@modelcontextprotocol/sdk": "^1.0.0"
        }
    }

    import json
    files_created = []
    for fname, content in [
        ("server.js",    server_content),
        ("package.json", json.dumps(package_json, indent=2, ensure_ascii=False)),
    ]:
        fpath = out / fname
        fpath.write_text(content, encoding="utf-8")
        files_created.append(str(fpath))

    return {
        "ok":          True,
        "path":        str(out),
        "files":       files_created,
        "server":      safe_name,
        "tools_count": len(tools),
    }
