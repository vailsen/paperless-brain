# services/imap_service.py
"""IMAP email search (read-only).

Builds standard IMAP search criteria from structured inputs.
On Gmail servers (CAPABILITY advertises X-GM-EXT-1, or the host looks like
Gmail), searches run as X-GM-RAW — Google's own index, so the results match
what the Gmail app shows; falls back to standard IMAP on any error.

Two things about non-ASCII are load-bearing here:

* `imaplib` encodes every *str* command argument as **ASCII**
  (`IMAP4._encoding`), so a criteria string containing an umlaut raises
  `UnicodeEncodeError` before a single byte reaches the server. Criteria are
  therefore built as text and handed to `search()` as **bytes** together with
  `CHARSET UTF-8`; bytes arguments pass through `_command` untouched.
* Folder names travel in modified UTF-7 (RFC 3501 §5.1.3). The LLM must never
  see `Bestellvorg&AOQ-nge` and must never have to produce it, so names are
  decoded at the tool boundary and re-encoded before `SELECT`.
"""

import asyncio
import base64
import email
import email.header
import imaplib
import logging
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import mktime_tz, parsedate_tz

from config.settings import local_tz

_log = logging.getLogger(__name__)

# RFC 3501 date-month: always English abbreviations, regardless of any locale.
_IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


# ── Helpers ───────────────────────────────────────────────────────────────────


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = email.header.decode_header(value)
    result = ""
    for part, enc in parts:
        if isinstance(part, bytes):
            result += part.decode(enc or "utf-8", errors="replace")
        else:
            result += str(part)
    return result.strip()


def _decode_part(part: email.message.Message) -> str:
    """Payload of one MIME part as text. '' when it cannot be decoded.

    `decode=True` undoes quoted-printable/base64 — without it the body is still
    `Drehmomentschl=C3=BCssel` and nothing matches it.
    """
    try:
        payload = part.get_payload(decode=True)
        if not payload:
            return ""
        return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
    except Exception:
        return ""


def _html_to_text(html: str) -> str:
    """Flatten HTML, keeping the parts order mails hide their content in.

    Shop mails carry the full product title in the image `alt` attribute and in
    the link slug long after the visible subject line has been truncated, so
    both are kept rather than stripped with the rest of the markup.
    """
    import html as _html
    import urllib.parse

    # Script/style first — their contents are not text.
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    # <img alt="…"> → the alt text itself.
    html = re.sub(
        r"""(?is)<img\b[^>]*?\balt\s*=\s*["']([^"']+)["'][^>]*>""",
        r" \1 ",
        html,
    )

    def _href(m: re.Match) -> str:
        url = m.group(1)
        try:
            url = urllib.parse.unquote(url)
        except Exception:
            pass
        # The slug carries the words; the query string is tracking noise.
        return " " + re.sub(r"[/_+-]+", " ", url.split("?", 1)[0]) + " "

    html = re.sub(r"""(?is)<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>""", _href, html)
    text = re.sub(r"<[^>]+>", " ", html)
    return _html.unescape(text)


def _body_snippet(msg: email.message.Message, max_chars: int = 400) -> str:
    plain = ""
    html = ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        if part.get_content_maintype() == "multipart":
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and not plain:
            plain = _decode_part(part)
        elif ctype == "text/html" and not html:
            html = _decode_part(part)

    # HTML-only mails (most order confirmations) used to yield an empty snippet
    # here, so the model never saw the body at all. When both parts exist the
    # plain one leads and the HTML fills whatever budget is left: the visible
    # text is the better summary, but the alt/href leftovers are where a
    # truncated product title is still spelled out in full.
    text = " ".join(plain.split())
    if html and len(text) < max_chars:
        extra = " ".join(_html_to_text(html).split())
        if extra:
            text = f"{text} {extra}".strip()
    return text[:max_chars]


def _imap_date(date_str: str) -> str | None:
    """Convert YYYY-MM-DD to IMAP date string (e.g. '15-Jan-2024')."""
    try:
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        return f"{d.day}-{_IMAP_MONTHS[d.month - 1]}-{d.year}"
    except ValueError:
        return None


def _format_email_date(date_str: str) -> str:
    if not date_str:
        return ""
    try:
        parsed = parsedate_tz(date_str)
        if parsed:
            ts = mktime_tz(parsed)
            dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(local_tz())
            return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        pass
    return date_str


