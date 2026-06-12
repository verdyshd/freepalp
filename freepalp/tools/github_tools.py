"""
GitHub Tools — интеграция с GitHub API через PyGitHub или REST.

Требует: GITHUB_TOKEN в .env
Опционально: pip install PyGithub

Инструменты:
  github_get_repo       — информация о репозитории
  github_list_issues    — список issues (с фильтрами)
  github_create_issue   — создать issue
  github_get_file       — получить содержимое файла
  github_create_file    — создать/обновить файл
  github_list_prs       — список pull requests
  github_search_code    — поиск по коду
  github_get_commits    — последние коммиты
"""

import os
import json
from typing import Optional

_BASE_URL = "https://api.github.com"


def _headers() -> dict:
    token = os.getenv("GITHUB_TOKEN", "")
    h = {
        "Accept":               "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _require_token() -> Optional[str]:
    token = os.getenv("GITHUB_TOKEN", "")
    if not token:
        return "GITHUB_TOKEN не настроен. Добавьте в .env: GITHUB_TOKEN=ghp_..."
    return None


async def _get(endpoint: str, params: Optional[dict] = None) -> dict:
    import urllib.request
    import urllib.parse
    url = f"{_BASE_URL}{endpoint}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


async def _post(endpoint: str, data: dict) -> dict:
    import urllib.request
    url = f"{_BASE_URL}{endpoint}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={**_headers(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


async def _put(endpoint: str, data: dict) -> dict:
    import urllib.request
    url = f"{_BASE_URL}{endpoint}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="PUT",
        headers={**_headers(), "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


# ──────────────────────────────────────────────────────────────────────────────

async def github_get_repo(repo: str) -> dict:
    """
    Получить информацию о репозитории.

    Args:
        repo: "owner/repo" (например: "microsoft/vscode")

    Returns:
        {"ok": True, "name": str, "description": str, "stars": int, ...}
    """
    err = _require_token()
    if err:
        return {"ok": False, "error": err}
    data = await _get(f"/repos/{repo}")
    if "error" in data or "message" in data:
        return {"ok": False, "error": data.get("error") or data.get("message")}
    return {
        "ok":          True,
        "name":        data.get("full_name"),
        "description": data.get("description"),
        "stars":       data.get("stargazers_count"),
        "forks":       data.get("forks_count"),
        "language":    data.get("language"),
        "open_issues": data.get("open_issues_count"),
        "default_branch": data.get("default_branch"),
        "url":         data.get("html_url"),
        "private":     data.get("private"),
        "created_at":  data.get("created_at"),
        "updated_at":  data.get("updated_at"),
    }


async def github_list_issues(
    repo: str,
    state: str = "open",
    labels: Optional[str] = None,
    limit: int = 20,
) -> dict:
    """
    Список issues репозитория.

    Args:
        repo:   "owner/repo"
        state:  "open" | "closed" | "all"
        labels: фильтр по меткам (через запятую)
        limit:  максимум issues

    Returns:
        {"ok": True, "issues": [...], "total": int}
    """
    err = _require_token()
    if err:
        return {"ok": False, "error": err}
    data = await _get(f"/repos/{repo}/issues", {
        "state":    state,
        "labels":   labels,
        "per_page": min(limit, 100),
    })
    if isinstance(data, dict) and ("error" in data or "message" in data):
        return {"ok": False, "error": data.get("error") or data.get("message")}
    issues = []
    for item in (data if isinstance(data, list) else []):
        if item.get("pull_request"):  # пропускаем PR
            continue
        issues.append({
            "number":  item.get("number"),
            "title":   item.get("title"),
            "state":   item.get("state"),
            "labels":  [l["name"] for l in item.get("labels", [])],
            "author":  item.get("user", {}).get("login"),
            "created": item.get("created_at"),
            "url":     item.get("html_url"),
        })
    return {"ok": True, "issues": issues[:limit], "total": len(issues)}


async def github_create_issue(
    repo: str,
    title: str,
    body: str = "",
    labels: Optional[list] = None,
    assignees: Optional[list] = None,
) -> dict:
    """
    Создать issue в репозитории.

    Args:
        repo:      "owner/repo"
        title:     заголовок issue
        body:      описание (поддерживает Markdown)
        labels:    список меток
        assignees: список GitHub логинов

    Returns:
        {"ok": True, "number": int, "url": str}
    """
    err = _require_token()
    if err:
        return {"ok": False, "error": err}
    payload: dict = {"title": title, "body": body}
    if labels:
        payload["labels"] = labels
    if assignees:
        payload["assignees"] = assignees
    data = await _post(f"/repos/{repo}/issues", payload)
    if "error" in data or "message" in data:
        return {"ok": False, "error": data.get("error") or data.get("message")}
    return {
        "ok":     True,
        "number": data.get("number"),
        "url":    data.get("html_url"),
        "title":  data.get("title"),
    }


async def github_get_file(repo: str, path: str, branch: str = "main") -> dict:
    """
    Получить содержимое файла из репозитория.

    Args:
        repo:   "owner/repo"
        path:   путь к файлу (например: "README.md")
        branch: ветка (по умолчанию "main")

    Returns:
        {"ok": True, "content": str, "sha": str, "size": int}
    """
    err = _require_token()
    if err:
        return {"ok": False, "error": err}
    data = await _get(f"/repos/{repo}/contents/{path}", {"ref": branch})
    if isinstance(data, dict) and ("error" in data or "message" in data):
        return {"ok": False, "error": data.get("error") or data.get("message")}
    import base64
    content_b64 = data.get("content", "").replace("\n", "")
    try:
        content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        content = content_b64
    return {
        "ok":      True,
        "content": content,
        "sha":     data.get("sha"),
        "size":    data.get("size"),
        "url":     data.get("html_url"),
    }


async def github_create_file(
    repo: str,
    path: str,
    content: str,
    message: str,
    branch: str = "main",
    sha: Optional[str] = None,
) -> dict:
    """
    Создать или обновить файл в репозитории.

    Args:
        repo:    "owner/repo"
        path:    путь к файлу
        content: содержимое файла (plain text)
        message: сообщение коммита
        branch:  ветка
        sha:     SHA текущего файла (нужно для обновления, None для создания)

    Returns:
        {"ok": True, "url": str, "sha": str, "commit": str}
    """
    err = _require_token()
    if err:
        return {"ok": False, "error": err}
    import base64
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("utf-8")
    payload: dict = {
        "message": message,
        "content": content_b64,
        "branch":  branch,
    }
    if sha:
        payload["sha"] = sha
    data = await _put(f"/repos/{repo}/contents/{path}", payload)
    if isinstance(data, dict) and ("error" in data or "message" in data):
        return {"ok": False, "error": data.get("error") or data.get("message")}
    commit = data.get("commit", {})
    return {
        "ok":    True,
        "url":   data.get("content", {}).get("html_url"),
        "sha":   data.get("content", {}).get("sha"),
        "commit": commit.get("sha", ""),
    }


async def github_list_prs(
    repo: str,
    state: str = "open",
    limit: int = 20,
) -> dict:
    """
    Список pull requests репозитория.

    Args:
        repo:  "owner/repo"
        state: "open" | "closed" | "all"
        limit: максимум PR

    Returns:
        {"ok": True, "prs": [...], "total": int}
    """
    err = _require_token()
    if err:
        return {"ok": False, "error": err}
    data = await _get(f"/repos/{repo}/pulls", {"state": state, "per_page": min(limit, 100)})
    if isinstance(data, dict) and ("error" in data or "message" in data):
        return {"ok": False, "error": data.get("error") or data.get("message")}
    prs = []
    for item in (data if isinstance(data, list) else []):
        prs.append({
            "number":  item.get("number"),
            "title":   item.get("title"),
            "state":   item.get("state"),
            "author":  item.get("user", {}).get("login"),
            "head":    item.get("head", {}).get("ref"),
            "base":    item.get("base", {}).get("ref"),
            "created": item.get("created_at"),
            "url":     item.get("html_url"),
        })
    return {"ok": True, "prs": prs[:limit], "total": len(prs)}


async def github_search_code(query: str, repo: Optional[str] = None, limit: int = 10) -> dict:
    """
    Поиск по коду на GitHub.

    Args:
        query: поисковый запрос (например: "def process_file language:python")
        repo:  ограничить поиск репозиторием "owner/repo" (необязательно)
        limit: максимум результатов

    Returns:
        {"ok": True, "results": [...], "total": int}
    """
    err = _require_token()
    if err:
        return {"ok": False, "error": err}
    q = query
    if repo:
        q += f" repo:{repo}"
    data = await _get("/search/code", {"q": q, "per_page": min(limit, 30)})
    if isinstance(data, dict) and ("error" in data or "message" in data):
        return {"ok": False, "error": data.get("error") or data.get("message")}
    items = data.get("items", [])
    results = []
    for item in items:
        results.append({
            "name":       item.get("name"),
            "path":       item.get("path"),
            "repository": item.get("repository", {}).get("full_name"),
            "url":        item.get("html_url"),
        })
    return {"ok": True, "results": results, "total": data.get("total_count", 0)}


async def github_get_commits(repo: str, branch: str = "main", limit: int = 10) -> dict:
    """
    Последние коммиты репозитория.

    Args:
        repo:   "owner/repo"
        branch: ветка
        limit:  количество коммитов

    Returns:
        {"ok": True, "commits": [...]}
    """
    err = _require_token()
    if err:
        return {"ok": False, "error": err}
    data = await _get(f"/repos/{repo}/commits", {"sha": branch, "per_page": min(limit, 100)})
    if isinstance(data, dict) and ("error" in data or "message" in data):
        return {"ok": False, "error": data.get("error") or data.get("message")}
    commits = []
    for item in (data if isinstance(data, list) else [])[:limit]:
        commit = item.get("commit", {})
        commits.append({
            "sha":     item.get("sha", "")[:7],
            "message": commit.get("message", "").split("\n")[0],
            "author":  commit.get("author", {}).get("name"),
            "date":    commit.get("author", {}).get("date"),
            "url":     item.get("html_url"),
        })
    return {"ok": True, "commits": commits}


# ──────────────────────────────────────────────────────────────────────────────
# Реестр инструментов
# ──────────────────────────────────────────────────────────────────────────────

GITHUB_TOOLS: dict = {
    "github_get_repo": {
        "description": "Информация о GitHub репозитории (stars, forks, language...)",
        "fn":          github_get_repo,
        "async":       True,
        "args":        {"repo": "str (owner/repo)"},
    },
    "github_list_issues": {
        "description": "Список issues репозитория с фильтрами",
        "fn":          github_list_issues,
        "async":       True,
        "args":        {"repo": "str", "state": "open|closed|all", "labels": "str", "limit": "int"},
    },
    "github_create_issue": {
        "description": "Создать issue в GitHub репозитории",
        "fn":          github_create_issue,
        "async":       True,
        "args":        {"repo": "str", "title": "str", "body": "str", "labels": "list"},
    },
    "github_get_file": {
        "description": "Получить содержимое файла из GitHub репозитория",
        "fn":          github_get_file,
        "async":       True,
        "args":        {"repo": "str", "path": "str", "branch": "str"},
    },
    "github_create_file": {
        "description": "Создать или обновить файл в GitHub репозитории",
        "fn":          github_create_file,
        "async":       True,
        "args":        {"repo": "str", "path": "str", "content": "str", "message": "str", "branch": "str"},
    },
    "github_list_prs": {
        "description": "Список pull requests репозитория",
        "fn":          github_list_prs,
        "async":       True,
        "args":        {"repo": "str", "state": "open|closed|all", "limit": "int"},
    },
    "github_search_code": {
        "description": "Поиск по коду на GitHub",
        "fn":          github_search_code,
        "async":       True,
        "args":        {"query": "str", "repo": "str (необязательно)", "limit": "int"},
    },
    "github_get_commits": {
        "description": "Последние коммиты репозитория",
        "fn":          github_get_commits,
        "async":       True,
        "args":        {"repo": "str", "branch": "str", "limit": "int"},
    },
}
