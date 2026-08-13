"""Regression tests for the IMAP search path.

The bug these pin down: `imaplib` encodes str command arguments as ASCII, so
every query containing an umlaut raised `UnicodeEncodeError` inside the library
and the surrounding `except` turned it into "no emails found".
"""

import email
import imaplib

import pytest

from services.imap_service import (
    _body_snippet,
    _build_imap_criteria,
    _criteria_parts,
    _fallback_variants,
    _fetch_messages,
    _fold_ascii,
    _html_to_text,
    _literal_split,
    _rank_by_term,
    _search,
    _shorten_term,
    _shortened_inputs,
    folder_display,
    folder_to_wire,
    imap_utf7_decode,
    imap_utf7_encode,
)


class FakeIMAP:
    """Records what `search` was called with, mimicking imaplib's ASCII rule."""

    def __init__(
        self,
        hits: dict | None = None,
        reject_charset: bool = False,
        reject_literal: bool = False,
    ):
        self.calls: list[tuple] = []
        self.hits = hits or {}
        self.reject_charset = reject_charset
        self.reject_literal = reject_literal
        self.literal = None

    def search(self, charset, criteria):
        # imaplib encodes str args as ASCII and raises on anything else.
        if isinstance(criteria, str):
            criteria.encode("ascii")
            wire = criteria
        else:
            wire = criteria.decode("utf-8")
        # A pending literal is appended to the command line, then consumed.
        literal, self.literal = self.literal, None
        if literal is not None:
            if self.reject_literal:
                return "BAD", [b""]
            wire = f'{wire} "{literal.decode("utf-8")}"'
        self.calls.append((charset, wire))
        if charset and self.reject_charset:
            raise imaplib.IMAP4.error("BAD [CANNOT] Unsupported charset")
        ids = self.hits.get(wire, b"")
        return "OK", [ids]


# ── Charset handling ─────────────────────────────────────────────────────────


def test_umlaut_query_reaches_the_server():
    """The regression: this used to raise inside imaplib and return nothing."""
    conn = FakeIMAP({'TEXT "Drehmomentschlüssel"': b"12 34"})
    assert _search(conn, 'TEXT "Drehmomentschlüssel"') == [b"12", b"34"]
    charset, wire = conn.calls[0]
    assert charset == "UTF-8"
    assert "Drehmomentschlüssel" in wire


def test_ascii_query_sends_no_charset():
    conn = FakeIMAP({'TEXT "invoice"': b"7"})
    assert _search(conn, 'TEXT "invoice"') == [b"7"]
    assert conn.calls == [(None, 'TEXT "invoice"')]


def test_charset_rejection_falls_back_to_folded_ascii():
    conn = FakeIMAP({'TEXT "Drehmomentschlussel"': b"9"}, reject_charset=True)
    assert _search(conn, 'TEXT "Drehmomentschlüssel"') == [b"9"]
    assert [c[0] for c in conn.calls] == ["UTF-8", None]


def test_empty_result_is_not_an_error():
    conn = FakeIMAP()
    assert _search(conn, 'TEXT "nothing"') == []


# ── Literals: the form Gmail actually matches on ─────────────────────────────


def test_non_ascii_term_is_sent_as_a_literal():
    """Quoted 8-bit gets an OK and zero hits from Gmail — it never decodes it."""
    conn = FakeIMAP({'TEXT "Drehmomentschlüssel"': b"3 7"})
    hits = _search(
        conn, 'TEXT "Drehmomentschlüssel"',
        literal_split=("TEXT", "Drehmomentschlüssel"),
    )
    assert hits == [b"3", b"7"]
    charset, wire = conn.calls[0]
    assert charset == "UTF-8"
    assert wire == 'TEXT "Drehmomentschlüssel"'


def test_literal_is_cleared_after_use():
    """A leftover literal would be appended to the next command."""
    conn = FakeIMAP()
    _search(conn, 'TEXT "ü"', literal_split=("TEXT", "ü"))
    assert conn.literal is None


