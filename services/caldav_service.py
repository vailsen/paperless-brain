# services/caldav_service.py
"""Calendar search — two modes, same result format.

Mode A — iCal URL (Google Calendar, Nextcloud, etc.)
    Simple HTTPS GET of a secret/private iCal export URL.
    No authentication needed; the secret token in the URL is the credential.
    Get it from Google Calendar → Settings → [calendar] → "Secret address in iCal format".

Mode B — CalDAV (Nextcloud, iCloud, Fastmail, …)
    Sends a CalDAV REPORT request with Basic Auth and parses the multi-status
    XML response to extract iCal data.

Both modes use the same iCal parser and return the same event dict structure.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
from lxml import etree


# ── iCal parser ───────────────────────────────────────────────────────────────


def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").splitlines():
        if line.startswith((" ", "\t")) and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ical_date(s: str) -> str:
    s = s.strip().split(";")[-1].rstrip("Z")  # strip TZID= param if present
    try:
        if len(s) == 8:
            return datetime.strptime(s, "%Y%m%d").strftime("%d.%m.%Y")
        return datetime.strptime(s[:15], "%Y%m%dT%H%M%S").strftime("%d.%m.%Y %H:%M")
    except Exception:
        return s


def _parse_ical_date_raw(s: str) -> str:
    """Return raw date string for sorting (strip TZID)."""
    return s.strip().split(":")[-1].rstrip("Z")[:15]


def _parse_ical_text(ical_text: str) -> list[dict[str, Any]]:
    """Extract VEVENT dicts from any iCal string."""
    events: list[dict[str, Any]] = []
    lines = _unfold(ical_text)
    in_event = False
    block: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "BEGIN:VEVENT":
            in_event = True
            block = []
        elif stripped == "END:VEVENT" and in_event:
            in_event = False
            ev: dict[str, Any] = {}
            for l in block:
                if ":" not in l:
                    continue
                key_part, _, val = l.partition(":")
                key = key_part.split(";")[0].upper().strip()
                val = _unescape(val.strip())
                if key == "SUMMARY":
                    ev["summary"] = val
                elif key == "DTSTART":
                    ev["dtstart_raw"] = _parse_ical_date_raw(val)
                    ev["dtstart"] = _parse_ical_date(val)
                elif key == "DTEND":
                    ev["dtend"] = _parse_ical_date(val)
                elif key == "DESCRIPTION":
                    ev["description"] = val[:600]
                elif key == "LOCATION":
                    ev["location"] = val
                elif key == "STATUS":
                    ev["status"] = val
            if ev.get("summary") or ev.get("description"):
                events.append(ev)
        elif in_event:
            block.append(line)

    return events


# ── CalDAV helpers ────────────────────────────────────────────────────────────

_NS_C = "urn:ietf:params:xml:ns:caldav"


def _extract_ical_from_multistatus(xml_bytes: bytes) -> list[str]:
    try:
        root = etree.fromstring(xml_bytes)
        return [el.text for el in root.iter(f"{{{_NS_C}}}calendar-data") if el.text]
    except Exception:
        raw = xml_bytes.decode("utf-8", errors="replace")
        return re.findall(r"(BEGIN:VCALENDAR.*?END:VCALENDAR)", raw, re.DOTALL)


def _build_report_xml(date_from: str, date_to: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">\n'
        "  <D:prop><D:getetag/><C:calendar-data/></D:prop>\n"
        "  <C:filter>\n"
        '    <C:comp-filter name="VCALENDAR">\n'
        '      <C:comp-filter name="VEVENT">\n'
        f'        <C:time-range start="{date_from}" end="{date_to}"/>\n'
        "      </C:comp-filter>\n"
        "    </C:comp-filter>\n"
        "  </C:filter>\n"
        "</C:calendar-query>"
    )


# Frequent German filler words that carry no search signal — dropped from queries
# so "Termin im Juni beim Zahnarzt" effectively searches for zahnarzt/termin.
_CAL_STOPWORDS = {
    "im", "am", "an", "in", "auf", "bei", "beim", "zum", "zur", "der", "die", "das",
    "den", "dem", "und", "oder", "mit", "für", "ein", "eine", "einen", "einem",
    "mein", "meine", "meinen", "meinem", "von", "vom", "ist", "war", "wann", "hab",
    "habe", "ich", "wir", "es", "the", "a", "an", "of",
}

try:  # Snowball gives proper German stems; heuristic fallback if the dep is absent.
    import snowballstemmer as _snowball

    _GER_STEMMER = _snowball.stemmer("german")

    def _stem(word: str) -> str:
        w = word.lower()
        return _GER_STEMMER.stemWord(w) if len(w) > 4 else w
except Exception:  # pragma: no cover - dependency-optional path
    _SUFFIXES = ("ungen", "ung", "innen", "erin", "chen", "lein",
                 "en", "er", "es", "em", "nen", "n", "e", "s")

    def _stem(word: str) -> str:
        w = word.lower()
        if len(w) <= 4:
            return w
        for suf in _SUFFIXES:
            if w.endswith(suf) and len(w) - len(suf) >= 4:
                return w[: -len(suf)]
        return w


def _query_stems(query: str) -> list[str]:
    """Meaningful, stemmed query tokens (stopwords + 1-char tokens removed)."""
    out: list[str] = []
    for w in query.lower().split():
        w = w.strip(".,;:!?\"'()")
        if len(w) < 2 or w in _CAL_STOPWORDS:
            continue
        out.append(_stem(w))
    return out


def _match_score(event: dict, q_stems: list[str]) -> int:
    """Number of query stems that match the event (OR semantics, for ranking).

    A stem matches if it equals a stemmed haystack token OR is a substring of the
    raw haystack (catches German compounds like 'Zahnarzttermin' for 'Zahnarzt').
    """
    hay = " ".join((
        event.get("summary", ""),
        event.get("description", ""),
        event.get("location", ""),
    )).lower()
    hay_stems = {_stem(t) for t in re.findall(r"\w+", hay)}
    return sum(1 for qs in q_stems if qs in hay_stems or qs in hay)


def _filter_and_sort(
    events: list[dict],
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    max_results: int = 30,
) -> list[dict]:
    # Date-range filter (YYYY-MM-DD → YYYYMMDD for lexicographic comparison with dtstart_raw)
    if date_from:
        _df = date_from.replace("-", "")
        events = [e for e in events if e.get("dtstart_raw", "") >= _df]
    if date_to:
        _dt = date_to.replace("-", "")
        events = [e for e in events if e.get("dtstart_raw", "")[:8] <= _dt]

    # Ascending when browsing a date range (future planning), descending otherwise
    ascending = bool(date_from or date_to)

    # Text filter — OR with ranking; empty query → all date-matched events pass
    q_stems = _query_stems(query)
    if q_stems:
        scored = [(s, e) for e in events if (s := _match_score(e, q_stems)) > 0]
        # Two-pass stable sort: date first (asc/desc per context), then score desc —
        # so best-matching events lead, ties keep the desired chronological order.
        scored.sort(key=lambda se: se[1].get("dtstart_raw", ""), reverse=not ascending)
        scored.sort(key=lambda se: se[0], reverse=True)
        events = [e for _, e in scored]
    else:
        events.sort(key=lambda e: e.get("dtstart_raw", ""), reverse=not ascending)

    return events[:max_results]


# ── Mode A: iCal URL ──────────────────────────────────────────────────────────


async def search_events_ical(
    ical_url: str,
    query: str = "",
    date_from: str = "",
    date_to: str = "",
    max_results: int = 30,
) -> list[dict[str, Any]]:
    """Fetch a private/secret iCal URL and search events by text and/or date range."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                ical_url,
                timeout=aiohttp.ClientTimeout(total=30),
                allow_redirects=True,
            ) as resp:
                if resp.status != 200:
                    return []
                text = await resp.text()
        except Exception:
            return []

    events = _parse_ical_text(text)
    return _filter_and_sort(events, query, date_from, date_to, max_results)


