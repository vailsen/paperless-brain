"""Email replies to notifications (architecture §7.2, Phase 5).

The one place that *changes* a mailbox: it marks, creates and moves. It is a
module of its own so the chat tool's IMAP code (`services/imap_service.py`)
stays visibly read-only; only the folder-name and header helpers are shared.

Per poll, on the system mailbox (`.env`), folder `IMAP_FOLDER`, `UNSEEN` only:

1. Match `In-Reply-To` / `References` against our own Message-IDs
   (`<{finding_id}.{hex}@{our domain}>`) → finding.
2. The sender must be the finding owner's *linked* address. `From` can be
   spoofed, which is why a reply can only do low-risk things (a status change,
   queueing a deep dive), and why results only ever go to the linked address,
   never to a `Reply-To`.
3. Keyword parse of the first non-quoted, non-empty line — deterministic, no
   LLM: `1|ja|interessant` → interested, `2|später` → later, `3|nein|nie` → never.
   Anything else is ignored and logged.
4. Move the mail to `IMAP_PROCESSED_FOLDER` (created if missing), processed or
   not, so it is looked at once.

A mail is moved only after its reaction was applied: a crash in between leaves
it unseen, and the next poll applies the reaction again — which the state
machine then ignores, because the status already moved.
"""

from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import logging
import re
import ssl
from dataclasses import dataclass
from email.utils import parseaddr

from services.imap_service import _decode_header, _decode_part, _html_to_text, folder_to_wire

log = logging.getLogger("proactive.imap")

KEYWORDS = {
    "interested": {"1", "ja", "interessant", "yes", "interested", "interesting"},
    "later": {"2", "später", "spaeter", "later"},
    "never": {"3", "nein", "nie", "never", "no", "nie wieder", "never again"},
}

#: Lines that introduce the quoted original ("On … wrote:", "Am … schrieb …:").
_ATTRIBUTION = re.compile(r"^(on .+ wrote:|am .+ schrieb .+:|-----\s*original message)", re.I)


@dataclass
class Reply:
    uid: bytes
    sender: str
    finding_id: str | None
    action: str | None
    subject: str


def parse_keyword(text: str) -> str | None:
    """Action for the reply's first own line, or None."""
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith(">"):
            continue  # quoted text above the reply (rare, but bottom-posters exist)
        if _ATTRIBUTION.match(line) or line == "--":
            return None  # reached the quote or the signature without a keyword
        word = re.sub(r"[^\w\s]", "", line.lower()).strip()
        word = " ".join(word.split())
        for action, words in KEYWORDS.items():
            if word in words:
                return action
        first = word.split(" ")[0] if word else ""
        for action, words in KEYWORDS.items():
            if first in words:
                return action  # "1 bitte", "Ja, gerne!", "Nein danke"
        return None
    return None


def finding_ids_in(header_values: list[str], domain: str) -> list[str]:
    """Our finding ids referenced by In-Reply-To / References, newest first."""
    pat = re.compile(rf"<([a-z2-7]{{7}})\.[0-9a-f]{{12}}@{re.escape(domain)}>", re.I)
    out: list[str] = []
    for v in header_values:
        for fid in pat.findall(v or ""):
            fid = fid.lower()
            if fid not in out:
                out.append(fid)
    return out


def _text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        plain = next((p for p in msg.walk() if p.get_content_type() == "text/plain"), None)
        if plain is not None:
            return _decode_part(plain)
        html = next((p for p in msg.walk() if p.get_content_type() == "text/html"), None)
        return _html_to_text(_decode_part(html)) if html is not None else ""
    text = _decode_part(msg)
    return _html_to_text(text) if msg.get_content_type() == "text/html" else text


def parse_mail(uid: bytes, raw: bytes, domain: str) -> Reply:
    msg = email.message_from_bytes(raw)
    refs = [msg.get("In-Reply-To", ""), msg.get("References", "")]
    fids = finding_ids_in(refs, domain)
    return Reply(
        uid=uid,
        sender=parseaddr(_decode_header(msg.get("From", "")))[1].strip().lower(),
        finding_id=fids[0] if fids else None,
        action=parse_keyword(_text_body(msg)),
        subject=_decode_header(msg.get("Subject", "")),
    )


# ── IMAP (blocking; run in a thread) ─────────────────────────────────────────


