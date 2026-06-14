"""
Web Tools — поиск в интернете и загрузка страниц.
"""

import asyncio
import re
from typing import Optional

from ..core.sanitize import neutralize_untrusted, wrap_untrusted


async def web_search(query: str, max_results: int = 5) -> dict:
    """
    Поиск через DuckDuckGo (не требует API ключа).
    """
    try:
        import httpx
        # DuckDuckGo Lite HTML поиск
        url = "https://lite.duckduckgo.com/lite/"
        params = {"q": query, "kl": "wt-wt"}
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; FreePalp-AI/1.0)"
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, data=params, headers=headers)
            resp.raise_for_status()
            html = resp.text

        results = _parse_ddg_results(html, max_results)
        # T4: дефанг control-токенов в недоверенных сниппетах/заголовках
        for r in results:
            if r.get("title"):
                r["title"] = neutralize_untrusted(r["title"])
            if r.get("snippet"):
                r["snippet"] = neutralize_untrusted(r["snippet"])
        return {"ok": True, "results": results, "query": query}

    except ImportError:
        return {"ok": False, "error": "httpx не установлен. pip install httpx"}
    except Exception as e:
        return {"ok": False, "error": str(e), "results": []}


async def fetch_page(url: str, max_chars: int = 3000) -> dict:
    """
    Загружает страницу и возвращает текст (без HTML тегов).
    """
    try:
        import httpx
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "error": "URL должен начинаться с http:// или https://"}

        headers = {"User-Agent": "Mozilla/5.0 (compatible; FreePalp-AI/1.0)"}
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            html = resp.text

        text = _strip_html(html)
        text = _clean_text(text)

        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n[... текст обрезан до {max_chars} символов]"

        # T4: контент страницы — недоверенный; оборачиваем в баннер «данные, не инструкции»
        text = wrap_untrusted(text, source=f"web page {url}")
        return {"ok": True, "url": url, "content": text, "length": len(text)}

    except ImportError:
        return {"ok": False, "error": "httpx не установлен"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _parse_ddg_results(html: str, max_results: int) -> list[dict]:
    """Простой парсер результатов DuckDuckGo Lite."""
    results = []
    # Ищем ссылки и сниппеты
    link_pattern = r'<a[^>]+href="([^"]+)"[^>]*>([^<]+)</a>'
    snippet_pattern = r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>'

    links = re.findall(link_pattern, html)
    snippets = re.findall(snippet_pattern, html, re.DOTALL)

    seen_urls = set()
    for i, (url, title) in enumerate(links):
        if not url.startswith("http"):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        snippet = ""
        if i < len(snippets):
            snippet = _strip_html(snippets[i]).strip()

        results.append({
            "title": title.strip(),
            "url": url,
            "snippet": snippet,
        })

        if len(results) >= max_results:
            break

    return results


def _strip_html(html: str) -> str:
    """Удаляет HTML теги."""
    clean = re.sub(r'<[^>]+>', ' ', html)
    clean = re.sub(r'&[a-zA-Z]+;', ' ', clean)
    clean = re.sub(r'&#\d+;', ' ', clean)
    return clean


def _clean_text(text: str) -> str:
    """Очищает текст от лишних пробелов и пустых строк."""
    lines = text.split('\n')
    lines = [l.strip() for l in lines if l.strip()]
    # Убрать дублирующиеся строки подряд
    result = []
    prev = None
    for line in lines:
        if line != prev:
            result.append(line)
        prev = line
    return '\n'.join(result)


# Реестр инструментов
async def deep_research(topic: str, queries: Optional[list] = None,
                        max_pages: int = 3) -> dict:
    """Глубокое исследование за один вызов: многоугловой поиск + выжимка из
    топ-страниц + список источников. LLM потом синтезирует отчёт с цитатами.

    Аргументы:
      topic   — тема исследования (str)
      queries — список поисковых углов (list[str]); если пусто — берём тему
      max_pages — сколько страниц прогрузить ради контента (default 3)
    Returns: {ok, topic, findings:[{query, results}], pages:[{url,title,excerpt}],
              sources:[url]}
    """
    try:
        # Нормализуем углы поиска (макс 5)
        if isinstance(queries, str):
            queries = [queries]
        angles = [q for q in (queries or []) if q and q.strip()][:5] or [topic]

        findings = []
        seen_urls: list[str] = []
        for q in angles:
            r = await web_search(q, max_results=4)
            res = r.get("results", []) if r.get("ok") else []
            findings.append({"query": q, "results": res})
            for item in res:
                u = item.get("url") or item.get("href")
                if u and u not in seen_urls:
                    seen_urls.append(u)

        # Грузим топ-страницы ради реального контента (не только сниппетов)
        pages = []
        for u in seen_urls[:max_pages]:
            p = await fetch_page(u, max_chars=2000)
            if p.get("ok"):
                title = next((it.get("title", "") for f in findings
                              for it in f["results"] if (it.get("url") or it.get("href")) == u), "")
                pages.append({"url": u, "title": title, "excerpt": p.get("content", "")})

        return {"ok": True, "topic": topic, "angles": angles,
                "findings": findings, "pages": pages, "sources": seen_urls[:12]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


WEB_TOOLS = {
    "web_search": {
        "fn": web_search,
        "description": "Поиск в интернете. Аргументы: query (str), max_results (int, default=5)",
        "args": ["query"],
        "async": True,
    },
    "fetch_page": {
        "fn": fetch_page,
        "description": "Загружает веб-страницу. Аргументы: url (str), max_chars (int, default=3000)",
        "args": ["url"],
        "async": True,
    },
    "deep_research": {
        "fn": deep_research,
        "description": "Глубокое исследование темы за один вызов: многоугловой поиск + выжимка страниц + источники. Аргументы: topic (str), queries (list[str] — углы поиска, опционально). Используй для запросов «исследуй», «собери информацию», «сделай обзор». Потом синтезируй отчёт с цитатами на источники.",
        "args": ["topic", "queries"],
        "async": True,
    },
}

