# services/imap_service.py
"""IMAP email search (read-only).

Builds standard IMAP search criteria from structured inputs.
On Gmail servers (host contains 'gmail' or 'google'), also attempts
X-GM-RAW search (Gmail's native search syntax) which is faster and
more accurate; falls back to standard IMAP on any error.
"""

import asyncio
import email
import email.header
import imaplib
import re
from datetime import datetime, timezone
from email.utils import mktime_tz, parsedate_tz

from config.settings import local_tz

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


def _body_snippet(msg: email.message.Message, max_chars: int = 400) -> str:
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    charset = part.get_content_charset() or "utf-8"
                    text = part.get_payload(decode=True).decode(charset, errors="replace")
                    break
                except Exception:
                    pass
    else:
        try:
            charset = msg.get_content_charset() or "utf-8"
            text = msg.get_payload(decode=True).decode(charset, errors="replace")
        except Exception:
            pass

    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())[:max_chars]


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


def _is_gmail(host: str) -> bool:
    return "gmail" in host.lower() or "google" in host.lower()


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


def _all_searchable_folders(conn: imaplib.IMAP4) -> list[str]:
    """Return all folder names worth searching (skip spam/trash/drafts)."""
    raw_folders = _list_folders(conn)
    result = []
    for raw in raw_folders:
        name = _extract_folder_name(raw)
        if not name:
            continue
        upper = name.upper()
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
        upper = raw.upper()
        if any(k in upper for k in allmail_keywords):
            name = _extract_folder_name(raw)
            if name:
                return name
    return None


def _best_sent_folder(conn: imaplib.IMAP4) -> str:
    sent_keywords = ("SENT", "GESENDET", "GESENDETE")
    for raw in _list_folders(conn):
        upper = raw.upper()
        if any(k in upper for k in sent_keywords):
            name = _extract_folder_name(raw)
            if name:
                return name
    return "INBOX"


def _build_imap_criteria(inputs: dict) -> str:
    """Build standard IMAP SEARCH criteria string from inputs dict."""
    parts: list[str] = []

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
            parts.append(_imap_or(or_terms))
        else:
            parts.append(f'TEXT "{query}"')
    if subject := _q(inputs.get("subject", "")):
        parts.append(f'SUBJECT "{subject}"')
    if raw_from := _q(inputs.get("from_addr", "")):
        from_token = _surname(raw_from) if " " in raw_from else raw_from
        parts.append(f'FROM "{from_token}"')
    if raw_to := _q(inputs.get("to_addr", "")):
        to_token = _surname(raw_to) if " " in raw_to else raw_to
        parts.append(f'TO "{to_token}"')
    if since_str := inputs.get("since", ""):
        d = _imap_date(since_str)
        if d:
            parts.append(f"SINCE {d}")
    if before_str := inputs.get("before", ""):
        d = _imap_date(before_str)
        if d:
            parts.append(f"BEFORE {d}")
    if inputs.get("unseen_only"):
        parts.append("UNSEEN")

    return " ".join(parts) if parts else "ALL"


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
    "full":    "(BODY.PEEK[])",
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


def _fetch_messages(
    conn: imaplib.IMAP4,
    message_ids: list[bytes],
    max_results: int,
    sort_by_date: bool = False,
    detail: str = "snippet",
    gmail_ids_ordered: bool = False,
) -> list[dict]:
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
    for eid in reversed(message_ids) if not gmail_ids_ordered else reversed(message_ids):
        try:
            _, msg_data = conn.fetch(eid, fetch_cmd)
            if not msg_data or not isinstance(msg_data[0], tuple):
                continue
            r = _parse_one(msg_data[0][1], detail=detail)
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

        # list_folders_only: diagnostic mode
        if inputs.get("list_folders_only"):
            folders = _list_folders(conn)
            return {"folder": "", "results": [], "all_folders": folders}

        _has_query = any(inputs.get(k) for k in ("query", "subject", "from_addr", "since", "before", "unseen_only"))
        # detail: "headers" | "snippet" | "full" (full_body=True is legacy alias for "full")
        detail = inputs.get("detail") or ("full" if inputs.get("full_body") else "snippet")
        offset = max(0, int(inputs.get("offset") or 0))
        criteria = _build_imap_criteria(inputs)

        def _search_folder(fname: str) -> list[bytes]:
            try:
                st, _ = conn.select(fname, readonly=True)
                if st != "OK":
                    return []
                # Gmail: always prefer X-GM-RAW (native Gmail search, finds ALL mail including very old)
                if _is_gmail(host):
                    raw_q = _build_gmail_raw(inputs)
                    if raw_q:
                        safe = raw_q.replace('"', "'")
                        try:
                            st2, d2 = conn.search(None, f'X-GM-RAW "{safe}"')
                            if st2 == "OK" and d2 and d2[0]:
                                return d2[0].split()
                        except Exception:
                            pass
                try:
                    st3, d3 = conn.search("UTF-8", criteria)
                except Exception:
                    try:
                        st3, d3 = conn.search(None, criteria)
                    except Exception:
                        return []
                if st3 == "OK" and d3 and d3[0]:
                    return d3[0].split()
            except Exception:
                pass
            return []

        # ── Determine which folders to search ────────────────────────────────
        if inputs.get("folder"):
            search_folders = [inputs["folder"]]
        elif inputs.get("to_addr"):
            search_folders = [_best_sent_folder(conn)]
        elif not _has_query:
            # Inbox overview — no filter, just newest
            search_folders = ["INBOX"]
        elif _is_gmail(host):
            # Gmail: single all-mail folder covers everything
            allmail = _has_allmail_folder(conn)
            search_folders = [allmail or "INBOX"]
        else:
            # Non-Gmail with filter: search ALL folders to find old emails
            search_folders = _all_searchable_folders(conn)

        # ── Collect IDs per folder ────────────────────────────────────────────
        folder_ids: list[tuple[str, list[bytes]]] = []
        folder_hit_counts: dict[str, int] = {}
        for fname in search_folders:
            fids = _search_folder(fname)
            folder_hit_counts[fname] = len(fids)
            if fids:
                folder_ids.append((fname, fids))

        # Debug summary: searched X folders, hits per folder
        _debug_parts = [f"{fn}({cnt})" for fn, cnt in folder_hit_counts.items()]
        folder_debug = f"Durchsucht: {', '.join(_debug_parts)}"

        if not folder_ids:
            return {"folder": folder_debug, "results": []}

        folder_label = folder_debug

        if not _has_query:
            # Fast path: INBOX overview, newest N by sequence
            fname, fids = folder_ids[0]
            conn.select(fname, readonly=True)
            total = len(fids)
            # newest = last in sequence; reverse so index 0 = newest, then apply offset
            sorted_ids = list(reversed(fids))
            page = sorted_ids[offset: offset + max_results]
            results = _fetch_messages(conn, page, max_results, sort_by_date=False, detail=detail)
            return {"folder": fname, "results": results, "total": total, "offset": offset}

        is_gmail = _is_gmail(host)

        # ── Gmail fast path: UIDs are chronological — newest = highest UID ──────
        if is_gmail and len(folder_ids) == 1:
            fname, fids = folder_ids[0]
            conn.select(fname, readonly=True)
            total = len(fids)
            sorted_ids = list(reversed(fids))          # newest first
            page = sorted_ids[offset: offset + max_results]
            results = _fetch_messages(conn, page, max_results, sort_by_date=False, detail=detail)
            return {"folder": folder_debug, "results": results, "total": total, "offset": offset}

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
        page_dated = dated[offset: offset + max_results]

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
        return {"folder": folder_label, "results": results, "total": total, "offset": offset}

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