def _is_gmail(host: str, conn: imaplib.IMAP4 | None = None) -> bool:
    """True when Gmail's own search (X-GM-RAW) is available.

    The capability is the real signal — the host heuristic only covers a
    connection that has not been probed yet (and custom-domain Google Workspace
    accounts reach imap.gmail.com anyway).
    """
    if conn is not None:
        try:
            if any("X-GM-EXT-1" in str(c).upper() for c in (conn.capabilities or ())):
                return True
        except Exception:
            pass
    return "gmail" in host.lower() or "google" in host.lower()


# ── Modified UTF-7 (RFC 3501 §5.1.3) ──────────────────────────────────────────
#
# Not the stdlib's `utf-7`: IMAP uses `&` as the shift character instead of `+`
# and `,` instead of `/` in the base64 alphabet.


def _b64_encode(chunk: str) -> str:
    encoded = base64.b64encode(chunk.encode("utf-16-be")).decode("ascii")
    return encoded.rstrip("=").replace("/", ",")


def _b64_decode(chunk: str) -> str:
    padded = chunk.replace(",", "/")
    padded += "=" * (-len(padded) % 4)
    return base64.b64decode(padded).decode("utf-16-be")


def imap_utf7_encode(value: str) -> str:
    """Plain text → modified UTF-7, as IMAP wants folder names."""
    out: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        if buf:
            out.append("&" + _b64_encode("".join(buf)) + "-")
            buf.clear()

    for ch in value:
        if ch == "&":
            flush()
            out.append("&-")
        elif "\x20" <= ch <= "\x7e":
            flush()
            out.append(ch)
        else:
            buf.append(ch)
    flush()
    return "".join(out)


def imap_utf7_decode(value: str) -> str:
    """Modified UTF-7 → plain text. Returns the input unchanged if malformed."""
    if "&" not in value:
        return value
    out: list[str] = []
    i = 0
    try:
        while i < len(value):
            ch = value[i]
            if ch != "&":
                out.append(ch)
                i += 1
                continue
            end = value.find("-", i + 1)
            if end == -1:          # unterminated shift — not our encoding
                return value
            chunk = value[i + 1 : end]
            out.append("&" if not chunk else _b64_decode(chunk))
            i = end + 1
    except Exception:
        return value
    return "".join(out)


_SKIP_FOLDER_KEYWORDS = (
    "SPAM", "JUNK", "TRASH", "PAPIERKORB", "GELÖSCHT", "DELETED",
    "DRAFT", "ENTWURF", "OUTBOX",
)


def _list_folders(conn: imaplib.IMAP4) -> list[str]:
    try:
        _, folder_list = conn.list()
        return [
            (entry.decode() if isinstance(entry, bytes) else str(entry))
            for entry in (folder_list or [])
        ]
    except Exception:
        return []


def _extract_folder_name(raw: str) -> str | None:
    m = re.search(r'"([^"]+)"$', raw) or re.search(r'(\S+)$', raw)
    return f'"{m.group(1)}"' if m else None


def folder_display(name: str) -> str:
    """Wire name (quoted, modified UTF-7) → readable text for the LLM and UI."""
    return imap_utf7_decode(name.strip().strip('"'))


def folder_to_wire(name: str) -> str:
    """Readable name → the quoted modified-UTF-7 form `SELECT` expects.

    Already-encoded input passes through unchanged: a round trip through
    decode+encode is the identity for a well-formed name, so an agent that
    echoes back what `list_folders_only` printed and one that types
    `Bestellvorgänge` land on the same wire string.
    """
    plain = name.strip().strip('"')
    return f'"{imap_utf7_encode(imap_utf7_decode(plain))}"'


def _all_searchable_folders(conn: imaplib.IMAP4) -> list[str]:
    """Return all folder names worth searching (skip spam/trash/drafts)."""
    raw_folders = _list_folders(conn)
    result = []
    for raw in raw_folders:
        name = _extract_folder_name(raw)
        if not name:
            continue
        # Decoded, because a German Trash folder is `Gel&APY-scht` on the wire
        # and would never match the keyword list in its encoded form.
        upper = folder_display(name).upper()
        if any(k in upper for k in _SKIP_FOLDER_KEYWORDS):
            continue
        result.append(name)
    return result or ["INBOX"]