def connect(cfg) -> imaplib.IMAP4:
    ctx = ssl.create_default_context()
    if cfg.imap_security == "ssl":
        conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, ssl_context=ctx, timeout=30)
    else:
        conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=30)
        if cfg.imap_security == "starttls":
            conn.starttls(ssl_context=ctx)
    conn.login(cfg.imap_user, cfg.imap_password)
    return conn


def fetch_unseen(conn, folder: str) -> list[tuple[bytes, bytes]]:
    st, _ = conn.select(folder_to_wire(folder))
    if st != "OK":
        raise RuntimeError(f"cannot select {folder!r}")
    st, data = conn.uid("SEARCH", None, "UNSEEN")
    if st != "OK":
        return []
    out = []
    for uid in (data[0] or b"").split():
        # BODY.PEEK: reading does not set \\Seen — a crash before the move
        # leaves the mail for the next poll.
        st, parts = conn.uid("FETCH", uid, "(BODY.PEEK[])")
        if st != "OK":
            continue
        raw = next((p[1] for p in parts if isinstance(p, tuple) and len(p) > 1), None)
        if raw:
            out.append((uid, raw))
    return out


def ensure_folder(conn, folder: str) -> None:
    wire = folder_to_wire(folder)
    st, _ = conn.select(wire, readonly=True)
    if st == "OK":
        return
    st, data = conn.create(wire)
    if st != "OK" and b"exist" not in b" ".join(d for d in data if isinstance(d, bytes)).lower():
        raise RuntimeError(f"cannot create {folder!r}: {data}")


def move(conn, uids: list[bytes], src: str, dest: str) -> None:
    if not uids:
        return
    ensure_folder(conn, dest)
    conn.select(folder_to_wire(src))
    wire = folder_to_wire(dest)
    caps = {c.upper() for c in (getattr(conn, "capabilities", ()) or ())}
    uid_set = b",".join(uids).decode()
    if "MOVE" in caps:
        st, data = conn.uid("MOVE", uid_set, wire)
        if st == "OK":
            return
    st, data = conn.uid("COPY", uid_set, wire)
    if st != "OK":
        raise RuntimeError(f"COPY to {dest!r} failed: {data}")
    conn.uid("STORE", uid_set, "+FLAGS.SILENT", r"(\Deleted)")
    if "UIDPLUS" in caps:
        conn.uid("EXPUNGE", uid_set)
    else:
        conn.expunge()


def _fetch_sync(cfg, connect_fn) -> list[tuple[bytes, bytes]]:
    conn = connect_fn(cfg)
    try:
        return fetch_unseen(conn, cfg.imap_folder)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _move_sync(cfg, connect_fn, uids: list[bytes]) -> None:
    conn = connect_fn(cfg)
    try:
        move(conn, uids, cfg.imap_folder, cfg.imap_processed_folder)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ── One poll ─────────────────────────────────────────────────────────────────


async def poll_once(connect_fn=connect) -> list[Reply]:
    """Fetch, apply, move. Returns what was seen (for logs and tests)."""
    from config.proactive import proactive_settings
    from proactive import linking, reactions
    from proactive.notify.email import from_domain

    cfg = proactive_settings()
    if not cfg.email_available:
        return []
    mails = await asyncio.to_thread(_fetch_sync, cfg, connect_fn)
    domain = from_domain()
    replies: list[Reply] = []
    done: list[bytes] = []
    for uid, raw in mails:
        try:
            r = parse_mail(uid, raw, domain)
        except Exception as exc:
            log.warning("imap: unparsable mail %s moved aside: %s", uid, exc)
            done.append(uid)
            continue
        replies.append(r)
        owner = reactions.owner_of(r.finding_id) if r.finding_id else None
        linked = linking.address(owner, "email") if owner else None
        if owner is None:
            log.info("imap: mail %s refers to no known finding — moved", uid)
        elif not linked or linking.norm("email", linked) != r.sender:
            log.warning(
                "imap: reply about finding %s from an address not linked to its owner — ignored",
                r.finding_id,
            )
        elif r.action is None:
            log.info("imap: reply about finding %s has no keyword — ignored", r.finding_id)
        else:
            await reactions.react(owner, r.finding_id, r.action, channel="email")
        done.append(uid)
    if done:
        await asyncio.to_thread(_move_sync, cfg, connect_fn, done)
    return replies
