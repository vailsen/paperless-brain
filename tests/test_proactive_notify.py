"""Proactive Phase 3: notifier, Telegram, email, channel linking."""

import asyncio
import logging
import re
from datetime import timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def channels_on(proactive_on, monkeypatch):
    from config import proactive as cfgmod

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    for k, v in {
        "SMTP_HOST": "smtp.example", "SMTP_FROM": "PaperlessBrain <brain@example.com>",
        "IMAP_HOST": "imap.example", "IMAP_USER": "brain", "SMTP_SECURITY": "none",
        "PROACTIVE_APP_BASE_URL": "https://brain.lan",
    }.items():
        monkeypatch.setenv(k, v)
    cfgmod.proactive_settings.cache_clear()
    return proactive_on


@pytest.fixture
def tg(monkeypatch):
    from proactive.notify import telegram

    calls = []

    async def fake_call(method, payload=None, *, timeout=30):
        calls.append((method, payload or {}))
        if method == "getMe":
            return {"username": "pb_bot"}
        return {}

    monkeypatch.setattr(telegram, "call", fake_call)
    return calls


@pytest.fixture
def smtp(monkeypatch):
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=30, **kw):
            sent.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context=None):
            sent.append(("starttls",))

        def login(self, u, p):
            sent.append(("login", u))

        def send_message(self, msg):
            sent.append(("send", msg))

    import smtplib

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return sent


def _drain(tick_times=3):
    from proactive.worker import Worker
    import proactive.tasks  # noqa: F401

    w = Worker()
    for _ in range(tick_times):
        asyncio.run(w.tick_once())


# ── Link codes ───────────────────────────────────────────────────────────────


def test_link_code_works_once_and_expires(proactive_on):
    from proactive import clock, linking
    from proactive.db import connect

    code = linking.create_code("alice", "telegram")
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", code)
    with connect() as conn:
        stored = [r[0] for r in conn.execute("SELECT code_hash FROM link_code")]
    assert code not in stored  # hashed at rest
    assert linking.redeem(code.lower(), "telegram") == ("alice", "")
    assert linking.redeem(code, "telegram") is None  # once

    late = linking.create_code("alice", "telegram")
    clock.advance(timedelta(minutes=10, seconds=1))
    assert linking.redeem(late, "telegram") is None


def test_new_code_kills_the_old_one_and_email_code_is_bound_to_its_user(proactive_on):
    from proactive import linking

    old = linking.create_code("alice", "telegram")
    new = linking.create_code("alice", "telegram")
    assert linking.redeem(old, "telegram") is None
    assert linking.redeem(new, "telegram") == ("alice", "")
    mail = linking.create_code("alice", "email", "Alice@Example.com")
    assert linking.redeem(mail, "email", user="bob") is None
    assert linking.redeem(mail, "email", user="alice") == ("alice", "alice@example.com")


# ── Telegram ─────────────────────────────────────────────────────────────────


def test_email_codes_three_in_the_first_hour_then_hourly_reset_after_a_day(proactive_on):
    from proactive import clock, linking

    def send():
        assert linking.email_code_wait("alice") is None
        linking.create_code("alice", "email", "a@example.com")

    for _ in range(3):                                     # burst for typos
        send()
        clock.advance(timedelta(minutes=5))
    wait = linking.email_code_wait("alice")
    assert wait is not None and timedelta(minutes=50) < wait <= timedelta(hours=1)
    assert linking.email_code_wait("bob") is None          # per user
    linking.create_code("alice", "telegram")               # Telegram codes are not limited

    clock.advance(timedelta(minutes=55))                   # an hour after the third
    send()
    clock.advance(timedelta(minutes=30))
    assert linking.email_code_wait("alice") is not None    # now one an hour
    clock.advance(timedelta(minutes=30))
    send()

    clock.advance(timedelta(hours=22, minutes=30))         # 24 h after the first code
    for _ in range(3):                                     # the burst is back
        send()
    assert linking.email_code_wait("alice") is not None


def _msg(chat, text):
    return {"update_id": 1, "message": {"chat": {"id": chat}, "text": text}}


