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
