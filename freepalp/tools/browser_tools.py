"""
Browser Tools — автоматизация браузера через Playwright.

Использование:
  - Не требует API ключей
  - Требует: pip install playwright && playwright install chromium

Инструменты:
  browser_open      — открыть URL, получить HTML/текст страницы
  browser_click     — кликнуть по CSS-селектору
  browser_fill      — заполнить поле формы
  browser_screenshot — сделать скриншот (base64 PNG)
  browser_eval      — выполнить JavaScript на странице
  browser_extract   — извлечь текст/атрибуты по CSS-селектору
"""

import asyncio
import base64
from typing import Optional


async def _get_playwright():
    """Ленивый импорт Playwright."""
    try:
        from playwright.async_api import async_playwright
        return async_playwright
    except ImportError:
        raise ImportError(
            "Playwright не установлен. Выполните:\n"
            "  pip install playwright\n"
            "  playwright install chromium"
        )


async def browser_open(url: str, wait_for: str = "load", timeout: int = 30000) -> dict:
    """
    Открывает URL и возвращает содержимое страницы.

    Args:
        url: URL для открытия
        wait_for: событие ожидания ("load", "networkidle", "domcontentloaded")
        timeout: таймаут в мс (по умолчанию 30000)

    Returns:
        {"ok": True, "url": str, "title": str, "text": str, "html": str}
    """
    try:
        async_playwright = await _get_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until=wait_for, timeout=timeout)
            title = await page.title()
            text = await page.inner_text("body")
            html = await page.content()
            await browser.close()
            return {
                "ok":    True,
                "url":   url,
                "title": title,
                "text":  text[:5000],   # первые 5000 символов текста
                "html":  html[:20000],  # первые 20К HTML
            }
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_open: {e}"}


async def browser_screenshot(url: str, selector: Optional[str] = None) -> dict:
    """
    Делает скриншот страницы или элемента.

    Args:
        url: URL страницы
        selector: CSS-селектор элемента (None = вся страница)

    Returns:
        {"ok": True, "base64": str, "width": int, "height": int}
    """
    try:
        async_playwright = await _get_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)

            if selector:
                element = await page.query_selector(selector)
                if not element:
                    await browser.close()
                    return {"ok": False, "error": f"Элемент не найден: {selector}"}
                screenshot = await element.screenshot()
            else:
                screenshot = await page.screenshot(full_page=True)

            await browser.close()
            encoded = base64.b64encode(screenshot).decode("utf-8")
            return {
                "ok":     True,
                "base64": encoded,
                "size":   len(screenshot),
                "format": "PNG",
            }
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_screenshot: {e}"}


async def browser_click(url: str, selector: str, wait_after: int = 1000) -> dict:
    """
    Открывает страницу и кликает по элементу.

    Args:
        url: URL страницы
        selector: CSS-селектор для клика
        wait_after: время ожидания после клика в мс

    Returns:
        {"ok": True, "clicked": str, "url_after": str}
    """
    try:
        async_playwright = await _get_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.click(selector)
            await page.wait_for_timeout(wait_after)
            url_after = page.url
            await browser.close()
            return {"ok": True, "clicked": selector, "url_after": url_after}
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_click: {e}"}


async def browser_fill(url: str, selector: str, value: str, submit: bool = False) -> dict:
    """
    Заполняет поле ввода на странице.

    Args:
        url: URL страницы
        selector: CSS-селектор поля
        value: значение для ввода
        submit: нажать Enter после заполнения

    Returns:
        {"ok": True, "filled": str, "url_after": str}
    """
    try:
        async_playwright = await _get_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await page.fill(selector, value)
            if submit:
                await page.press(selector, "Enter")
                await page.wait_for_load_state("networkidle")
            url_after = page.url
            await browser.close()
            return {"ok": True, "filled": selector, "url_after": url_after}
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_fill: {e}"}


async def browser_extract(url: str, selector: str, attribute: Optional[str] = None) -> dict:
    """
    Извлекает текст или атрибут элементов по CSS-селектору.

    Args:
        url: URL страницы
        selector: CSS-селектор
        attribute: имя HTML-атрибута (None = внутренний текст)

    Returns:
        {"ok": True, "results": [str, ...], "count": int}
    """
    try:
        async_playwright = await _get_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            elements = await page.query_selector_all(selector)
            results = []
            for el in elements:
                if attribute:
                    val = await el.get_attribute(attribute)
                else:
                    val = await el.inner_text()
                if val:
                    results.append(val.strip())
            await browser.close()
            return {"ok": True, "results": results, "count": len(results)}
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_extract: {e}"}


async def browser_eval(url: str, script: str) -> dict:
    """
    Выполняет JavaScript на странице и возвращает результат.

    Args:
        url: URL страницы
        script: JavaScript код (должен возвращать значение)

    Returns:
        {"ok": True, "result": any}
    """
    try:
        async_playwright = await _get_playwright()
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(url, wait_until="networkidle", timeout=30000)
            result = await page.evaluate(script)
            await browser.close()
            return {"ok": True, "result": result}
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_eval: {e}"}


# ──────────────────────────────────────────────────────────────────────────────
# Реестр инструментов (подключается к ToolAgent)
# ──────────────────────────────────────────────────────────────────────────────

BROWSER_TOOLS: dict = {
    "browser_open": {
        "description": "Открыть URL и получить текст/HTML страницы",
        "fn":          browser_open,
        "async":       True,
        "args":        {"url": "str", "wait_for": "str (load|networkidle)", "timeout": "int"},
    },
    "browser_screenshot": {
        "description": "Сделать скриншот страницы (возвращает base64 PNG)",
        "fn":          browser_screenshot,
        "async":       True,
        "args":        {"url": "str", "selector": "str (CSS, необязательно)"},
    },
    "browser_click": {
        "description": "Открыть страницу и кликнуть по CSS-элементу",
        "fn":          browser_click,
        "async":       True,
        "args":        {"url": "str", "selector": "str", "wait_after": "int (мс)"},
    },
    "browser_fill": {
        "description": "Заполнить поле формы на странице",
        "fn":          browser_fill,
        "async":       True,
        "args":        {"url": "str", "selector": "str", "value": "str", "submit": "bool"},
    },
    "browser_extract": {
        "description": "Извлечь текст/атрибуты всех элементов по CSS-селектору",
        "fn":          browser_extract,
        "async":       True,
        "args":        {"url": "str", "selector": "str", "attribute": "str (необязательно)"},
    },
    "browser_eval": {
        "description": "Выполнить JavaScript на странице и вернуть результат",
        "fn":          browser_eval,
        "async":       True,
        "args":        {"url": "str", "script": "str"},
    },
}
