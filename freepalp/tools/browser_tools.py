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

WATCH-MODE (FREEPALP_BROWSER_WATCH=1):
  Браузер запускается ВИДИМЫМ отдельным окном (headless=False), одно окно на
  всю сессию (persistent), с замедлением и оверлеем: «личный курсор» агента +
  подсветка элемента перед действием. Человек видит, ГДЕ агент и ЧТО он делает,
  но управление своим ПК/курсором НЕ теряет — это отдельное окно агента.
  По умолчанию (флаг не выставлен) — прежнее поведение: headless, без окна.
"""

import asyncio
import base64
import os
from typing import Optional


def _watch_on() -> bool:
    return os.environ.get("FREEPALP_BROWSER_WATCH", "0").strip().lower() in ("1", "true", "yes", "on")


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


# ── Оверлей watch-mode: курсор-«осьминог» + подсветка элемента ────────────────
_OVERLAY_JS = r"""
(() => {
  if (window.__fpOverlay) return; window.__fpOverlay = true;
  const cur = document.createElement('div'); cur.id = '__fp-cur';
  cur.style.cssText = 'position:fixed;z-index:2147483647;left:24px;top:24px;width:24px;height:24px;'
    + 'border-radius:50%;background:radial-gradient(circle at 34% 30%,#d8b4fe,#7c3aed);border:2px solid #fff;'
    + 'box-shadow:0 0 14px rgba(124,58,237,.85);pointer-events:none;opacity:.95;'
    + 'transition:left .45s cubic-bezier(.3,1,.3,1),top .45s cubic-bezier(.3,1,.3,1);';
  const tag = document.createElement('div'); tag.id = '__fp-tag'; tag.textContent = '🐙 FreePalp';
  tag.style.cssText = 'position:fixed;z-index:2147483647;left:52px;top:18px;background:rgba(124,58,237,.96);'
    + 'color:#fff;font:600 12px system-ui,sans-serif;padding:3px 9px;border-radius:8px;pointer-events:none;'
    + 'box-shadow:0 4px 14px rgba(0,0,0,.32);transition:left .45s,top .45s;white-space:nowrap;';
  const st = document.createElement('style');
  st.textContent = '@keyframes __fpPulse{0%{box-shadow:0 0 0 0 rgba(124,58,237,.55)}100%{box-shadow:0 0 0 14px rgba(124,58,237,0)}}';
  document.documentElement.appendChild(st);
  document.documentElement.appendChild(cur);
  document.documentElement.appendChild(tag);
  window.__fpHi = (sel, label) => {
    const cur = document.getElementById('__fp-cur'), tag = document.getElementById('__fp-tag');
    let el = null; try { el = document.querySelector(sel); } catch (e) {}
    const r = el ? el.getBoundingClientRect() : { left: 24, top: 24, width: 0, height: 0 };
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cur) { cur.style.left = cx + 'px'; cur.style.top = cy + 'px'; }
    if (tag && label) { tag.textContent = '🐙 ' + label; tag.style.left = Math.max(6, r.left) + 'px'; tag.style.top = Math.max(6, r.top - 26) + 'px'; }
    if (el) {
      const ring = document.createElement('div');
      ring.style.cssText = 'position:fixed;z-index:2147483646;border:3px solid #7c3aed;border-radius:9px;pointer-events:none;'
        + 'left:' + (r.left - 5) + 'px;top:' + (r.top - 5) + 'px;width:' + (r.width + 10) + 'px;height:' + (r.height + 10) + 'px;'
        + 'animation:__fpPulse 1s ease-out 2;';
      document.documentElement.appendChild(ring);
      setTimeout(() => ring.remove(), 2100);
    }
  };
})();
"""

# Persistent сессия для watch-mode (одно окно на всё)
_pw = None
_browser = None
_page = None
_lock = asyncio.Lock()


async def _open_session():
    """Лениво открывает ОДИН видимый браузер (watch-mode) и переиспользует его."""
    global _pw, _browser, _page
    if _page is not None:
        return _page
    apw = await _get_playwright()
    _pw = await apw().start()
    _browser = await _pw.chromium.launch(headless=False, slow_mo=300)
    _page = await _browser.new_page(viewport={"width": 1180, "height": 760})
    return _page


async def close_session():
    """Закрыть видимое окно watch-mode (вызывается при простое/выключении)."""
    global _pw, _browser, _page
    try:
        if _browser is not None:
            await _browser.close()
    except Exception:
        pass
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _pw = _browser = _page = None


async def _acquire(url: str, wait: str = "networkidle", timeout: int = 30000):
    """Возвращает (page, closer). Watch-mode → общий видимый браузер (closer=noop)."""
    if _watch_on():
        try:
            async with _lock:
                page = await _open_session()
            await page.goto(url, wait_until=wait, timeout=timeout)
            try:
                await page.evaluate(_OVERLAY_JS)
            except Exception:
                pass

            async def _noop():
                return None
            return page, _noop
        except Exception:
            # видимое окно не удалось открыть (нет рабочего стола/сессии) —
            # БЕЗОПАСНЫЙ откат в headless, чтобы браузер всё равно работал
            await close_session()

    # обычный режим (или фолбэк) — свежий headless-браузер на вызов
    apw = await _get_playwright()
    pw = await apw().start()
    browser = await pw.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto(url, wait_until=wait, timeout=timeout)

    async def _close():
        try:
            await browser.close()
        finally:
            await pw.stop()
    return page, _close


async def _highlight(page, selector: str, label: str = ""):
    """В watch-mode двигает курсор к элементу и подсвечивает его перед действием."""
    try:
        await page.evaluate("([s,l]) => window.__fpHi && window.__fpHi(s,l)", [selector, label])
        await page.wait_for_timeout(550)
    except Exception:
        pass


async def browser_open(url: str, wait_for: str = "load", timeout: int = 30000) -> dict:
    """Открывает URL и возвращает содержимое страницы.
    Returns: {"ok": True, "url", "title", "text", "html"}"""
    try:
        page, closer = await _acquire(url, wait_for, timeout)
        try:
            title = await page.title()
            text = await page.inner_text("body")
            html = await page.content()
            return {"ok": True, "url": url, "title": title, "text": text[:5000], "html": html[:20000]}
        finally:
            await closer()
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_open: {e}"}


async def browser_screenshot(url: str, selector: Optional[str] = None) -> dict:
    """Скриншот страницы или элемента. Returns: {"ok", "base64", "size", "format"}"""
    try:
        page, closer = await _acquire(url, "networkidle", 30000)
        try:
            if selector:
                element = await page.query_selector(selector)
                if not element:
                    return {"ok": False, "error": f"Элемент не найден: {selector}"}
                shot = await element.screenshot()
            else:
                shot = await page.screenshot(full_page=True)
            return {"ok": True, "base64": base64.b64encode(shot).decode("ascii"),
                    "size": len(shot), "format": "PNG"}
        finally:
            await closer()
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_screenshot: {e}"}


async def browser_click(url: str, selector: str, wait_after: int = 1000) -> dict:
    """Открывает страницу и кликает по элементу. Returns: {"ok", "clicked", "url_after"}"""
    try:
        page, closer = await _acquire(url, "networkidle", 30000)
        try:
            await _highlight(page, selector, "клик")
            await page.click(selector)
            await page.wait_for_timeout(wait_after)
            return {"ok": True, "clicked": selector, "url_after": page.url}
        finally:
            await closer()
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_click: {e}"}


async def browser_fill(url: str, selector: str, value: str, submit: bool = False) -> dict:
    """Заполняет поле ввода. Returns: {"ok", "filled", "url_after"}"""
    try:
        page, closer = await _acquire(url, "networkidle", 30000)
        try:
            await _highlight(page, selector, "ввод")
            await page.fill(selector, value)
            if submit:
                await page.press(selector, "Enter")
                await page.wait_for_load_state("networkidle")
            return {"ok": True, "filled": selector, "url_after": page.url}
        finally:
            await closer()
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_fill: {e}"}


async def browser_extract(url: str, selector: str, attribute: Optional[str] = None) -> dict:
    """Извлекает текст/атрибут элементов по CSS-селектору. Returns: {"ok", "results", "count"}"""
    try:
        page, closer = await _acquire(url, "networkidle", 30000)
        try:
            await _highlight(page, selector, "читаю")
            elements = await page.query_selector_all(selector)
            results = []
            for el in elements:
                val = await (el.get_attribute(attribute) if attribute else el.inner_text())
                if val:
                    results.append(val.strip())
            return {"ok": True, "results": results, "count": len(results)}
        finally:
            await closer()
    except ImportError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"browser_extract: {e}"}


async def browser_eval(url: str, script: str) -> dict:
    """Выполняет JavaScript на странице. Returns: {"ok", "result"}"""
    try:
        page, closer = await _acquire(url, "networkidle", 30000)
        try:
            result = await page.evaluate(script)
            return {"ok": True, "result": result}
        finally:
            await closer()
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
