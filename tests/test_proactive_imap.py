"""Proactive Phase 5: email replies over IMAP."""

import asyncio
import logging
from email.message import EmailMessage

import pytest

from proactive.imap_replies import finding_ids_in, parse_keyword

DOMAIN = "example.com"


class FakeIMAP:
    """Just enough of imaplib.IMAP4 for the reply poller."""

    def __init__(self, folders, caps=("IMAP4REV1", "MOVE")):
        self.folders = folders  # wire name → {uid: [raw, flags]}
        self.capabilities = caps
        self.selected = None
        self.log = []

    def select(self, name, readonly=False):
        self.log.append(("select", name))
        if name not in self.folders:
            return "NO", [b"no such folder"]
        self.selected = name
        return "OK", [str(len(self.folders[name])).encode()]

    def create(self, name):
        self.log.append(("create", name))
        self.folders.setdefault(name, {})
        return "OK", [b"created"]

    def uid(self, cmd, *args):
        box = self.folders[self.selected]
        cmd = cmd.upper()
        if cmd == "SEARCH":
            ids = [u for u, (raw, flags) in box.items() if "\\Seen" not in flags]
            return "OK", [b" ".join(ids)]
        if cmd == "FETCH":
            raw = box[args[0]][0]
            return "OK", [(b"1 (UID 1 BODY[] {%d}" % len(raw), raw), b")"]
        uids = args[0].encode().split(b",")
        if cmd in ("MOVE", "COPY"):
            dest = self.folders[args[1]]
            for u in uids:
                dest[u] = list(box[u])
                if cmd == "MOVE":
                    del box[u]
            return "OK", [b"done"]
        if cmd == "STORE":
            for u in uids:
                box[u][1].add("\\Deleted")
            return "OK", [b""]
        if cmd == "EXPUNGE":
            for u in uids:
                box.pop(u, None)
            return "OK", [b""]
        raise AssertionError(cmd)

    def expunge(self):
        box = self.folders[self.selected]
        for u in [u for u, (r, f) in box.items() if "\\Deleted" in f]:
            del box[u]
        return "OK", [b""]

    def logout(self):
        pass


def _mail(body, *, sender="alice@example.org", fid="abcdefg", subject="Re: Kfz-Versicherung"):
    m = EmailMessage()
    m["From"] = f"Alice <{sender}>"
    m["To"] = "brain@example.com"
    m["Subject"] = subject
    if fid:
        m["In-Reply-To"] = f"<{fid}.0123456789ab@{DOMAIN}>"
        m["References"] = f"<{fid}.0123456789ab@{DOMAIN}>"
    m.set_content(body)
    return m.as_bytes()


@pytest.fixture
def mailbox(proactive_on, tmp_path, monkeypatch):
    from config import proactive as cfgmod
    from config.settings import settings
    from proactive import findings, linking

    for k, v in {
        "SMTP_HOST": "smtp", "SMTP_FROM": "PaperlessBrain <brain@example.com>",
        "IMAP_HOST": "imap", "IMAP_USER": "brain",
        "IMAP_PROCESSED_FOLDER": "PaperlessBrain/Verarbeitet äöü",
    }.items():
        monkeypatch.setenv(k, v)
    cfgmod.proactive_settings.cache_clear()
    root = tmp_path / "vaults"
    monkeypatch.setattr(settings, "vault_root", root)
    findings._index.clear()
    d = root / "alice" / "PaperlessBrain Findings" / "Notices"
    d.mkdir(parents=True)
    (d / "k.md").write_text(
        "---\nfinding_id: abcdefg\nkind: insight\ntitle: Kfz\nstatus: surfaced\ntopic: kfz\n---\n## Summary\n\nx\n"
    )
    linking.link("alice", "email", "Alice@Example.org")
    return d / "k.md"


def _poll(conn):
    from proactive.imap_replies import poll_once

    return asyncio.run(poll_once(connect_fn=lambda cfg: conn))


def _status(path):
    import re

    return re.search(r"^status: (\w+)", path.read_text(), re.M).group(1)