def _has_allmail_folder(conn: imaplib.IMAP4) -> str | None:
    """Return name of All-Mail folder. Detects via \\All attribute (RFC 6154) first, then by name."""
    raw_folders = _list_folders(conn)
    # 1. RFC 6154 special-use attribute \All (most reliable, language-independent)
    for raw in raw_folders:
        m = re.match(r'\(([^)]*)\)', raw)
        if m and r"\all" in m.group(1).lower():
            name = _extract_folder_name(raw)
            if name:
                return name
    # 2. Name-based fallback (covers providers that don't advertise \All)
    allmail_keywords = (
        "ALL MAIL", "ALLE MAILS", "ALLMAIL",
        "ALLE NACHRICHTEN",          # German Gmail
        "TOUS LES MESSAGES",         # French Gmail
        "TODOS LOS MENSAJES",        # Spanish Gmail
        "ALL MESSAGES",
    )
    for raw in raw_folders:
        upper = imap_utf7_decode(raw).upper()
        if any(k in upper for k in allmail_keywords):
            name = _extract_folder_name(raw)
            if name:
                return name
    return None


def _best_sent_folder(conn: imaplib.IMAP4) -> str:
    sent_keywords = ("SENT", "GESENDET", "GESENDETE")
    for raw in _list_folders(conn):
        upper = imap_utf7_decode(raw).upper()
        if any(k in upper for k in sent_keywords):
            name = _extract_folder_name(raw)
            if name:
                return name
    return "INBOX"


def _criteria_parts(inputs: dict) -> list[tuple[str, str | None]]:
    """SEARCH criteria as (keyword, value) pairs.

    Kept as pairs rather than one string so a non-ASCII *value* can be sent as
    an IMAP literal, which is the only form RFC 3501 defines for it — see
    `_search`. `value is None` marks a token that is already complete
    (`UNSEEN`, `SINCE 1-Jan-2024`, an OR tree).
    """
    parts: list[tuple[str, str | None]] = []

    def _q(v: str) -> str:
        return v.replace('"', " ").strip()

    def _surname(name: str) -> str:
        """For multi-word names return the last token (most likely surname/domain)."""
        parts = name.split()
        return parts[-1] if parts else name

    if query := _q(inputs.get("query", "")):
        import re as _re
        or_terms = [t.strip() for t in _re.split(r'\bOR\b', query) if t.strip()]
        if len(or_terms) > 1:
            def _imap_or(terms: list[str]) -> str:
                if len(terms) == 1:
                    return f'TEXT "{terms[0]}"'
                return f'OR TEXT "{terms[0]}" {_imap_or(terms[1:])}'
            # An OR tree holds several values and cannot carry a literal.
            parts.append((_imap_or(or_terms), None))
        else:
            # One TEXT key per word, which IMAP ANDs together — the same
            # semantics Gmail gives a space. As a single key the whole string
            # would be one substring match, so "invoice Stadtwerke" only
            # matched mails with those two words adjacent, which is never what
            # the caller meant.
            for word in query.split():
                parts.append(("TEXT", word))
    if subject := _q(inputs.get("subject", "")):
        parts.append(("SUBJECT", subject))
    if raw_from := _q(inputs.get("from_addr", "")):
        from_token = _surname(raw_from) if " " in raw_from else raw_from
        parts.append(("FROM", from_token))
    if raw_to := _q(inputs.get("to_addr", "")):
        to_token = _surname(raw_to) if " " in raw_to else raw_to
        parts.append(("TO", to_token))
    if since_str := inputs.get("since", ""):
        d = _imap_date(since_str)
        if d:
            parts.append((f"SINCE {d}", None))
    if before_str := inputs.get("before", ""):
        d = _imap_date(before_str)
        if d:
            parts.append((f"BEFORE {d}", None))
    if inputs.get("unseen_only"):
        parts.append(("UNSEEN", None))

    return parts


def _render_part(part: tuple[str, str | None]) -> str:
    keyword, value = part
    return keyword if value is None else f'{keyword} "{value}"'


def _build_imap_criteria(inputs: dict) -> str:
    """Standard IMAP SEARCH criteria as one string."""
    parts = _criteria_parts(inputs)
    return " ".join(_render_part(p) for p in parts) if parts else "ALL"


def _literal_split(parts: list[tuple[str, str | None]]) -> tuple[str, str] | None:
    """Split criteria into (prefix, literal value), or None if not applicable.

    A literal is always the *last* thing on the command line, and `imaplib`
    can only append one, so this works exactly when a single value is
    non-ASCII — which is the normal case ("Drehmomentschlüssel", one field).
    """
    non_ascii = [p for p in parts if p[1] is not None and not p[1].isascii()]
    if len(non_ascii) != 1:
        return None
    target = non_ascii[0]
    others = [_render_part(p) for p in parts if p is not target]
    prefix = " ".join([*others, target[0]])
    return prefix, target[1]


