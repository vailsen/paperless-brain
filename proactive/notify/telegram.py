"""Telegram Bot API over httpx — the worker is its only caller.

Only one process may long-poll a bot token (`getUpdates`); a second one gets
HTTP 409 Conflict. That process is the worker: the app enqueues jobs and reads
the bot's username from `proactive.db` (written here after `getMe`), it never
talks to Telegram. No new dependency — the Bot API is a handful of JSON calls.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from proactive.notify import ACTION_LABELS, Notification

log = logging.getLogger("proactive.telegram")

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50
#: `callback_data` is limited to 64 bytes by Telegram.
CALLBACK_MAX = 64


class TelegramConflict(Exception):
    """409: another process is polling this bot token."""


def _token() -> str:
    from config.proactive import proactive_settings

    return proactive_settings().telegram_bot_token


async def call(method: str, payload: dict | None = None, *, timeout: float = 30) -> dict:
    url = f"{API}/bot{_token()}/{method}"
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.post(url, json=payload or {})
    if r.status_code == 409:
        raise TelegramConflict(r.text[:200])
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if r.status_code != 200 or not data.get("ok"):
        # Never log the URL: it carries the bot token.
        raise RuntimeError(f"Telegram {method} failed: {r.status_code} {data.get('description', '')}")
    return data.get("result")


def callback_data(finding_id: str, action: str) -> str:
    data = f"f:{finding_id}:{action}"
    if len(data.encode()) > CALLBACK_MAX:
        raise ValueError("callback_data too long")
    return data


def parse_callback(data: str) -> tuple[str, str] | None:
    parts = (data or "").split(":")
    if len(parts) != 3 or parts[0] != "f" or not parts[1] or parts[2] not in ACTION_LABELS:
        return None
    return parts[1], parts[2]


def keyboard(n: Notification) -> dict | None:
    if not n.finding_id or not n.actions:
        return None
    from i18n import translator_for

    _ = translator_for(n.lang)
    return {
        "inline_keyboard": [[
            {"text": _(ACTION_LABELS[a]), "callback_data": callback_data(n.finding_id, a)}
            for a in n.actions
        ]]
    }


async def send(chat_id: str, n: Notification, *, full_text: bool) -> None:
    payload = {
        "chat_id": chat_id,
        "text": (n.body_full if full_text and n.body_full else n.body_short)[:4096],
        "disable_web_page_preview": True,
    }
    kb = keyboard(n)
    if kb:
        payload["reply_markup"] = kb
    await call("sendMessage", payload)


async def send_text(chat_id: str, text: str) -> None:
    await call("sendMessage", {"chat_id": chat_id, "text": text[:4096]})


# ── Polling (worker only) ────────────────────────────────────────────────────


async def handle_update(upd: dict) -> None:
    """One update: `/start <code>`, `/stop`, or a button press."""
    from i18n import translator_for
    from proactive import linking, prefs

    if "callback_query" in upd:
        await _handle_callback(upd["callback_query"])
        return
    msg = upd.get("message") or {}
    chat = str((msg.get("chat") or {}).get("id", ""))
    text = (msg.get("text") or "").strip()
    if not chat or not text:
        return
    known = linking.user_for("telegram", chat)
    _ = translator_for(prefs.get(known).language if known else "en")
    if text.startswith("/start"):
        code = text[len("/start"):].strip()
        hit = linking.redeem(code, "telegram") if code else None
        if hit is None:
            await send_text(chat, _(
                "This code is invalid or has expired. Create a new one in "
                "PaperlessBrain under Settings > Proactive."
            ))
            return
        user, _addr = hit
        linking.link(user, "telegram", chat)
        _ = translator_for(prefs.get(user).language)
        log.info("telegram chat linked to %s", user)
        await send_text(chat, _(
            "Linked to PaperlessBrain as {user}. Send /stop to unlink."
        ).format(user=user))
    elif text.startswith("/stop"):
        user = linking.unlink_address("telegram", chat)
        if user:
            log.info("telegram chat of %s unlinked by /stop", user)
        await send_text(chat, _("Unlinked. This chat no longer receives messages."))


async def _handle_callback(cq: dict) -> None:
    from proactive import linking, reactions

    chat = str(((cq.get("message") or {}).get("chat") or {}).get("id", ""))
    parsed = parse_callback(cq.get("data", ""))
    answer = ""
    if parsed is None or not chat:
        log.warning("telegram: malformed callback ignored")
    else:
        finding_id, action = parsed
        chat_user = linking.user_for("telegram", chat)
        owner = reactions.owner_of(finding_id)
        if chat_user is None or owner is None or chat_user != owner:
            # Never act for a chat that is not the owner's linked one.
            log.warning(
                "telegram: callback for finding %s from chat not linked to its owner — ignored",
                finding_id,
            )
        else:
            answer = await reactions.react(owner, finding_id, action, channel="telegram")
    try:
        await call("answerCallbackQuery", {"callback_query_id": cq.get("id", ""), "text": answer[:200]})
    except Exception as exc:
        log.info("answerCallbackQuery failed: %s", exc)


async def poll_forever(stopping: asyncio.Event) -> None:
    from proactive.db import kv_get, kv_set

    try:
        me = await call("getMe")
        kv_set("telegram_bot_username", me.get("username", ""))
    except Exception as exc:
        log.error("telegram getMe failed: %s", exc)
    offset = int(kv_get("telegram_offset", "0") or 0)
    while not stopping.is_set():
        try:
            updates = await call(
                "getUpdates",
                {"offset": offset, "timeout": POLL_TIMEOUT_S, "allowed_updates": ["message", "callback_query"]},
                timeout=POLL_TIMEOUT_S + 15,
            )
        except TelegramConflict:
            log.error(
                "telegram 409 Conflict: another process polls TELEGRAM_BOT_TOKEN — "
                "only the worker may; retrying in 60 s"
            )
            await _sleep(stopping, 60)
            continue
        except Exception as exc:
            log.warning("telegram getUpdates failed: %s", exc)
            await _sleep(stopping, 15)
            continue
        for upd in updates or []:
            try:
                await handle_update(upd)
            except Exception:
                log.exception("telegram update %s failed", upd.get("update_id"))
            offset = int(upd["update_id"]) + 1
            # Persisted per update: a restart must not re-apply a button press.
            kv_set("telegram_offset", str(offset))


async def _sleep(stopping: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stopping.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
