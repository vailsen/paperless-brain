"""Findings that belong together are one finding: twelve Vodafone invoices
announcing the same price change are one series, not twelve messages."""

import asyncio
import json

import pytest

from tests import test_proactive_triage as _triage_tests
from tests.test_proactive_triage import _model, _run, _tg

# The triage tests' fixtures: a vault, sidecars, a push device, a fake model.
env = _triage_tests.env
triage_llm = _triage_tests.triage_llm

INVOICE = (
    "Vodafone Rechnung {month}\n"
    "Ab dem nächsten Monat erhöht sich Ihr Basispreis um 5 € auf 44,99 €.\n"
)
VODAFONE, STADTWERKE = 17, 23


@pytest.fixture
def series_env(env, triage_llm, monkeypatch):
    from proactive import prefs

    p = prefs.get("alice")
    p.quiet_start = p.quiet_end = "00:00"  # no quiet hours in these tests
    prefs.save(p)
    for doc, month in ((101, "Juli"), (102, "August"), (103, "September"), (201, "Oktober")):
        (env["tmp"] / "sidecars" / f"{doc}.json").write_text(
            json.dumps({"pages": [{"page_text": INVOICE.format(month=month)}]}))
    triage_llm["triage"] = {
        "worth_finding": True, "topic": "Mobilfunk", "urgency": "medium",
        "title": "Basispreisanpassung um 5 €", "summary": "Der Basispreis steigt um 5 €.",
        "needs_web": False, "web_query": "", "dossier_update": False,
        "evidence_quotes": ["Ab dem nächsten Monat erhöht sich Ihr Basispreis um 5 € auf 44,99 €."],
    }
    return env


def _triage(doc_id, correspondent):
    from proactive import triage
    from proactive.analysis import Analysis

    a = Analysis(doc_id=doc_id, version=1, summary="Rechnung", topic_hint="Mobilfunk")
    asyncio.run(triage.triage("alice", "t", a, {"title": f"Rechnung {doc_id}", "correspondent": correspondent}, _model()))


def _insights():
    from proactive import findings

    return [f for f in findings.list_findings("alice") if f.kind == "insight"]


def test_invoices_of_one_sender_become_one_finding_sent_once(series_env):
    for doc in (101, 102, 103):
        _triage(doc, VODAFONE)
        _run("ledger_scan")
    (f,) = _insights()
    assert f.doc_ids == [101, 102, 103] and f.correspondent == VODAFONE
    assert f.finding_key == f"insight:mobilfunk:c{VODAFONE}"
    assert "Seen in 3 documents: #101, #102, #103" in f.body
    assert f.body.count("Basispreis um 5 €") == 1  # the same quote once, not three times
    assert len([t for t in _tg(series_env) if "Basispreisanpassung" in t]) == 1  # one message


def test_another_sender_is_another_finding(series_env):
    _triage(101, VODAFONE)
    _triage(201, STADTWERKE)
    assert sorted(f.correspondent for f in _insights()) == [VODAFONE, STADTWERKE]


def test_without_a_sender_each_document_stays_its_own(series_env):
    _triage(101, None)
    _triage(102, None)
    assert len(_insights()) == 2


def test_a_dismissed_series_stays_dismissed_unless_it_turns_critical(series_env, triage_llm):
    from proactive import reactions

    _triage(101, VODAFONE)
    (f,) = _insights()
    asyncio.run(reactions.react("alice", f.finding_id, "dismiss", channel="app"))
    _triage(102, VODAFONE)
    (f,) = _insights()
    assert f.status == "dismissed" and f.doc_ids == [101, 102]  # attached, silently
    triage_llm["triage"]["urgency"] = "critical"
    _triage(103, VODAFONE)
    (f,) = _insights()
    assert f.status == "new" and f.urgency == "critical"


def test_same_document_again_changes_nothing(series_env):
    _triage(101, VODAFONE)
    before = _insights()[0].sha
    _triage(101, VODAFONE)
    assert _insights()[0].sha == before


def test_triage_is_told_about_the_senders_earlier_findings(series_env, triage_llm, monkeypatch):
    from proactive import llm

    _triage(101, VODAFONE)
    seen = []
    real = llm.structured

    async def spy(model, user, token, **kw):
        seen.append(kw["messages"][0]["content"])
        return await real(model, user, token, **kw)

    monkeypatch.setattr(llm, "structured", spy)
    _triage(102, VODAFONE)
    assert "Already reported for this sender" in seen[0] and "Basispreisanpassung" in seen[0]


# ── Merging what was split before ────────────────────────────────────────────


def _legacy(env, fid, doc, *, title="Basispreisanpassung um 5 €", topic="mobilfunk",
            status="surfaced", created="2026-10-27T03:00:00"):
    d = env["root"] / "alice" / "PaperlessBrain Findings" / "Notices"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{fid}.md").write_text(
        f"---\nfinding_id: {fid}\nfinding_key: insight:{topic}:{doc}\nkind: insight\ntitle: {title}\n"
        f"status: {status}\ntopic: {topic}\nurgency: medium\ndoc_ids: [{doc}]\ncreated: '{created}'\n---\n"
        f"## Summary\n\nDer Basispreis steigt.\n\n## Evidence\n\n> Basispreis um 5 € (#{doc})\n"
        f"> — [#{doc}](http://x/documents/{doc}/details)\n"
    )


def test_split_series_are_merged_once(series_env, monkeypatch):
    from proactive import paperless_ro, series

    senders = {101: VODAFONE, 102: VODAFONE, 103: VODAFONE, 104: VODAFONE, 201: STADTWERKE}

    async def document(tok, doc_id):
        return {"id": doc_id, "correspondent": senders[doc_id]}

    monkeypatch.setattr(paperless_ro, "document", document)
    _legacy(series_env, "aaaaaaa", 101, created="2026-10-27T01:00:00")
    _legacy(series_env, "bbbbbbb", 102, status="interested")
    _legacy(series_env, "ccccccc", 103, status="dismissed")
    _legacy(series_env, "ddddddd", 104, topic="vertrag")         # other topic, same title + sender
    _legacy(series_env, "eeeeeee", 201, title="Abschlag steigt", topic="strom")
    assert asyncio.run(series.consolidate("alice", "t")) == 3
    by_sender = {f.correspondent: f for f in _insights()}
    assert len(_insights()) == 2
    v = by_sender[VODAFONE]
    assert v.finding_id == "aaaaaaa"                       # the oldest stays
    assert v.doc_ids == [101, 102, 103, 104]
    assert v.status == "interested"                        # the most engaged status
    assert v.finding_key == f"insight:mobilfunk:c{VODAFONE}"
    assert by_sender[STADTWERKE].finding_key == f"insight:strom:c{STADTWERKE}"
    assert asyncio.run(series.consolidate("alice", "t")) == 0  # once
    # the next invoice joins the merged finding
    _triage(103, VODAFONE)
    assert len(_insights()) == 2


def test_paperless_down_postpones_the_merge_and_never_blocks_sending(series_env, monkeypatch):
    from proactive import paperless_ro, series
    from proactive.db import kv_get

    async def down(tok, doc_id):
        raise ConnectionError("paperless down")

    monkeypatch.setattr(paperless_ro, "document", down)
    _legacy(series_env, "aaaaaaa", 101, status="new")
    _run("ledger_scan")
    assert _insights()[0].status == "surfaced"            # sent anyway
    assert kv_get(series.MARKER.format(user="alice")) == ""  # merge tried again later