# ── Parsing ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text,action",
    [
        ("1", "interested"), ("Ja, gerne!", "interested"), ("Interessant", "interested"),
        ("2", "later"), ("Später", "later"), ("später bitte", "later"),
        ("3", "never"), ("nie", "never"), ("Nein danke", "never"), ("Nie wieder!", "never"),
        ("\n\n  1.  \n", "interested"),
        ("Was soll das?", None), ("", None),
    ],
)
def test_keywords(text, action):
    assert parse_keyword(text) == action


def test_quoted_original_and_signature_do_not_count():
    reply = (
        "2\n\n-- \nAlice Example\nTel. 0123 / 3\n\n"
        "Am 26.10.2026 um 07:00 schrieb PaperlessBrain <brain@example.com>:\n"
        "> Oder antworte mit 1 (interessant), 2 (später) oder 3 (nie wieder).\n"
    )
    assert parse_keyword(reply) == "later"
    # nothing above the quote → the quote's "1" must not count
    assert parse_keyword("On Mon, 26 Oct 2026, PaperlessBrain wrote:\n> 1\n") is None
    assert parse_keyword("> 3\n\n--\nsig 1") is None


def test_only_our_message_ids_match():
    vals = [f"<abcdefg.0123456789ab@{DOMAIN}> <xyz@elsewhere.org>", "<bcdefgh.0123456789ab@evil.org>"]
    assert finding_ids_in(vals, DOMAIN) == ["abcdefg"]


# ── Polling ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("body,status", [("1", "interested"), ("Später", "snoozed"), ("nie", "dismissed")])
def test_reply_from_linked_address_changes_status(mailbox, body, status):
    conn = FakeIMAP({'"INBOX"': {b"7": [_mail(body), set()]}})
    _poll(conn)
    assert _status(mailbox) == status
    assert conn.folders['"INBOX"'] == {}
    assert b"7" in conn.folders['"PaperlessBrain/Verarbeitet &AOQA9gD8-"']


def test_reply_from_other_address_is_ignored_logged_and_moved(mailbox, caplog):
    conn = FakeIMAP({'"INBOX"': {b"7": [_mail("1", sender="mallory@example.org"), set()]}})
    with caplog.at_level(logging.WARNING, logger="proactive.imap"):
        _poll(conn)
    assert _status(mailbox) == "surfaced"
    assert any("not linked" in r.message for r in caplog.records)
    assert conn.folders['"INBOX"'] == {}


def test_quoted_reply_with_signature_and_non_ascii_subject(mailbox):
    body = "Ja\n\n--\nAlice\n\nAm 26.10.2026 schrieb PaperlessBrain:\n> 3 (nie wieder)\n"
    raw = _mail(body, subject="Re: Kündigungsfrist für die Kfz-Versicherung – Änderung")
    conn = FakeIMAP({'"INBOX"': {b"9": [raw, set()]}})
    (r,) = _poll(conn)
    assert r.action == "interested" and "Kündigungsfrist" in r.subject
    assert _status(mailbox) == "interested"


def test_without_move_capability_copy_store_expunge(mailbox):
    conn = FakeIMAP({'"INBOX"': {b"5": [_mail("1"), set()]}}, caps=("IMAP4REV1",))
    _poll(conn)
    assert conn.folders['"INBOX"'] == {}
    assert ("create", '"PaperlessBrain/Verarbeitet &AOQA9gD8-"') in conn.log


def test_seen_mail_is_left_alone_and_a_repeat_is_harmless(mailbox):
    conn = FakeIMAP({'"INBOX"': {b"1": [_mail("3"), {"\\Seen"}], b"2": [_mail("1"), set()]}})
    _poll(conn)
    assert b"1" in conn.folders['"INBOX"']  # not UNSEEN → not ours to touch
    assert _status(mailbox) == "interested"
    # the same reply delivered again (crash before the move): ignored by the state machine
    conn2 = FakeIMAP({'"INBOX"': {b"3": [_mail("2"), set()]}})
    _poll(conn2)
    assert _status(mailbox) == "interested"


def test_not_configured_never_connects(proactive_on):
    from proactive.imap_replies import poll_once

    def boom(cfg):
        raise AssertionError("connected")

    assert asyncio.run(poll_once(connect_fn=boom)) == []
