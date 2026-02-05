from __future__ import annotations

import base64
import datetime
import json
import logging
from typing import Any, Dict, Iterable, List

from aiohttp import web
from aiogram import Bot
from sqlalchemy import select

from .config import settings
from .db import get_session
from .models import User
from .ringostat_ingest import upsert_calls, upsert_answer_call

logger = logging.getLogger(__name__)
_runner: web.AppRunner | None = None
_notify_bot: Bot | None = None


def _auth_ok(request: web.Request) -> bool:
    user = settings.ringostat_webhook_user
    password = settings.ringostat_webhook_password
    if not user and not password:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        sent_user, sent_pass = decoded.split(":", 1)
    except Exception:
        return False
    return sent_user == (user or "") and sent_pass == (password or "")


def _extract_rows(payload: Any) -> List[Dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        data = payload.get("data") or payload.get("items") or payload.get("call") or payload
    else:
        data = payload
    if isinstance(data, list):
        rows = data
    else:
        rows = [data]
    result: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict) and "call" in row and isinstance(row["call"], dict):
            result.append(row["call"])
        elif isinstance(row, dict):
            result.append(row)
    return result


def _summarize_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        keys = list(payload.keys())
        return f"dict keys={keys[:30]} total_keys={len(keys)}"
    if isinstance(payload, list):
        return f"list len={len(payload)} first_type={type(payload[0]).__name__ if payload else 'empty'}"
    return f"type={type(payload).__name__}"


def _summarize_rows(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "rows=0"
    sample = rows[0]
    keys = list(sample.keys())
    return f"rows={len(rows)} sample_keys={keys[:40]} sample_key_count={len(keys)}"


def _get_notify_bot() -> Bot | None:
    global _notify_bot
    if _notify_bot:
        return _notify_bot
    if not settings.telegram_bot_token:
        return None
    _notify_bot = Bot(token=settings.telegram_bot_token)
    return _notify_bot


async def _notify_manager(payload: Dict[str, Any]) -> None:
    manager = payload.get("manager") or payload.get("employee") or payload.get("responsible_name")
    if not manager:
        return
    bot = _get_notify_bot()
    if not bot:
        return
    with get_session() as session:
        user = session.scalar(
            select(User).where(
                User.representative_name == str(manager),
                User.telegram_chat_id.is_not(None),
                User.is_admin.is_(False),
            )
        )
    if not user:
        return
    sales_source = payload.get("sales_source") or payload.get("sales_cource")
    fields = [
        ("Кампания", payload.get("market_comp")),
        ("Объявление", payload.get("target_post")),
        ("Канал", payload.get("sales_channel")),
        ("Источник", sales_source),
        ("Лендинг", payload.get("landing") or payload.get("last_page")),
        ("Реферер", payload.get("referrer")),
        ("Тип звонка", payload.get("call_type")),
        ("Номер", payload.get("caller_id")),
        ("User Agent", payload.get("user_agent")),
    ]
    details = [f"{label}: {value}" for label, value in fields if value]
    if not details:
        return
    header = "📣 Маркетингові дані дзвінка"
    msg = "\n".join([header, *details])
    try:
        await bot.send_message(chat_id=user.telegram_chat_id, text=msg)
    except Exception:
        logger.exception("Failed to send Ringostat marketing info to %s", manager)


async def handle_after_call(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    logger.info(
        "Ringostat webhook request method=%s path=%s ip=%s content_type=%s content_length=%s",
        request.method,
        request.path,
        request.remote,
        request.content_type,
        request.content_length,
    )
    try:
        payload = await request.json()
    except Exception:
        text = await request.text()
        logger.warning(
            "Ringostat webhook invalid JSON content_type=%s len=%s body=%s",
            request.content_type,
            len(text),
            text[:1000],
        )
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    try:
        payload_preview = json.dumps(payload, ensure_ascii=True)[:2000]
    except Exception:
        payload_preview = str(payload)[:2000]
    logger.info("Ringostat webhook payload %s preview=%s", _summarize_payload(payload), payload_preview)
    rows = _extract_rows(payload)
    received_at = datetime.datetime.utcnow()
    created_ids, created, updated = upsert_calls(rows, received_at=received_at)
    logger.info("Ringostat webhook rows %s", _summarize_rows(rows))
    logger.info(
        "Ringostat webhook after_call received=%s created=%s updated=%s",
        len(rows),
        created,
        updated,
    )
    return web.json_response(
        {"ok": True, "received": len(rows), "created": created, "updated": updated}
    )


async def handle_answer_call(request: web.Request) -> web.Response:
    if not _auth_ok(request):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    logger.info(
        "Ringostat answer webhook request method=%s path=%s ip=%s content_type=%s content_length=%s",
        request.method,
        request.path,
        request.remote,
        request.content_type,
        request.content_length,
    )
    try:
        payload = await request.json()
    except Exception:
        text = await request.text()
        logger.warning(
            "Ringostat answer webhook invalid JSON content_type=%s len=%s body=%s",
            request.content_type,
            len(text),
            text[:1000],
        )
        return web.json_response({"ok": False, "error": "invalid_json"}, status=400)
    if not isinstance(payload, dict):
        return web.json_response({"ok": False, "error": "invalid_payload"}, status=400)
    try:
        payload_preview = json.dumps(payload, ensure_ascii=True)[:2000]
    except Exception:
        payload_preview = str(payload)[:2000]
    logger.info(
        "Ringostat answer webhook payload %s preview=%s",
        _summarize_payload(payload),
        payload_preview,
    )
    received_at = datetime.datetime.utcnow()
    stored = False
    try:
        stored = upsert_answer_call(payload, received_at=received_at)
    except Exception:
        logger.exception("Ringostat answer webhook store failed")
    # Manager notifications disabled per request.
    return web.json_response({"ok": True, "stored": stored})


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


async def start_webhook_server() -> None:
    global _runner
    if not settings.ringostat_webhook_enabled:
        logger.info("Ringostat webhook server disabled via env")
        return
    if _runner:
        return
    app = web.Application()
    app.router.add_post("/webhooks/ringostat/after_call", handle_after_call)
    app.router.add_post("/webhooks/ringostat/answer_call", handle_answer_call)
    app.router.add_get("/webhooks/ringostat/health", handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(
        runner, host="0.0.0.0", port=settings.ringostat_webhook_port
    )
    await site.start()
    _runner = runner
    logger.info(
        "Ringostat webhook server started on port %s",
        settings.ringostat_webhook_port,
    )