def test_literal_rejection_falls_back_to_bytes():
    conn = FakeIMAP({'TEXT "Drehmomentschlüssel"': b"9"}, reject_literal=True)
    assert _search(
        conn, 'TEXT "Drehmomentschlüssel"',
        literal_split=("TEXT", "Drehmomentschlüssel"),
    ) == [b"9"]


def test_empty_literal_result_is_not_retried():
    """The server searched and found nothing — trying again cannot change that."""
    conn = FakeIMAP()
    assert _search(conn, 'TEXT "ü"', literal_split=("TEXT", "ü")) == []
    assert len(conn.calls) == 1


def test_gmail_raw_query_uses_a_literal_too():
    conn = FakeIMAP({'X-GM-RAW "Drehmomentschlüssel"': b"42"})
    hits = _search(
        conn, 'X-GM-RAW "Drehmomentschlüssel"',
        literal_split=("X-GM-RAW", "Drehmomentschlüssel"),
    )
    assert hits == [b"42"]


def test_literal_split_moves_the_non_ascii_value_last():
    parts = _criteria_parts({"query": "Drehmomentschlüssel", "from_addr": "amazon.de"})
    assert _literal_split(parts) == ('FROM "amazon.de" TEXT', "Drehmomentschlüssel")


def test_literal_split_declines_when_two_values_are_non_ascii():
    """Only one literal fits on a command line, and it has to be last."""
    parts = _criteria_parts({"query": "Schlüssel", "subject": "Grüße"})
    assert _literal_split(parts) is None


def test_literal_split_declines_for_pure_ascii():
    assert _literal_split(_criteria_parts({"query": "invoice"})) is None


def test_criteria_string_is_unchanged_by_the_split_refactor():
    assert _build_imap_criteria(
        {"query": "invoice", "from_addr": "amazon.de", "unseen_only": True}
    ) == 'TEXT "invoice" FROM "amazon.de" UNSEEN'


def test_multi_word_query_becomes_anded_text_keys():
    """One TEXT key for the whole string is a substring match: it only found
    mails with those words adjacent. Juxtaposed keys are ANDed by IMAP."""
    assert _build_imap_criteria({"query": "torque wrench"}) == (
        'TEXT "torque" TEXT "wrench"'
    )


def test_or_query_still_renders_as_a_tree():
    crit = _build_imap_criteria({"query": "Rechnung OR Invoice"})
    assert crit == 'OR TEXT "Rechnung" TEXT "Invoice"'


def test_or_query_is_never_split_into_a_literal():
    """An OR tree holds several values; a literal can only carry the last one."""
    assert _literal_split(_criteria_parts({"query": "Schlüssel OR Grüße"})) is None


def test_fold_ascii_strips_diacritics():
    assert _fold_ascii("Drehmomentschlüssel") == "Drehmomentschlussel"
    assert _fold_ascii("Rechnung Café") == "Rechnung Cafe"


# ── Prefix fallback ──────────────────────────────────────────────────────────


def test_shorten_term_cuts_long_compounds():
    assert _shorten_term("Drehmomentschlüssel") == "Drehmoment"


def test_shorten_term_leaves_short_terms_alone():
    assert _shorten_term("Rechnung") == ""
    assert _shorten_term("neue Mail") == ""


def test_shorten_term_keeps_the_other_words():
    """Dropping them turns a specific question into a one-word fishing trip."""
    assert _shorten_term("Drehmomentschlüssel torque wrench") == (
        "Drehmoment torque wrench"
    )


def test_shorten_term_can_reduce_to_the_longest_word():
    assert _shorten_term("die Handwerkerrechnung von Mai", keep_all_words=False) == (
        "Handwerker"
    )


def test_shortened_inputs_returns_none_when_nothing_to_shorten():
    assert _shortened_inputs({"query": "Rechnung"}) is None


