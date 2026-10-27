"""Proactive Phase 3: notifier, email, channel linking (push: test_proactive_push)."""

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

    for k, v in {
        "SMTP_HOST": "smtp.example", "SMTP_FROM": "PaperlessBrain <brain@example.com>",
        "IMAP_HOST": "imap.example", "IMAP_USER": "brain", "SMTP_SECURITY": "none",
        "PROACTIVE_APP_BASE_URL": "https://brain.lan",
    }.items():
        monkeypatch.setenv(k, v)
    cfgmod.proactive_settings.cache_clear()
    return proactive_on


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

    code = linking.create_code("alice", "email", "a@example.com")
    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{8}", code)
    with connect() as conn:
        stored = [r[0] for r in conn.execute("SELECT code_hash FROM link_code")]
    assert code not in stored  # hashed at rest
    assert linking.redeem(code.lower(), "email") == ("alice", "a@example.com")
    assert linking.redeem(code, "email") is None  # once

    late = linking.create_code("alice", "email", "a@example.com")
    clock.advance(timedelta(minutes=10, seconds=1))
    assert linking.redeem(late, "email") is None


def test_new_code_kills_the_old_one_and_email_code_is_bound_to_its_user(proactive_on):
    from proactive import linking

    old = linking.create_code("alice", "email", "a@example.com")
    mail = linking.create_code("alice", "email", "Alice@Example.com")
    assert linking.redeem(old, "email") is None
    assert linking.redeem(mail, "email", user="bob") is None
    assert linking.redeem(mail, "email", user="alice") == ("alice", "alice@example.com")


# ── Delivery ─────────────────────────────────────────────────────────────────


def _choose(user, channels, full_text=()):
    from proactive import linking, prefs

    p = prefs.get(user)
    p.optin = True
    p.channels = channels
    p.language = "en"
    prefs.save(p)
    linking.link(user, "email", "alice@example.com")
    for ch in full_text:
        linking.set_full_text(user, ch, True)


def test_test_message_arrives_short_by_default(channels_on, smtp):
    from proactive import jobs

    _choose("alice", "email")
    jobs.enqueue("send_test", "alice")
    _drain()
    mails = [m for m in smtp if m[0] == "send"]
    assert len(mails) == 1
    msg = mails[0][1]
    assert msg["To"] == "alice@example.com"
    assert msg.is_multipart()
    text = msg.get_body(("plain",)).get_content()
    assert "test message" in text and "Urgency: Low" in text and "https://brain.lan/findings" in text
    assert "If you can read this" not in text  # full text needs the opt-in


def test_full_text_only_with_opt_in(channels_on, smtp):
    from proactive import jobs

    _choose("alice", "email", full_text=("email",))
    jobs.enqueue("send_test", "alice")
    _drain()
    (msg,) = [m[1] for m in smtp if m[0] == "send"]
    assert "If you can read this" in msg.get_body(("plain",)).get_content()


def test_finding_mail_has_message_id_and_action_links(channels_on, smtp):
    from proactive import notify
    from proactive.notify import INSIGHT_ACTIONS, compose
    from proactive.worker import Worker

    _choose("alice", "email")
    n = compose("alice", "de", finding_id="f7k3q9x", title="Kfz-Versicherung Wechselfrist",
                urgency="high", actions=INSIGHT_ACTIONS)
    assert notify.enqueue(n) == ["email"]
    asyncio.run(Worker().tick_once())
    (msg,) = [m[1] for m in smtp if m[0] == "send"]
    assert re.fullmatch(r"<f7k3q9x\.[0-9a-f]{12}@example\.com>", msg["Message-ID"])
    assert "https://brain.lan/findings/f7k3q9x?action=never" in msg.get_body(("plain",)).get_content()


def test_email_not_configured_is_never_offered_or_attempted(proactive_on, smtp):
    from proactive import jobs, notify

    _choose("alice", "email")
    assert notify.chosen_channels("alice") == []
    jobs.enqueue("send_test", "alice")
    _drain()
    assert smtp == []  # no connection attempted


def test_stored_telegram_choice_reads_as_the_remaining_channels(proactive_on):
    """Telegram was removed; a user who had chosen it must still save cleanly."""
    from proactive import prefs
    from proactive.db import connect

    with connect() as conn:
        conn.execute("INSERT INTO proactive_prefs (user, channels, updated_at) VALUES ('alice', 'push,telegram', 'x')")
    p = prefs.get("alice")
    assert p.channel_list() == ["push"] and p.channels == "push"
    prefs.save(p)
    assert prefs.ProactivePrefs("a", channels="both").channel_list() == ["email"]


def test_email_verification_code_is_mailed_and_scrubbed(channels_on, smtp):
    from proactive import jobs, linking

    code = linking.create_code("alice", "email", "alice@example.com")
    jid = jobs.enqueue("email_verify", "alice", {"address": "alice@example.com", "code": code})
    _drain()
    (msg,) = [m[1] for m in smtp if m[0] == "send"]
    assert code in msg.get_content()
    assert jobs.get(jid).payload == {}


def test_worker_logging_never_prints_request_urls(caplog, monkeypatch):
    """httpx logs request URLs at INFO; a URL can carry a secret."""
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
            await c.post("https://fcm.googleapis.com/fcm/send/SECRET-TOKEN")

    caplog.set_level(logging.INFO)  # root at INFO, as in the worker
    asyncio.run(go())
    assert "SECRET-TOKEN" not in caplog.text


@pytest.mark.parametrize("start,end,now,wait_min", [
    ("22:00", "07:00", "23:30", 7 * 60 + 30),   # across midnight, evening
    ("22:00", "07:00", "02:00", 5 * 60),        # across midnight, night
    ("22:00", "07:00", "07:00", 0),             # ends exactly then
    ("22:00", "07:00", "12:00", 0),
    ("13:00", "15:00", "14:00", 60),            # same-day window
    ("07:00", "07:00", "03:00", 0),             # start == end: none
])
def test_quiet_hours_wait(start, end, now, wait_min):
    from datetime import datetime, timezone

    from proactive.notify import quiet_wait

    h, m = map(int, now.split(":"))
    at = datetime(2026, 10, 27, h, m, tzinfo=timezone.utc)
    assert quiet_wait(start, end, at) == timedelta(minutes=wait_min)


def test_message_in_quiet_hours_waits_but_the_test_message_does_not(channels_on, monkeypatch):
    from proactive import jobs, notify
    from proactive.db import connect

    _choose("alice", "email")
    monkeypatch.setattr(notify, "quiet_delay", lambda user: timedelta(hours=5))
    notify.enqueue(notify.compose("alice", "en", finding_id="f1", title="Insight", urgency="low"))
    notify.enqueue(notify.compose("alice", "en", finding_id="", title="Test", urgency="low"), quiet=False)
    with connect() as conn:
        rows = {('"title": "Insight"' in r["payload_json"]): r["run_after"] > r["created_at"]
                for r in conn.execute("SELECT * FROM jobs WHERE kind = 'notify_channel'")}
    assert rows == {True: True, False: False}