def _build_gmail_raw(inputs: dict) -> str:
    """Build a Gmail X-GM-RAW search string (Gmail web search syntax)."""
    parts: list[str] = []
    if query := inputs.get("query", "").strip():
        parts.append(query)
    if subject := inputs.get("subject", "").strip():
        parts.append(f"subject:{subject}")
    if raw_from := inputs.get("from_addr", "").strip():
        from_token = raw_from.split()[-1] if " " in raw_from else raw_from
        parts.append(f"from:{from_token}")
    if raw_to := inputs.get("to_addr", "").strip():
        to_token = raw_to.split()[-1] if " " in raw_to else raw_to
        parts.append(f"to:{to_token}")
    if since_str := inputs.get("since", "").strip():
        # Gmail raw uses YYYY/MM/DD
        try:
            d = datetime.strptime(since_str, "%Y-%m-%d")
            parts.append(f"after:{d.strftime('%Y/%m/%d')}")
        except ValueError:
            pass
    if before_str := inputs.get("before", "").strip():
        try:
            d = datetime.strptime(before_str, "%Y-%m-%d")
            parts.append(f"before:{d.strftime('%Y/%m/%d')}")
        except ValueError:
            pass
    if inputs.get("unseen_only"):
        parts.append("is:unread")
    return " ".join(parts)


def _search_literal(conn: imaplib.IMAP4, prefix: str, term: str) -> list[bytes] | None:
    """`SEARCH CHARSET UTF-8 <prefix> {n}` + the term as an 8-bit literal.

    The form RFC 3501 actually defines for non-ASCII search keys, and the one
    Gmail needs: handed the same bytes inside a quoted string it answers OK and
    matches nothing — it does not decode them as UTF-8, so an exact search for
    "Drehmomentschlüssel" came back empty while the Gmail app found the mail.

    Returns None when the command itself failed, so the caller can try another
    encoding; an empty list means the server searched and found nothing.
    """
    payload = term.encode("utf-8")
    try:
        # imaplib appends `{len}` and streams the bytes after the server's
        # continuation response. Only ever one literal, always last.
        conn.literal = payload
        status, data = conn.search("UTF-8", prefix)
    except Exception as e:
        _log.debug("IMAP SEARCH literal failed (%s): %s", prefix, e)
        return None
    finally:
        # A literal left behind would be appended to the next command.
        conn.literal = None
    if status != "OK":
        return None
    return data[0].split() if data and data[0] else []


def _search(
    conn: imaplib.IMAP4,
    criteria: str,
    literal_split: tuple[str, str] | None = None,
) -> list[bytes]:
    """Run SEARCH with criteria that may contain non-ASCII. [] on failure.

    Three encodings, in decreasing order of correctness:

    1. `literal_split` — the RFC form, and the only one Gmail matches on.
    2. The whole criteria as UTF-8 **bytes** with `CHARSET UTF-8`. `imaplib`
       encodes str arguments as ASCII, so passing the string itself raises
       `UnicodeEncodeError` inside the library — swallowed by the caller's
       `except` and indistinguishable from "no such mail". That was why every
       German query returned nothing at all.
    3. Diacritics folded away (ü → u), for servers that reject CHARSET.
    """
    if literal_split:
        hits = _search_literal(conn, literal_split[0], literal_split[1])
        if hits is not None:
            return hits

    attempts: list[tuple[str | None, str | bytes]] = []
    if criteria.isascii():
        attempts.append((None, criteria))
    else:
        attempts.append(("UTF-8", criteria.encode("utf-8")))
        folded = _fold_ascii(criteria)
        if folded != criteria and folded.isascii():
            attempts.append((None, folded))
    for charset, crit in attempts:
        try:
            status, data = conn.search(charset, crit)
        except Exception as e:
            _log.debug("IMAP SEARCH failed (charset=%s): %s", charset, e)
            continue
        if status == "OK" and data and data[0]:
            return data[0].split()
        if status == "OK":
            return []          # searched fine, genuinely nothing
    return []


def _fold_ascii(value: str) -> str:
    """Strip diacritics: 'Drehmomentschlüssel' → 'Drehmomentschlussel'."""
    return "".join(
        c for c in unicodedata.normalize("NFD", value)
        if not unicodedata.combining(c)
    )


# Compound-word fallback: German product titles get cut off in subject lines
# ("Bestellt: … Drehmomentschlüsse"), so an exact substring search cannot match
# a query holding the whole word. Retry with a prefix instead.
_FALLBACK_MIN_LEN = 12
_FALLBACK_PREFIX_LEN = 10


