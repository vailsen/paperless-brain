"""What a tool call did — and what it therefore proves.

Every string in this file is a real return value of a tool in
`services/chat_service.py`. The classification is prose-matching, which is only
defensible as long as it is pinned here: if a tool rewords its answer, one of
these fails, instead of a run quietly reporting "no e-mails exist" because IMAP
was never configured.
"""

from __future__ import annotations

import pytest

from werkbank.v2.tools import (
    OUTCOME_EMPTY,
    OUTCOME_FAILED,
    OUTCOME_OK,
    _hits_from,
    classify,
)

# (tool, text, expected outcome, expected hits)
CASES = [
    # ── never ran ────────────────────────────────────────────────────────────
    ("search_emails", "IMAP not configured. Please store IMAP credentials under "
     "Settings (/settings).", OUTCOME_FAILED, None),
    ("search_emails", "IMAP error: [Errno -2] Name or service not known",
     OUTCOME_FAILED, None),
    ("search_calendar", "Calendar not configured. Please store an iCal URL or "
     "CalDAV credentials under Settings (/settings).", OUTCOME_FAILED, None),
    ("search_calendar", "No active user — please sign in again.", OUTCOME_FAILED, None),
    ("web_search", "Search unavailable — every engine refused this request "
     "(google: CAPTCHA; duckduckgo: too many requests). This is not evidence "
     "that nothing exists; the search itself did not run.", OUTCOME_FAILED, None),
    ("web_search", "Web search failed: ReadTimeout", OUTCOME_FAILED, None),
    ("web_fetch_page", "Could not read https://openregister.de/person/9533 — only "
     "564 characters came back, which is a banner or a paywall stub.",
     OUTCOME_FAILED, None),
    ("web_fetch_page", "No text content extractable from: https://example.com",
     OUTCOME_FAILED, None),
    ("get_document_details", "Dokument #163 not found or no access.", OUTCOME_FAILED, None),
    ("search_exact", "Suchtext zu kurz.", OUTCOME_FAILED, None),
    ("vault_search", "No notes indexed in the vault.", OUTCOME_FAILED, None),
    ("get_document_page_text", "No extracted text found for page 3 of document #163.",
     OUTCOME_FAILED, None),
    ("get_document_table", "Invalid table_index 4. Document #163 hat 2 table(s) (0–1).",
     OUTCOME_FAILED, None),
    ("search", "Tool 'search' failed: 500 Internal Server Error", OUTCOME_FAILED, None),
    ("search", "Tool 'search' was not run: its arguments were not valid JSON.",
     OUTCOME_FAILED, None),

    # ── ran, nothing there ───────────────────────────────────────────────────
    ("search_emails", "No emails found (searched folder: [Google Mail]/Alle "
     "Nachrichten). If the emails may be in another folder, use the 'folder' "
     "parameter.", OUTCOME_EMPTY, 0),
    ("search_calendar", "No calendar entries found (Marcel Milbich)", OUTCOME_EMPTY, 0),
    ("search_exact", "No documents match the criteria.", OUTCOME_EMPTY, 0),
    ("web_search", "Web search: 'x'\n\nNo results found.", OUTCOME_EMPTY, 0),
    ("search_memory", "No relevant facts found in memory.", OUTCOME_EMPTY, 0),
    ("vault_search", "No relevant notes found.", OUTCOME_EMPTY, 0),
    ("get_actions", "No actions/deadlines found matching the filter criteria.",
     OUTCOME_EMPTY, 0),

    # ── ran, found something ─────────────────────────────────────────────────
    ("search", "Found: 18 document(s)\n\n1. #347 — Arztbrief", OUTCOME_OK, 18),
    ("search_exact", "Analytical search (Text 'Milbich'): 1 document(s)\n\n"
     "1. #163 — Auflistung", OUTCOME_OK, 1),
    ("vault_search", "Vault: 5 hits\n\n• [[To-Dos]] › …", OUTCOME_OK, 5),
    ("search_memory", "Memory: 2 relevant fact(s)\n\n• [0.24] ID: 387d…", OUTCOME_OK, 2),
    ("search_calendar", "Calendar search 'Marcel Milbich' — 1 hits\n\n"
     "1. Essen bei Marcella | 20.01.2024 18:00", OUTCOME_OK, 1),
    ("web_search", "Web search: 'Marcel Milbich'\n\n1. [Marcel](https://x)\n"
     "   Ein Text\n\n2. [Milbich](https://y)\n", OUTCOME_OK, 2),
]