def test_start_links_and_stop_unlinks(channels_on, tg):
    from proactive import linking
    from proactive.notify import telegram

    code = linking.create_code("alice", "telegram")
    asyncio.run(telegram.handle_update(_msg(4242, f"/start {code}")))
    assert linking.address("alice", "telegram") == "4242"
    assert "alice" in tg[-1][1]["text"]
    asyncio.run(telegram.handle_update(_msg(4242, f"/start {code}")))  # used up
    assert "invalid" in tg[-1][1]["text"]
    asyncio.run(telegram.handle_update(_msg(4242, "/stop")))
    assert linking.address("alice", "telegram") is None


def test_callback_from_a_chat_not_linked_to_the_owner_is_ignored(channels_on, tg, monkeypatch, caplog):
    from proactive import linking, reactions
    from proactive.notify import telegram

    linking.link("alice", "telegram", "1")
    linking.link("bob", "telegram", "2")
    monkeypatch.setattr(reactions, "owner_of", lambda fid: "alice" if fid == "abc1234" else None)
    reacted = []

    async def react(user, fid, action, *, channel):
        reacted.append((user, fid, action, channel))
        return "ok"

    monkeypatch.setattr(reactions, "react", react)
    cq = lambda chat, data: {"update_id": 9, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": chat}}}}  # noqa: E731
    with caplog.at_level(logging.WARNING, logger="proactive.telegram"):
        asyncio.run(telegram.handle_update(cq(2, "f:abc1234:interested")))   # bob's chat
        asyncio.run(telegram.handle_update(cq(99, "f:abc1234:interested")))  # unknown chat
        asyncio.run(telegram.handle_update(cq(1, "f:abc1234:rm -rf")))        # malformed
    assert reacted == []
    assert sum("ignored" in r.message for r in caplog.records) == 3
    asyncio.run(telegram.handle_update(cq(1, "f:abc1234:interested")))
    assert reacted == [("alice", "abc1234", "interested", "telegram")]


def test_callback_data_fits_telegram_limit():
    from proactive.notify import telegram

    assert len(telegram.callback_data("f7k3q9x", "interested")) <= 64
    assert telegram.parse_callback("f:f7k3q9x:later") == ("f7k3q9x", "later")
    assert telegram.parse_callback("f:x:explode") is None


def test_poller_survives_409_and_persists_offset(channels_on, monkeypatch, caplog):
    from proactive.db import kv_get
    from proactive.notify import telegram

    stopping = asyncio.Event()
    seq = iter(["409", [{"update_id": 41, "message": {"chat": {"id": 5}, "text": "hi"}}], "stop"])

    async def fake_call(method, payload=None, *, timeout=30):
        if method == "getMe":
            return {"username": "pb_bot"}
        if method == "sendMessage":
            return {}
        nxt = next(seq)
        if nxt == "409":
            raise telegram.TelegramConflict("conflict")
        if nxt == "stop":
            stopping.set()
            return []
        return nxt

    async def no_sleep(ev, s):
        pass

    monkeypatch.setattr(telegram, "call", fake_call)
    monkeypatch.setattr(telegram, "_sleep", no_sleep)
    with caplog.at_level(logging.ERROR, logger="proactive.telegram"):
        asyncio.run(telegram.poll_forever(stopping))
    assert any("409" in r.message for r in caplog.records)
    assert kv_get("telegram_offset") == "42"
    assert kv_get("telegram_bot_username") == "pb_bot"


def test_the_app_never_talks_to_telegram():
    """Only the worker polls the token; the app enqueues jobs."""
    offenders = []
    for path in list((REPO / "app_ui").rglob("*.py")) + [REPO / "main.py", REPO / "proactive" / "app.py"]:
        src = path.read_text()
        if "notify.telegram" in src or "notify import telegram" in src or "api.telegram.org" in src:
            offenders.append(str(path))
    assert offenders == []


# ── Delivery ─────────────────────────────────────────────────────────────────


def _choose(user, channels, full_text=()):
    from proactive import linking, prefs

    p = prefs.get(user)
    p.optin = True
    p.channels = channels
    p.language = "en"
    prefs.save(p)
    linking.link(user, "telegram", "777")
    linking.link(user, "email", "alice@example.com")
    for ch in full_text:
        linking.set_full_text(user, ch, True)


def test_test_message_arrives_on_both_channels_short_by_default(channels_on, tg, smtp):
    from proactive import jobs

    _choose("alice", "both")
    jobs.enqueue("send_test", "alice")
    _drain()
    sends = [p for m, p in tg if m == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == "777"
    text = sends[0]["text"]
    assert "test message" in text and "Urgency: Low" in text and "https://brain.lan/findings" in text
    assert "If you can read this" not in text  # full text needs the opt-in
    mails = [m for m in smtp if m[0] == "send"]
    assert len(mails) == 1
    msg = mails[0][1]
    assert msg["To"] == "alice@example.com"
    assert msg.is_multipart()
    assert "If you can read this" not in msg.get_body(("plain",)).get_content()


def test_full_text_only_with_opt_in(channels_on, tg, smtp):
    from proactive import jobs

    _choose("alice", "telegram", full_text=("telegram",))
    jobs.enqueue("send_test", "alice")
    _drain()
    (text,) = [p["text"] for m, p in tg if m == "sendMessage"]
    assert "If you can read this" in text
    assert not [m for m in smtp if m[0] == "send"]


def test_finding_message_has_buttons_message_id_and_action_links(channels_on, tg, smtp):
    from proactive import notify
    from proactive.notify import INSIGHT_ACTIONS, compose
    from proactive.worker import Worker

    _choose("alice", "both")
    n = compose("alice", "de", finding_id="f7k3q9x", title="Kfz-Versicherung Wechselfrist",
                urgency="high", actions=INSIGHT_ACTIONS)
    assert notify.enqueue(n) == ["telegram", "email"]
    asyncio.run(Worker().tick_once())
    (payload,) = [p for m, p in tg if m == "sendMessage"]
    buttons = payload["reply_markup"]["inline_keyboard"][0]
    assert [b["callback_data"] for b in buttons] == ["f:f7k3q9x:interested", "f:f7k3q9x:later", "f:f7k3q9x:never"]
    assert buttons[0]["text"] == "Interessant"  # recipient's language
    (msg,) = [m[1] for m in smtp if m[0] == "send"]
    assert re.fullmatch(r"<f7k3q9x\.[0-9a-f]{12}@example\.com>", msg["Message-ID"])
    assert "https://brain.lan/findings/f7k3q9x?action=never" in msg.get_body(("plain",)).get_content()


def test_email_not_configured_is_never_offered_or_attempted(proactive_on, tg, smtp, monkeypatch):
    from config import proactive as cfgmod
    from proactive import jobs, notify

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:secret")
    cfgmod.proactive_settings.cache_clear()
    _choose("alice", "both")
    assert notify.chosen_channels("alice") == ["telegram"]
    jobs.enqueue("send_test", "alice")
    _drain()
    assert smtp == []  # no connection attempted


def test_email_verification_code_is_mailed_and_scrubbed(channels_on, smtp):
    from proactive import jobs, linking

    code = linking.create_code("alice", "email", "alice@example.com")
    jid = jobs.enqueue("email_verify", "alice", {"address": "alice@example.com", "code": code})
    _drain()
    (msg,) = [m[1] for m in smtp if m[0] == "send"]
    assert code in msg.get_content()
    assert jobs.get(jid).payload == {}


def test_worker_logging_never_prints_the_bot_token(caplog, monkeypatch):
    """httpx logs request URLs at INFO; the Bot API has the token in the URL."""
    import logging

    import httpx

    from proactive.worker import _setup_logging

    for name in ("httpx", "httpcore"):
        monkeypatch.setattr(logging.getLogger(name), "level", logging.NOTSET)
    _setup_logging()
    assert logging.getLogger("httpx").level == logging.WARNING  # set explicitly, not inherited

    async def go():
        transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"ok": True, "result": {}}))
        async with httpx.AsyncClient(transport=transport) as c:
            await c.post("https://api.telegram.org/bot123:SECRET-TOKEN/getMe")

    caplog.set_level(logging.INFO)  # root at INFO, as in the worker
    asyncio.run(go())
    assert "SECRET-TOKEN" not in caplog.text