def _shorten_term(term: str, keep_all_words: bool = True) -> str:
    """Cut over-long words of `term` to a prefix. '' when nothing to shorten.

    `keep_all_words` shortens in place ("Drehmomentschlüssel torque wrench" →
    "Drehmoment torque wrench"). Dropping the other words instead — which is
    what this used to do — turns a specific question into a search for a single
    common engineering term and buries the answer under everything that ever
    mentioned it.
    """
    words = term.split()
    long_words = [w for w in words if len(w) >= _FALLBACK_MIN_LEN]
    if not long_words:
        return ""
    if keep_all_words:
        shortened = [
            w[:_FALLBACK_PREFIX_LEN] if len(w) >= _FALLBACK_MIN_LEN else w
            for w in words
        ]
        result = " ".join(shortened)
    else:
        result = max(long_words, key=len)[:_FALLBACK_PREFIX_LEN]
    return "" if result == term else result


def _shortened_inputs(inputs: dict, keep_all_words: bool = True) -> dict | None:
    """A copy of `inputs` with query/subject cut to a prefix, or None."""
    changed = False
    variant = dict(inputs)
    for key in ("query", "subject"):
        short = _shorten_term(str(inputs.get(key) or ""), keep_all_words)
        if short and short != inputs.get(key):
            variant[key] = short
            changed = True
    return variant if changed else None


def _fallback_variants(inputs: dict) -> list[dict]:
    """Progressively looser searches to try after the exact one found nothing.

    Two stages, narrow before wide: shorten the long words but keep every term,
    then — only if that also fails — the longest term alone. The second stage
    is where the noise lives (one common word matches half the archive), so it
    must never run before the first.
    """
    variants: list[dict] = []
    seen: set[str] = set()
    for keep_all in (True, False):
        variant = _shortened_inputs(inputs, keep_all_words=keep_all)
        if not variant:
            continue
        key = f"{variant.get('query', '')}|{variant.get('subject', '')}"
        if key in seen:
            continue
        seen.add(key)
        variants.append(variant)
    return variants


def _rank_by_term(results: list[dict], term: str) -> list[dict]:
    """Stable-sort the results that best match `term` to the front.

    Scored per word, not on the whole string: a fallback search runs precisely
    because the full phrase matched nothing, so a containment test on it is a
    constant and sorts nothing. A mail holding "Drehmomentschlüssel" has to beat
    one that only shares the prefix "Drehmoment".
    """
    full = _fold_ascii(term).casefold().strip()
    # Short words ("and", "der", "3") match everywhere and carry no signal.
    words = [w for w in full.split() if len(w) > 3]
    if not full:
        return results

    def score(r: dict) -> int:
        hay = _fold_ascii(f"{r.get('subject', '')} {r.get('snippet', '')}").casefold()
        points = 10 if full in hay else 0
        points += sum(1 for w in words if w in hay)
        return -points          # ascending sort → best first, ties keep date order

    return sorted(results, key=score)


# ── Sync worker ───────────────────────────────────────────────────────────────


def _parse_date_for_sort(date_str: str) -> datetime:
    try:
        parsed = parsedate_tz(date_str)
        if parsed:
            return datetime.fromtimestamp(mktime_tz(parsed))
    except Exception:
        pass
    return datetime.min


# detail levels: "headers" | "snippet" | "full"
_DETAIL_FETCH = {
    "headers": "(RFC822.HEADER)",          # full headers, no body — always parses correctly
    "snippet": "(BODY.PEEK[]<0.16384>)",   # first 16 KB — covers long headers + body preview
    # 256 KB, not the whole message. `full` keeps at most 40 000 characters of
    # text (`_DETAIL_CHARS`), and an unbounded BODY.PEEK[] downloaded every
    # attachment to get there — a construction thread with photos and PDFs cost
    # megabytes per message and minutes per search. The text parts of a MIME
    # message come first, so the window holds the body and drops the payload.
    "full":    "(BODY.PEEK[]<0.262144>)",
}
_DETAIL_CHARS = {"headers": 0, "snippet": 300, "full": 40000}


