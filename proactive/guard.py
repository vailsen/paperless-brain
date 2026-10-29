"""Web query guard (architecture §10) — code, not a prompt instruction.

Every web query a proactive job sends passes `check()`. A query that carries
something personal — an IBAN, an email address, a long number, an amount, a
full date, the user's own name, a contract or policy number — is rejected
with a tool error telling the model to rephrase generically, and logged. The
system prompt also asks for generic queries ("category + product + year");
this is the part that holds when the model does not listen.
"""

from __future__ import annotations

import logging
import re

log = logging.getLogger("proactive.guard")

REJECTION = "query rejected: contains personal identifiers ({what}) — rephrase generically (category + product + year)"

_MONTHS = (
    "jan|feb|mär|maer|mar|apr|mai|may|jun|jul|aug|sep|okt|oct|nov|dez|dec|"
    "januar|februar|märz|maerz|april|juni|juli|august|september|oktober|november|dezember|"
    "january|february|march|june|july|october|december"
)

PATTERNS: list[tuple[str, re.Pattern]] = [
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){2,7}(?:\s?[A-Z0-9]{1,4})?\b", re.I)),
    ("email address", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    # six or more digits, also when grouped by spaces, dots, dashes or slashes
    ("long number", re.compile(r"(?<![\d])\d(?:[ .\-/]?\d){5,}(?![\d])")),
    ("amount", re.compile(
        r"(?:[€$£]|\b(?:eur|euro|usd|chf|gbp)\b)\s?\d|\d(?:[\d.,']*\d)?\s?(?:[€$£]|\b(?:eur|euro|euros|usd|chf|gbp|dollars?)\b)",
        re.I,
    )),
    ("date", re.compile(
        rf"\b\d{{1,2}}[./]\d{{1,2}}[./](?:\d{{4}}|\d{{2}})\b|\b\d{{4}}-\d{{1,2}}-\d{{1,2}}\b"
        rf"|\b\d{{1,2}}\.?\s+(?:{_MONTHS})\.?\s+\d{{4}}\b|\b(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}\b",
        re.I,
    )),
    # "Nr. 4711", "Vertragsnummer: AB-12", "Az. 3 K 12/24": the word says it is an identifier
    ("contract or policy number", re.compile(
        r"\b(?:nr|no|nummer|vertragsnummer|vertrags-nr|policennummer|versicherungsnummer|"
        r"vers\.?-?nr|kundennummer|kunden-nr|mandatsreferenz|aktenzeichen|az|rechnungsnummer|"
        r"rechnungs-nr|ref)\.?\s*[:#]?\s*[A-Z]*[-/]?\d[A-Z0-9\-/ ]{1,}",
        re.I,
    )),
    # "policy 2026" is a year; "policy number LV-88213" is not
    ("contract or policy number", re.compile(
        r"\b(?:policy|contract|customer|account|reference|invoice|police)\s*"
        r"(?:no\.?|nr\.?|number|nummer|#)\s*[:#]?\s*[A-Z0-9][A-Z0-9\-/]{2,}",
        re.I,
    )),
]


def check(query: str, names: list[str] | None = None) -> str | None:
    """None if the query may go out, else the rejection text for the model."""
    q = query or ""
    found = [what for what, pat in PATTERNS if pat.search(q)]
    for name in names or []:
        name = (name or "").strip()
        if len(name) >= 2 and re.search(rf"(?<!\w){re.escape(name)}(?!\w)", q, re.I):
            found.append("the user's name")
            break
    if not found:
        return None
    log.warning("web query rejected (%s)", ", ".join(found))  # never log the query itself
    return REJECTION.format(what=", ".join(found))


def user_names(info: dict) -> list[str]:
    """First name, last name and username from Paperless' user record."""
    out = []
    for key in ("first_name", "last_name", "username"):
        for part in str(info.get(key) or "").replace("-", " ").split():
            if len(part) >= 2 and part.lower() not in out:
                out.append(part.lower())
    return out