@pytest.mark.parametrize("tool,text,outcome,hits", CASES)
def test_a_tool_answer_is_classified_by_what_it_actually_did(tool, text, outcome, hits):
    assert classify(text) == outcome
    assert _hits_from(tool, text) == hits


def test_the_pagination_total_is_the_finding_not_the_page_size():
    """`showing 1–50 of 143` is a statement about 143 e-mails. Recording 50 would
    make the honest claim ("143 mails with this person") fail its own check."""
    text = (
        "Email search: 'Marcel Milbich' — 50 hits, showing 1–50 of 143 "
        "(folder: searched: [Google Mail]/Alle Nachrichten(143))\n"
        "IMPORTANT: emails have no document IDs.\n\n"
        "[E1] 05.03.2025 10:10 | From: \"V.Pichlmaier\"\n   Subject: Fragen\n"
    )
    assert _hits_from("search_emails", text) == 143


def test_a_broader_fallback_search_has_no_countable_hits():
    """The hits answer a different query than the one recorded on the source, so
    a count here would verify a claim about a search that never happened."""
    text = (
        "Email search: 'Milbich Rechnung' — 12 hits (folder: All)\n"
        "NOTE: no email contains 'Milbich Rechnung'. The hits below come from a "
        "broader search for 'Milbich' and may be unrelated.\n\n"
        "[E1] 05.03.2025 | From: x\n"
    )
    assert _hits_from("search_emails", text) is None


def test_a_fetched_page_never_reports_hits():
    """It used to: the bullet-counting fallback counted the list items in the
    page's own markdown and recorded one fetch as 95 hits — a number D3 accepts
    as evidence for a claim nobody counted."""
    page = "[Crawl4AI] Content of https://example.com:\n\n" + "\n".join(
        f"- Menüpunkt {i}" for i in range(95)
    )

    assert classify(page) == OUTCOME_OK
    assert _hits_from("web_fetch_page", page) is None
    assert _hits_from("get_document_page_text", "Page 1 of document #163:\n\n- a\n- b") is None


# ── what the rest of the run does with the outcome ────────────────────────────


@pytest.fixture
def reg():
    from werkbank.v2 import registry

    return registry.available_agents({"paperless", "vault", "email"})


def test_one_call_that_never_ran_is_enough_to_disqualify_a_source(reg):
    """The three-strikes rule of `dead_tools` is about a host that answers and
    answers nothing. A tool without credentials does not answer at all, and the
    single call that proves it is the whole story — waiting for three would let
    "IMAP not configured" through as "there are no e-mails"."""
    from werkbank.v2 import tools
    from werkbank.v2.models import Gap, GapReason, SubtaskResult
    from werkbank.v2.runner import _mark_dead_sources

    belt = tools.ToolBelt(registry=reg, allowed_tools=["search_emails"],
                          user_id="alice", persist=False)
    belt._record("search_emails", {"query": "Milbich"},
                 "IMAP not configured. Please store IMAP credentials under Settings.")

    assert belt.failed_tools() == ["search_emails"]
    assert belt.dead_tools() == []          # it did not answer nothing — it did not answer

    result = SubtaskResult(
        subtask_id="st1", agent="comms_researcher",
        gaps=[Gap(question="Gibt es Mails?", reason=GapReason.NOT_FOUND)],
    )
    _mark_dead_sources(result, belt)

    assert result.gaps[0].reason is GapReason.SOURCE_UNAVAILABLE
    assert "could not run at all" in result.gaps[0].note


def test_nothing_in_an_error_message_is_quotable(reg):
    """D2 checks a quote against the text the tool returned. An error message is
    such a text, so a fact quoting "IMAP error: connection refused" passed — the
    check did its job on a source that establishes nothing."""
    from werkbank.v2 import tools

    belt = tools.ToolBelt(registry=reg, allowed_tools=["search_emails", "search"],
                          user_id="alice", persist=False)
    belt._record("search_emails", {"query": "x"}, "IMAP error: connection refused")
    belt._record("search", {"query": "x"}, "Found: 1 document(s)\n\n1. #7 — Vertrag")

    assert list(belt.raw_texts()) == ["s2"]
    assert "FAILED" in belt.catalogue()
    assert "not evidence of absence" in belt.catalogue()


