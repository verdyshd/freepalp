"""
Notification Tools — Email (SMTP), Slack (Webhook), Notion (API).

Требует переменных .env:
  Email : EMAIL_FROM, EMAIL_PASSWORD, EMAIL_SMTP_HOST, EMAIL_SMTP_PORT
  Slack : SLACK_WEBHOOK_URL  (или SLACK_BOT_TOKEN + SLACK_CHANNEL)
  Notion: NOTION_API_KEY, NOTION_DATABASE_ID

Инструменты:
  email_send      — отправить email через SMTP
  slack_send      — отправить сообщение в Slack
  notion_create   — создать страницу в Notion базе данных
  notion_search   — поиск по Notion
"""

import os
import json
from typing import Optional


# ══════════════════════════════════════════════════════════════════════════════
# EMAIL
# ══════════════════════════════════════════════════════════════════════════════

async def email_send(
    to: str,
    subject: str,
    body: str,
    html: bool = False,
    cc: Optional[str] = None,
) -> dict:
    """
    Отправить email через SMTP.

    Переменные .env:
      EMAIL_FROM      — адрес отправителя
      EMAIL_PASSWORD  — пароль (для Gmail: App Password)
      EMAIL_SMTP_HOST — SMTP сервер (по умолчанию smtp.gmail.com)
      EMAIL_SMTP_PORT — порт (по умолчанию 587)

    Args:
        to:      адрес получателя (или "a@b.com, c@d.com")
        subject: тема письма
        body:    текст письма
        html:    True если body это HTML
        cc:      копия (необязательно)

    Returns:
        {"ok": True, "to": str, "subject": str}
    """
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from_addr = os.getenv("EMAIL_FROM", "")
    password  = os.getenv("EMAIL_PASSWORD", "")
    smtp_host = os.getenv("EMAIL_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("EMAIL_SMTP_PORT", "587"))

    if not from_addr:
        return {"ok": False, "error": "EMAIL_FROM не настроен в .env"}
    if not password:
        return {"ok": False, "error": "EMAIL_PASSWORD не настроен в .env"}

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = from_addr
        msg["To"]      = to
        if cc:
            msg["Cc"] = cc

        mime_type = "html" if html else "plain"
        msg.attach(MIMEText(body, mime_type, "utf-8"))

        recipients = [addr.strip() for addr in to.split(",")]
        if cc:
            recipients += [addr.strip() for addr in cc.split(",")]

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(from_addr, password)
            server.sendmail(from_addr, recipients, msg.as_string())

        return {"ok": True, "to": to, "subject": subject}

    except smtplib.SMTPAuthenticationError:
        return {"ok": False, "error": "Ошибка аутентификации SMTP. Проверьте EMAIL_FROM и EMAIL_PASSWORD."}
    except Exception as e:
        return {"ok": False, "error": f"email_send: {e}"}


# ══════════════════════════════════════════════════════════════════════════════
# SLACK
# ══════════════════════════════════════════════════════════════════════════════

async def slack_send(
    message: str,
    channel: Optional[str] = None,
    username: str = "FreePalp",
    icon_emoji: str = ":octopus:",
) -> dict:
    """
    Отправить сообщение в Slack.

    Использует Webhook (проще) или Bot Token + Channel.

    Переменные .env:
      SLACK_WEBHOOK_URL — Incoming Webhook URL (приоритет)
        или
      SLACK_BOT_TOKEN   — Bot Token (xoxb-...)
      SLACK_CHANNEL     — канал по умолчанию (#general)

    Args:
        message:    текст сообщения (поддерживает Slack Markdown)
        channel:    канал (#general) — переопределяет SLACK_CHANNEL
        username:   имя бота
        icon_emoji: эмодзи бота

    Returns:
        {"ok": True, "channel": str}
    """
    import urllib.request

    webhook_url = os.getenv("SLACK_WEBHOOK_URL", "")
    bot_token   = os.getenv("SLACK_BOT_TOKEN", "")
    default_ch  = os.getenv("SLACK_CHANNEL", "#general")
    target_ch   = channel or default_ch

    if not webhook_url and not bot_token:
        return {
            "ok":    False,
            "error": "Не настроен Slack. Добавьте в .env:\n"
                     "  SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...\n"
                     "  (или SLACK_BOT_TOKEN + SLACK_CHANNEL)"
        }

    try:
        if webhook_url:
            # Incoming Webhook
            payload = json.dumps({
                "text":       message,
                "username":   username,
                "icon_emoji": icon_emoji,
            }).encode("utf-8")
            req = urllib.request.Request(
                webhook_url, data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp_text = resp.read().decode("utf-8")
                if resp_text != "ok":
                    return {"ok": False, "error": f"Slack webhook error: {resp_text}"}
            return {"ok": True, "channel": "webhook", "method": "webhook"}

        else:
            # Bot Token API
            payload = json.dumps({
                "channel":  target_ch,
                "text":     message,
                "username": username,
            }).encode("utf-8")
            req = urllib.request.Request(
                "https://slack.com/api/chat.postMessage",
                data=payload,
                headers={
                    "Content-Type":  "application/json",
                    "Authorization": f"Bearer {bot_token}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if not data.get("ok"):
                    return {"ok": False, "error": data.get("error", "unknown")}
            return {"ok": True, "channel": target_ch, "method": "bot_token"}

    except Exception as e:
        return {"ok": False, "error": f"slack_send: {e}"}


# ══════════════════════════════════════════════════════════════════════════════
# NOTION
# ══════════════════════════════════════════════════════════════════════════════

def _notion_headers() -> dict:
    api_key = os.getenv("NOTION_API_KEY", "")
    return {
        "Authorization":  f"Bearer {api_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type":   "application/json",
    }


async def _notion_request(method: str, endpoint: str, data: Optional[dict] = None) -> dict:
    import urllib.request
    url = f"https://api.notion.com/v1{endpoint}"
    body = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(
        url, data=body, method=method,
        headers=_notion_headers(),
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"object": "error", "message": str(e)}


async def notion_create(
    title: str,
    content: str,
    database_id: Optional[str] = None,
    properties: Optional[dict] = None,
) -> dict:
    """
    Создать страницу в Notion базе данных.

    Переменные .env:
      NOTION_API_KEY      — Integration Token (secret_...)
      NOTION_DATABASE_ID  — ID базы данных (из URL страницы)

    Args:
        title:       заголовок страницы
        content:     текст содержимого (будет добавлен как paragraph блоки)
        database_id: ID базы данных (переопределяет NOTION_DATABASE_ID)
        properties:  дополнительные свойства страницы (dict)

    Returns:
        {"ok": True, "id": str, "url": str}
    """
    api_key = os.getenv("NOTION_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "NOTION_API_KEY не настроен в .env"}

    db_id = database_id or os.getenv("NOTION_DATABASE_ID", "")
    if not db_id:
        return {"ok": False, "error": "NOTION_DATABASE_ID не настроен в .env"}

    # Разбиваем content на абзацы (Notion лимит: 2000 символов на блок)
    paragraphs = [content[i:i+1900] for i in range(0, len(content), 1900)]
    children = [
        {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": para}}]
            }
        }
        for para in paragraphs[:100]  # max 100 блоков за раз
    ]

    page_props = {
        "title": {
            "title": [{"type": "text", "text": {"content": title}}]
        }
    }
    if properties:
        page_props.update(properties)

    payload = {
        "parent":     {"database_id": db_id},
        "properties": page_props,
        "children":   children,
    }

    data = await _notion_request("POST", "/pages", payload)

    if data.get("object") == "error":
        return {"ok": False, "error": data.get("message", "Notion API error")}

    return {
        "ok":    True,
        "id":    data.get("id"),
        "url":   data.get("url"),
        "title": title,
    }


async def notion_search(query: str, limit: int = 10) -> dict:
    """
    Поиск по всем страницам и базам данных Notion.

    Args:
        query: поисковый запрос
        limit: максимум результатов

    Returns:
        {"ok": True, "results": [...], "total": int}
    """
    api_key = os.getenv("NOTION_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "NOTION_API_KEY не настроен в .env"}

    payload = {
        "query":      query,
        "page_size":  min(limit, 100),
    }
    data = await _notion_request("POST", "/search", payload)

    if data.get("object") == "error":
        return {"ok": False, "error": data.get("message", "Notion API error")}

    results = []
    for item in data.get("results", []):
        obj_type = item.get("object")
        # Извлекаем заголовок
        title = ""
        if obj_type == "page":
            props = item.get("properties", {})
            for key in ("Name", "Title", "title"):
                title_prop = props.get(key, {})
                if title_prop.get("type") == "title":
                    title_arr = title_prop.get("title", [])
                    if title_arr:
                        title = title_arr[0].get("plain_text", "")
                        break
        elif obj_type == "database":
            title_arr = item.get("title", [])
            if title_arr:
                title = title_arr[0].get("plain_text", "")

        results.append({
            "id":    item.get("id"),
            "type":  obj_type,
            "title": title or "(без названия)",
            "url":   item.get("url"),
        })

    return {"ok": True, "results": results, "total": len(results)}


async def notion_get_page(page_id: str) -> dict:
    """
    Получить содержимое страницы Notion.

    Args:
        page_id: ID страницы Notion

    Returns:
        {"ok": True, "title": str, "content": str, "url": str}
    """
    api_key = os.getenv("NOTION_API_KEY", "")
    if not api_key:
        return {"ok": False, "error": "NOTION_API_KEY не настроен в .env"}

    # Получаем блоки страницы
    data = await _notion_request("GET", f"/blocks/{page_id}/children")
    if data.get("object") == "error":
        return {"ok": False, "error": data.get("message", "Notion API error")}

    texts = []
    for block in data.get("results", []):
        block_type = block.get("type", "")
        block_content = block.get(block_type, {})
        rich_text = block_content.get("rich_text", [])
        for rt in rich_text:
            plain = rt.get("plain_text", "")
            if plain:
                texts.append(plain)

    return {
        "ok":      True,
        "page_id": page_id,
        "content": "\n".join(texts),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Реестр инструментов
# ──────────────────────────────────────────────────────────────────────────────

NOTIFICATION_TOOLS: dict = {
    "email_send": {
        "description": "Отправить email через SMTP (Gmail, Yandex и др.)",
        "fn":          email_send,
        "async":       True,
        "args":        {"to": "str", "subject": "str", "body": "str", "html": "bool", "cc": "str"},
    },
    "slack_send": {
        "description": "Отправить сообщение в Slack (Webhook или Bot Token)",
        "fn":          slack_send,
        "async":       True,
        "args":        {"message": "str", "channel": "str (#channel)", "username": "str"},
    },
    "notion_create": {
        "description": "Создать страницу в Notion базе данных",
        "fn":          notion_create,
        "async":       True,
        "args":        {"title": "str", "content": "str", "database_id": "str (необязательно)"},
    },
    "notion_search": {
        "description": "Поиск по всем страницам Notion",
        "fn":          notion_search,
        "async":       True,
        "args":        {"query": "str", "limit": "int"},
    },
    "notion_get_page": {
        "description": "Получить содержимое страницы Notion по ID",
        "fn":          notion_get_page,
        "async":       True,
        "args":        {"page_id": "str"},
    },
}