def test_shortened_inputs_keeps_other_fields():
    variant = _shortened_inputs({"query": "Drehmomentschlüssel", "from_addr": "amazon.de"})
    assert variant == {"query": "Drehmoment", "from_addr": "amazon.de"}


def test_fallback_variants_go_narrow_before_wide():
    """The single-word stage is where the noise lives — it must come last."""
    variants = _fallback_variants({"query": "Drehmomentschlüssel torque wrench"})
    assert [v["query"] for v in variants] == [
        "Drehmoment torque wrench",
        "Drehmoment",
    ]


def test_fallback_variants_collapse_to_one_for_a_single_word():
    assert [v["query"] for v in _fallback_variants({"query": "Drehmomentschlüssel"})] == [
        "Drehmoment"
    ]


def test_fallback_variants_empty_when_nothing_can_be_shortened():
    assert _fallback_variants({"query": "Rechnung Mai"}) == []


def test_rank_by_term_puts_full_matches_first():
    results = [
        {"subject": "Drehmomentmessung", "snippet": ""},
        {"subject": "Bestellt: VANPO", "snippet": "… Drehmomentschlüssel 3/8 …"},
    ]
    ranked = _rank_by_term(results, "Drehmomentschlüssel")
    assert ranked[0]["subject"] == "Bestellt: VANPO"


def test_rank_by_term_ignores_diacritics():
    results = [
        {"subject": "other", "snippet": ""},
        {"subject": "Drehmomentschlussel gekauft", "snippet": ""},
    ]
    ranked = _rank_by_term(results, "Drehmomentschlüssel")
    assert ranked[0]["subject"] == "Drehmomentschlussel gekauft"


def test_rank_by_term_scores_multi_word_queries_per_word():
    """The whole phrase is in no mail — that is why the fallback ran. Scoring
    the phrase alone is a constant and sorts nothing."""
    results = [
        {"subject": "Prozessrating", "snippet": "Drehmoment beim Diesel"},
        {"subject": "Bestellt: VANPO", "snippet": "Drehmomentschlüssel torque wrench"},
        {"subject": "Studienarbeit", "snippet": "Drehmoment der Maschine"},
    ]
    ranked = _rank_by_term(results, "Drehmomentschlüssel torque wrench")
    assert ranked[0]["subject"] == "Bestellt: VANPO"


def test_rank_by_term_ignores_short_words():
    """'der'/'and' appear everywhere and would drown the real terms."""
    results = [
        {"subject": "der und die", "snippet": ""},
        {"subject": "Drehmomentschlüssel", "snippet": ""},
    ]
    ranked = _rank_by_term(results, "der Drehmomentschlüssel")
    assert ranked[0]["subject"] == "Drehmomentschlüssel"


def test_rank_by_term_keeps_date_order_within_a_tie():
    results = [{"subject": "a", "snippet": ""}, {"subject": "b", "snippet": ""}]
    assert _rank_by_term(results, "nothing matches") == results


# ── Result ordering ──────────────────────────────────────────────────────────


class FetchIMAP:
    """Serves one canned message per id, dated by id number."""

    def __init__(self):
        self.fetched: list[bytes] = []

    def fetch(self, eid, _cmd):
        self.fetched.append(eid)
        n = int(eid)
        raw = (
            f"Date: Mon, {n:02d} Jan 2020 10:00:00 +0100\r\n"
            f"From: a@b.test\r\nSubject: mail {n}\r\n\r\nbody"
        ).encode()
        return "OK", [(b"1 (RFC822 {1})", raw)]


def test_preserve_order_keeps_the_callers_page_order():
    """The fast paths hand over a page that is already newest-first.

    Reversing it again turned "the newest five" into "the oldest five" — which
    is how a search for a 2024 purchase answered with mail from 2011.
    """
    conn = FetchIMAP()
    page = [b"9", b"8", b"7"]          # newest first, as the caller ordered it
    results = _fetch_messages(conn, page, 3, detail="headers", preserve_order=True)
    assert [r["subject"] for r in results] == ["mail 9", "mail 8", "mail 7"]