def test_a_failed_call_carries_no_hit_count(reg):
    """`hits` is what D3 accepts instead of arithmetic. A failed call reporting
    0 hits would make "there is nothing" checkable — and true — for a search
    that never ran."""
    from werkbank.v2 import tools

    belt = tools.ToolBelt(registry=reg, allowed_tools=["search_emails"],
                          user_id="alice", persist=False)
    belt._record("search_emails", {"query": "x"}, "IMAP error: connection refused")
    belt._record("search_emails", {"query": "y"}, "No emails found (searched folder: All).")

    assert [r.hits for r in belt.records] == [None, 0]


def test_a_call_that_did_not_run_carries_no_trust(reg):
    """`trust` comes from the tool — but only when the tool answered. A failed
    Paperless call is not an authoritative document; it is not a source at all."""
    from werkbank.v2 import tools

    belt = tools.ToolBelt(registry=reg, allowed_tools=["search", "get_document_details"],
                          user_id="alice", persist=False)
    belt._record("search", {"query": "x"}, "Found: 1 document(s)\n\n1. #7 — Vertrag")
    belt._record("get_document_details", {"document_id": 163},
                 "Dokument #163 not found or no access.")

    assert belt.trust_by_source() == {"s1": "authoritative", "s2": "model"}


def test_the_reporting_call_gets_the_retrieved_text_back(reg):
    """The two calls of a subtask used to be joined only by the model's own
    closing notes, so a fact could be no better than that paraphrase and a
    `quote` no more verbatim than its memory. Observed: five e-mail searches,
    299 hits, one fact saying "searches were carried out"."""
    from werkbank.v2 import tools
    from werkbank.v2.runner import evidence_block

    belt = tools.ToolBelt(registry=reg, allowed_tools=["search_emails", "search"],
                          user_id="alice", persist=False)
    belt._record("search_emails", {"query": "Milbich"},
                 "Email search: 'Milbich' — 2 hits\n\n[E1] 05.03.2025 | Subject: Dach")
    belt._record("search", {"query": "Milbich"}, "Tool 'search' failed: 500")

    block = evidence_block(belt)

    assert "[E1] 05.03.2025 | Subject: Dach" in block      # quotable text is there
    assert "2 hits" in block
    assert "s2: search" in block and "FAILED" in block
    assert "500" not in block                              # an error is not evidence


def test_the_evidence_block_stays_within_its_budget(reg):
    """Bounded on purpose: the whole retrieved corpus does not fit, and a
    reporting call that overflows its context returns nothing at all."""
    from werkbank.v2 import runner, tools

    belt = tools.ToolBelt(registry=reg, allowed_tools=["search"], user_id="alice",
                          persist=False)
    for i in range(40):
        belt._record("search", {"query": f"q{i}"}, "Found: 1 document(s)\n" + "x" * 50_000)

    block = runner.evidence_block(belt)

    assert len(block) <= 40 * (runner.MIN_SOURCE_EXCERPT + 200)
    assert "(excerpt)" in block


def test_a_mailbox_survey_does_not_pull_hundreds_of_full_bodies(reg):
    """One `detail=full` search over 50 messages took four and a half minutes.
    The cap is visible to the model, not silent: it is told what to do instead."""
    from werkbank.v2.tools import MAX_FULL_EMAILS, _shape_args

    args, note = _shape_args("search_emails",
                             {"query": "Milbich", "detail": "full", "max_results": 200})

    assert args["max_results"] == MAX_FULL_EMAILS
    assert args["query"] == "Milbich"
    assert "detail='headers'" in note

    # Headers are the cheap path and stay uncapped, as does a small full fetch.
    assert _shape_args("search_emails",
                       {"query": "x", "detail": "headers", "max_results": 200}) == (
        {"query": "x", "detail": "headers", "max_results": 200}, "")
    assert _shape_args("search_emails", {"query": "x", "detail": "full"})[1] == ""
    assert _shape_args("search", {"query": "x", "max_results": 200})[1] == ""