def _parse_one(msg_bytes: bytes, detail: str = "snippet") -> dict | None:
    if not msg_bytes:
        return None
    msg = email.message_from_bytes(msg_bytes)
    raw_date = msg.get("Date", "")
    if not raw_date and not msg.get("From") and not msg.get("Subject"):
        return None
    r: dict = {
        "date": _format_email_date(raw_date),
        "_raw_date": raw_date,
        "from": _decode_header(msg.get("From", "")),
        "subject": _decode_header(msg.get("Subject", "")) or "(no subject)",
    }
    if detail != "headers":
        r["snippet"] = _body_snippet(msg, max_chars=_DETAIL_CHARS[detail])
    return r


def _fetch_dates_batch(conn: imaplib.IMAP4, ids: list[bytes]) -> list[tuple[bytes, datetime]]:
    """Single IMAP command to fetch Date headers for all IDs (one round-trip)."""
    if not ids:
        return []
    id_set = b",".join(ids)
    result: list[tuple[bytes, datetime]] = []
    try:
        _, data = conn.fetch(id_set, "(BODY.PEEK[HEADER.FIELDS (DATE)])")
        for item in (data or []):
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            seq_m = re.match(rb'^(\d+)', item[0])
            if not seq_m:
                continue
            msg = email.message_from_bytes(item[1])
            dt = _parse_date_for_sort(msg.get("Date", ""))
            result.append((seq_m.group(1), dt))
    except Exception:
        pass
    return result


def _fetch_batch(
    conn: imaplib.IMAP4, ids: list[bytes], fetch_cmd: str
) -> dict[bytes, bytes]:
    """One IMAP round trip for many messages: sequence number → raw bytes.

    A 50-result search used to issue 50 FETCH commands. Against Gmail over a WAN
    the round trips, not the bytes, were most of the wait — a single search with
    full bodies took four and a half minutes, and a research run that made eight
    of them spent its entire budget inside IMAP.

    Returns whatever came back; the caller fetches the rest one by one, so a
    server that dislikes a set is slower, never wrong.
    """
    if len(ids) < 2:
        return {}
    out: dict[bytes, bytes] = {}
    try:
        _, data = conn.fetch(b",".join(ids), fetch_cmd)
    except Exception:
        return {}
    for item in (data or []):
        if not isinstance(item, tuple) or len(item) < 2 or not item[1]:
            continue
        if seq := re.match(rb"^(\d+)", item[0]):
            out[seq.group(1)] = item[1]
    return out


def _fetch_messages(
    conn: imaplib.IMAP4,
    message_ids: list[bytes],
    max_results: int,
    sort_by_date: bool = False,
    detail: str = "snippet",
    gmail_ids_ordered: bool = False,
    preserve_order: bool = False,
) -> list[dict]:
    """Fetch messages for `message_ids`.

    `preserve_order` means the caller has already put the ids in display order
    (newest first) and paginated them. Without it this function reverses the
    list — which is right for a raw ascending id list from SEARCH, and exactly
    wrong for an already-ordered page: it silently turned "newest 5" back into
    "oldest 5", so a search over a mailbox with history answered with mail from
    2011.
    """
    if sort_by_date and len(message_ids) > max_results:
        if gmail_ids_ordered:
            # Gmail UIDs are chronological — newest = highest → no header round-trips needed
            message_ids = list(reversed(list(reversed(message_ids))[-max_results * 2:]))[:max_results * 2]
            # just take last N (highest UIDs = newest)
            message_ids = message_ids[-(max_results * 2):]
        else:
            dated = _fetch_dates_batch(conn, message_ids)
            dated.sort(key=lambda x: x[1], reverse=True)
            top_seqs = {seq for seq, _ in dated[:max_results]}
            message_ids = [eid for eid in message_ids if eid in top_seqs][:max_results]

    fetch_cmd = _DETAIL_FETCH.get(detail, _DETAIL_FETCH["snippet"])
    results: list[dict] = []
    ordered_ids = message_ids if preserve_order else list(reversed(message_ids))
    wanted = ordered_ids[:max_results]
    batched = _fetch_batch(conn, wanted, fetch_cmd)
    for eid in ordered_ids:
        try:
            raw = batched.get(eid)
            if raw is None:
                _, msg_data = conn.fetch(eid, fetch_cmd)
                if not msg_data or not isinstance(msg_data[0], tuple):
                    continue
                raw = msg_data[0][1]
            r = _parse_one(raw, detail=detail)
            if r:
                results.append(r)
                if len(results) >= max_results:
                    break
        except Exception:
            continue
    if sort_by_date:
        results.sort(key=lambda r: _parse_date_for_sort(r["_raw_date"]), reverse=True)
    for r in results:
        r.pop("_raw_date", None)
    return results