def test_without_preserve_order_an_ascending_id_list_is_reversed():
    """Raw SEARCH output is ascending, so newest-last — that one must flip."""
    conn = FetchIMAP()
    results = _fetch_messages(conn, [b"7", b"8", b"9"], 3, detail="headers")
    assert [r["subject"] for r in results] == ["mail 9", "mail 8", "mail 7"]


# ── Modified UTF-7 folder names ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "plain,wire",
    [
        ("Bestellvorgänge und Rechnungen", "Bestellvorg&AOQ-nge und Rechnungen"),
        ("INBOX", "INBOX"),
        ("Gelöscht", "Gel&APY-scht"),
        ("R&D", "R&-D"),
        ("[Gmail]/Alle Nachrichten", "[Gmail]/Alle Nachrichten"),
    ],
)
def test_utf7_roundtrip(plain, wire):
    assert imap_utf7_encode(plain) == wire
    assert imap_utf7_decode(wire) == plain


def test_utf7_decode_leaves_malformed_input_alone():
    assert imap_utf7_decode("Bestell&vorgang") == "Bestell&vorgang"


def test_folder_display_strips_quotes_and_decodes():
    assert folder_display('"Bestellvorg&AOQ-nge"') == "Bestellvorgänge"


def test_folder_to_wire_accepts_plain_text_from_the_agent():
    assert folder_to_wire("Bestellvorgänge") == '"Bestellvorg&AOQ-nge"'


def test_folder_to_wire_is_idempotent_on_encoded_input():
    """The agent may echo back what list_folders_only printed — either works."""
    assert folder_to_wire("Bestellvorg&AOQ-nge") == '"Bestellvorg&AOQ-nge"'


# ── Body extraction ──────────────────────────────────────────────────────────


def _mail(payload: str, ctype: str = "text/html", encoding: str = "quoted-printable"):
    raw = (
        f"Subject: Test\r\n"
        f"Content-Type: {ctype}; charset=utf-8\r\n"
        f"Content-Transfer-Encoding: {encoding}\r\n\r\n"
        f"{payload}"
    )
    return email.message_from_bytes(raw.encode("utf-8"))


def test_html_only_mail_yields_a_snippet():
    msg = _mail("<html><body><p>Bestellung best=C3=A4tigt</p></body></html>")
    assert "Bestellung bestätigt" in _body_snippet(msg)


def test_alt_attribute_survives_extraction():
    """Where a shop mail still spells out a title the subject line truncated."""
    html = '<img src="x.jpg" alt="VANPO Drehmomentschl=C3=BCssel 3/8 Zoll">'
    assert "Drehmomentschlüssel" in _body_snippet(_mail(html))


def test_link_slug_survives_extraction():
    html = '<a href="https://amazon.de/Drehmomentschluessel-VANPO/dp/B01">Ansehen</a>'
    assert "Drehmomentschluessel" in _body_snippet(_mail(html))


def test_percent_encoded_url_is_decoded():
    html = '<a href="https://x.de/Gr%C3%BCne-Rechnung">Link</a>'
    assert "Grüne" in _body_snippet(_mail(html))


def test_script_and_style_are_dropped():
    text = _html_to_text("<style>.a{color:red}</style><script>var x=1;</script><p>Hallo</p>")
    assert "color" not in text and "var x" not in text
    assert "Hallo" in text


def test_quoted_printable_is_decoded_not_matched_raw():
    msg = _mail("Drehmomentschl=C3=BCssel", ctype="text/plain")
    snippet = _body_snippet(msg)
    assert "Drehmomentschlüssel" in snippet
    assert "=C3=BC" not in snippet