async def test_ical_url(ical_url: str) -> str:
    """Return empty string on success, error message on failure."""
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                ical_url,
                timeout=aiohttp.ClientTimeout(total=15),
                allow_redirects=True,
            ) as resp:
                if resp.status == 200:
                    text = await resp.text()
                    if "BEGIN:VCALENDAR" in text:
                        return ""
                    return "URL reachable, but no valid calendar data found"
                return f"HTTP {resp.status}"
        except Exception as e:
            return str(e)


# ── Mode B: CalDAV ────────────────────────────────────────────────────────────


async def search_events_caldav(
    url: str,
    username: str,
    password: str,
    query: str = "",
    months_back: int = 36,
    months_forward: int = 12,
    date_from: str = "",
    date_to: str = "",
    max_results: int = 30,
) -> list[dict[str, Any]]:
    """Search CalDAV calendar events via REPORT request."""
    now = datetime.now(timezone.utc)
    if date_from:
        _df = datetime.strptime(date_from, "%Y-%m-%d")
        report_date_from = _df.strftime("%Y%m%dT000000Z")
    else:
        report_date_from = (now - timedelta(days=months_back * 30)).strftime("%Y%m%dT000000Z")
    if date_to:
        _dt = datetime.strptime(date_to, "%Y-%m-%d")
        report_date_to = _dt.strftime("%Y%m%dT235959Z")
    else:
        report_date_to = (now + timedelta(days=months_forward * 30)).strftime("%Y%m%dT235959Z")
    report_xml = _build_report_xml(report_date_from, report_date_to)
    auth = aiohttp.BasicAuth(username, password)

    async with aiohttp.ClientSession() as session:
        try:
            async with session.request(
                "REPORT",
                url,
                data=report_xml.encode("utf-8"),
                headers={
                    "Content-Type": "application/xml; charset=utf-8",
                    "Depth": "1",
                },
                auth=auth,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status not in (200, 207):
                    return []
                body = await resp.read()
        except Exception:
            return []

    all_events: list[dict] = []
    for ical_text in _extract_ical_from_multistatus(body):
        all_events.extend(_parse_ical_text(ical_text))

    return _filter_and_sort(all_events, query, date_from, date_to, max_results)


async def test_caldav_connection(url: str, username: str, password: str) -> str:
    propfind = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:propfind xmlns:D="DAV:"><D:prop><D:resourcetype/></D:prop></D:propfind>'
    )
    auth = aiohttp.BasicAuth(username, password)
    try:
        async with aiohttp.ClientSession() as session:
            async with session.request(
                "PROPFIND",
                url,
                data=propfind.encode(),
                headers={"Content-Type": "application/xml", "Depth": "0"},
                auth=auth,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status in (200, 207):
                    return ""
                return f"HTTP {resp.status}"
    except Exception as e:
        return str(e)


# ── Unified dispatcher ────────────────────────────────────────────────────────


async def search_events(
    config: dict,
    query: str = "",
    date_from: str = "",
    date_to: str = "",
) -> list[dict[str, Any]]:
    """Dispatch to the right backend based on config keys.

    iCal mode (multi):  config = {"ical_urls": ["https://...", ...]}
    iCal mode (legacy): config = {"ical_url": "https://..."}
    CalDAV mode:        config = {"url": "...", "username": "...", "password": "..."}
    """
    # Normalise to list — support both new multi-URL and legacy single-URL
    ical_urls: list[str] = config.get("ical_urls") or []
    if not ical_urls and config.get("ical_url"):
        ical_urls = [config["ical_url"]]

    if ical_urls:
        # Fetch all calendars in parallel and merge
        results_per_cal = await asyncio.gather(
            *[search_events_ical(url, query, date_from, date_to) for url in ical_urls],
            return_exceptions=True,
        )
        merged: list[dict] = []
        for r in results_per_cal:
            if isinstance(r, list):
                merged.extend(r)
        # Re-sort the merged set
        ascending = bool(date_from or date_to)
        merged.sort(key=lambda e: e.get("dtstart_raw", ""), reverse=not ascending)
        return merged

    return await search_events_caldav(
        url=config["url"],
        username=config["username"],
        password=config["password"],
        query=query,
        date_from=date_from,
        date_to=date_to,
    )