def _search_sync(
    host: str,
    port: int,
    username: str,
    password: str,
    inputs: dict,
    use_ssl: bool,
    max_results: int,
) -> dict:
    ConnClass = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
    conn = ConnClass(host, port)

    try:
        conn.login(username, password)

        # list_folders_only: diagnostic mode. The LLM only ever sees plain text —
        # it must not have to read (or reproduce) modified UTF-7.
        if inputs.get("list_folders_only"):
            names = []
            for raw in _list_folders(conn):
                name = _extract_folder_name(raw)
                if name:
                    names.append(folder_display(name))
            return {"folder": "", "results": [], "all_folders": names}

        _has_query = any(inputs.get(k) for k in ("query", "subject", "from_addr", "since", "before", "unseen_only"))
        # detail: "headers" | "snippet" | "full" (full_body=True is legacy alias for "full")
        detail = inputs.get("detail") or ("full" if inputs.get("full_body") else "snippet")
        offset = max(0, int(inputs.get("offset") or 0))
        gmail = _is_gmail(host, conn)

        def _search_folder(fname: str, variant: dict) -> list[bytes]:
            try:
                st, _ = conn.select(fname, readonly=True)
                if st != "OK":
                    return []
                # Gmail: prefer X-GM-RAW — Google's own index, so stemming,
                # compound splitting and OCR on inline images all apply and the
                # results match what the Gmail app shows for the same query.
                if gmail:
                    raw_q = _build_gmail_raw(variant)
                    if raw_q:
                        safe = raw_q.replace('"', "'")
                        hits = _search(
                            conn,
                            f'X-GM-RAW "{safe}"',
                            # Gmail only decodes the term as UTF-8 when it
                            # arrives as a literal; quoted 8-bit matches nothing.
                            literal_split=("X-GM-RAW", safe) if not safe.isascii() else None,
                        )
                        if hits:
                            return hits
                parts = _criteria_parts(variant)
                return _search(
                    conn,
                    _build_imap_criteria(variant),
                    literal_split=_literal_split(parts),
                )
            except Exception as e:
                _log.debug("folder search failed (%s): %s", fname, e)
            return []

        # ── Determine which folders to search ────────────────────────────────
        if inputs.get("folder"):
            # The agent hands us plain text (that is all it ever sees) — put it
            # back on the wire the way IMAP wants it.
            search_folders = [folder_to_wire(str(inputs["folder"]))]
        elif inputs.get("to_addr"):
            search_folders = [_best_sent_folder(conn)]
        elif not _has_query:
            # Inbox overview — no filter, just newest
            search_folders = ["INBOX"]
        elif gmail:
            # Gmail: single all-mail folder covers everything
            allmail = _has_allmail_folder(conn)
            search_folders = [allmail or "INBOX"]
        else:
            # Non-Gmail with filter: search ALL folders to find old emails
            search_folders = _all_searchable_folders(conn)

        # ── Collect IDs per folder ────────────────────────────────────────────
        def _collect(variant: dict) -> tuple[list[tuple[str, list[bytes]]], dict[str, int]]:
            found: list[tuple[str, list[bytes]]] = []
            counts: dict[str, int] = {}
            for fname in search_folders:
                fids = _search_folder(fname, variant)
                counts[folder_display(fname)] = len(fids)
                if fids:
                    found.append((fname, fids))
            return found, counts

        folder_ids, folder_hit_counts = _collect(inputs)

        # Nothing found with the full term — retry with progressively shorter
        # prefixes. Compound nouns get truncated in subject lines
        # ("… Drehmomentschlüsse"), which no substring search can match against
        # the whole word.
        fallback_term = ""
        fallback_query = ""
        if not folder_ids:
            original = str(inputs.get("query") or inputs.get("subject") or "")
            for variant in _fallback_variants(inputs):
                attempt = str(variant.get("query") or variant.get("subject") or "")
                _log.info("IMAP: 0 hits for %r — retrying with %r", original, attempt)
                folder_ids, folder_hit_counts = _collect(variant)
                if folder_ids:
                    fallback_term, fallback_query = original, attempt
                    break

        # Debug summary: searched X folders, hits per folder
        _debug_parts = [f"{fn}({cnt})" for fn, cnt in folder_hit_counts.items()]
        folder_debug = f"searched: {', '.join(_debug_parts)}"
        if fallback_term:
            folder_debug += f" [no exact match — searched for '{fallback_query}' instead]"

        if not folder_ids:
            return {"folder": folder_debug, "results": []}

        folder_label = folder_debug

        # After a prefix fallback the hit list is deliberately too wide, so the
        # mail the user meant can sit well past the first page. Ranking only the
        # page would never move it up — fetch a wider window, rank that, then
        # cut. Capped: each candidate is one FETCH round-trip.
        rank_window = max_results
        if fallback_term:
            rank_window = min(max(max_results * 4, 20), 40)

        if not _has_query:
            # Fast path: INBOX overview, newest N by sequence
            fname, fids = folder_ids[0]
            conn.select(fname, readonly=True)
            total = len(fids)
            # newest = last in sequence; reverse so index 0 = newest, then apply offset
            sorted_ids = list(reversed(fids))
            page = sorted_ids[offset: offset + max_results]
            results = _fetch_messages(
                conn, page, max_results, sort_by_date=False,
                detail=detail, preserve_order=True,
            )
            return {"folder": folder_display(fname), "results": results, "total": total, "offset": offset}

        is_gmail = gmail

        # ── Gmail fast path: UIDs are chronological — newest = highest UID ──────
        if is_gmail and len(folder_ids) == 1:
            fname, fids = folder_ids[0]
            conn.select(fname, readonly=True)
            total = len(fids)
            sorted_ids = list(reversed(fids))          # newest first
            page = sorted_ids[offset: offset + rank_window]
            results = _fetch_messages(
                conn, page, len(page), sort_by_date=False,
                detail=detail, preserve_order=True,
            )
            if fallback_term:
                results = _rank_by_term(results, fallback_term)
            results = results[:max_results]
            return {"folder": folder_debug, "results": results, "total": total,
                    "offset": offset, "fallback": fallback_query}

        # ── Multi-folder: batch header fetch → global date sort → paginate ───────
        from collections import defaultdict
        dated: list[tuple[str, bytes, datetime]] = []
        for fname, fids in folder_ids:
            try:
                conn.select(fname, readonly=True)
            except Exception:
                continue
            batch = _fetch_dates_batch(conn, fids)
            for seq, dt in batch:
                dated.append((fname, seq, dt))

        if not dated:
            return {"folder": folder_label, "results": [], "total": 0, "offset": offset}

        dated.sort(key=lambda x: x[2], reverse=True)
        total = len(dated)
        page_dated = dated[offset: offset + rank_window]

        fetch_cmd = _DETAIL_FETCH.get(detail, _DETAIL_FETCH["snippet"])
        by_folder: dict[str, list[tuple[int, bytes]]] = defaultdict(list)
        for rank, (fname, seq, _) in enumerate(page_dated):
            by_folder[fname].append((rank, seq))

        ordered: list[tuple[int, dict]] = []
        for fname, ranked_ids in by_folder.items():
            try:
                conn.select(fname, readonly=True)
            except Exception:
                continue
            for rank, seq in ranked_ids:
                try:
                    _, msg_data = conn.fetch(seq, fetch_cmd)
                    if msg_data and isinstance(msg_data[0], tuple):
                        r = _parse_one(msg_data[0][1], detail=detail)
                        if r:
                            ordered.append((rank, r))
                except Exception:
                    continue

        ordered.sort(key=lambda x: x[0])
        results = [r for _, r in ordered]
        for r in results:
            r.pop("_raw_date", None)
        if fallback_term:
            results = _rank_by_term(results, fallback_term)
        results = results[:max_results]
        return {"folder": folder_label, "results": results, "total": total,
                "offset": offset, "fallback": fallback_query}

    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _test_connection_sync(
    host: str, port: int, username: str, password: str, use_ssl: bool
) -> str:
    try:
        ConnClass = imaplib.IMAP4_SSL if use_ssl else imaplib.IMAP4
        conn = ConnClass(host, port)
        conn.login(username, password)
        conn.logout()
        return ""
    except Exception as e:
        return str(e)


# ── Async API ─────────────────────────────────────────────────────────────────


async def search_emails(
    host: str,
    port: int,
    username: str,
    password: str,
    inputs: dict,
    use_ssl: bool = True,
    max_results: int = 10,
) -> dict:
    """Search emails. Returns dict with 'folder', 'results', and optionally 'all_folders'."""
    return await asyncio.to_thread(
        _search_sync, host, port, username, password, inputs, use_ssl, max_results
    )


async def test_connection(
    host: str, port: int, username: str, password: str, use_ssl: bool = True
) -> str:
    return await asyncio.to_thread(
        _test_connection_sync, host, port, username, password, use_ssl
    )
