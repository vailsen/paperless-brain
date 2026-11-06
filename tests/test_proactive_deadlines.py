"""Proactive Phase 6: doc_analysis, deadline findings, reminders, new documents."""

import asyncio
import json
import re
from datetime import date

import pytest

DOC_TEXT = (
    "Stadtwerke Musterstadt — Rechnung Nr. 4711 vom 01.10.2026\n\n"
    "Bitte überweisen Sie den Betrag von 120,00 EUR bis zum 15.11.2026 auf unser Konto.\n"
    "Der Vertrag kann mit einer Frist von einem Monat zum 31.12.2026 gekündigt werden.\n"
    "Leistungszeitraum: 01.09.2026 bis 30.09.2026.\n"
)


@pytest.fixture
def env(proactive_on, tmp_path, monkeypatch):
    """Vaults, sidecars, a fake Paperless, a fake push service, two users."""
    from config.settings import settings
    from proactive import clock, findings, prefs, users
    from proactive.tasks import documents
    from tests.conftest import fake_push

    root = tmp_path / "vaults"
    for u in ("alice", "bob"):
        (root / u).mkdir(parents=True)
    monkeypatch.setattr(settings, "vault_root", root)
    monkeypatch.setattr(settings, "app_path", tmp_path)
    monkeypatch.setattr(settings, "extraction_sidecar_path", "sidecars")
    (tmp_path / "sidecars").mkdir()
    findings._index.clear()

    # "today" = 2026-10-26 local
    now = clock.local_now()
    target = now.replace(year=2026, month=10, day=26, hour=8, minute=0)
    clock.advance(target - now)

    sent = []
    fake_push(monkeypatch, {"alice": "11", "bob": "22"}, sent)
    for user in ("alice", "bob"):
        p = prefs.get(user)
        p.optin, p.channels, p.language = True, "push", "en"
        prefs.save(p)
    monkeypatch.setattr(users, "eligible", lambda: [("alice", "t-alice"), ("bob", "t-bob")])

    access = {"t-alice": {100, 101}, "t-bob": {101}}  # bob cannot read #100

    async def can_read(tok, doc_id):
        return doc_id in access[tok]

    async def document(tok, doc_id):
        return {"id": doc_id, "title": f"Doc {doc_id}", "created": "2026-10-01"} if doc_id in access[tok] else None

    monkeypatch.setattr(documents, "can_read", can_read)
    monkeypatch.setattr(documents, "document", document)
    return {"tmp": tmp_path, "sent": sent, "root": root}


def _sidecar(env, doc_id, text=DOC_TEXT):
    (env["tmp"] / "sidecars" / f"{doc_id}.json").write_text(json.dumps({"pages": [{"page_text": text}]}))


ANSWER = {
    "summary": "Rechnung der Stadtwerke.",
    "topic_hint": "Strom",
    "deadlines": [
        {"date": "2026-11-15", "kind": "payment", "actionable": True, "description": "Pay 120 EUR",
         "quote": "Bitte überweisen Sie den Betrag von 120,00 EUR bis zum 15.11.2026"},
        {"date": "2026-12-31", "kind": "cancellation_window", "actionable": True, "description": "Cancel",
         "quote": "Der Vertrag kann mit einer Frist von einem Monat zum 31.12.2026 gekündigt werden."},
        {"date": "2026-09-30", "kind": "informational", "actionable": False, "description": "Period end",
         "quote": "Leistungszeitraum: 01.09.2026 bis 30.09.2026."},
        # past, actionable: must never remind
        {"date": "2026-10-01", "kind": "payment", "actionable": True, "description": "old",
         "quote": "Rechnung Nr. 4711 vom 01.10.2026"},
        # invented quote: dropped
        {"date": "2026-11-20", "kind": "objection", "actionable": True, "description": "x",
         "quote": "Widerspruch ist bis zum 20.11.2026 möglich."},
    ],
}


@pytest.fixture
def fake_llm(monkeypatch):
    from proactive import llm

    calls = []

    async def structured(model, user, token, **kw):
        if kw.get("tool_name") != "doc_analysis":
            return {"worth_finding": False}  # triage (Phase 7): nothing to report
        calls.append((model.key, user))
        return json.loads(json.dumps(ANSWER))

    monkeypatch.setattr(llm, "structured", structured)
    return calls


def _model():
    from proactive.models import ResolvedModel

    return ResolvedModel("qwen", "local", "default", "qwen3:32b")


def _texts(env):
    return [p["text"] for m, p in env["sent"] if m == "sendMessage"]


def _worker_run(kind, user="", payload=None):
    from proactive import jobs
    from proactive.worker import Worker

    class Open:
        woke_host = False

        async def local_ready(self, may_wake=True):
            return True

    jobs.enqueue(kind, user, payload or {})
    asyncio.run(Worker(gate=Open()).tick_once())


# ── Analysis ─────────────────────────────────────────────────────────────────


def test_validation_drops_invented_quotes_bad_dates_and_kinds():
    from proactive.analysis import validate

    raw = dict(ANSWER, deadlines=ANSWER["deadlines"] + [
        {"date": "15.11.2026", "kind": "payment", "actionable": True, "description": "", "quote": DOC_TEXT[:60]},
        {"date": "2026-11-15", "kind": "vibes", "actionable": True, "description": "", "quote": DOC_TEXT[:60]},
    ])
    summary, topic, ds = validate(raw, DOC_TEXT)
    assert [d.date for d in ds] == ["2026-11-15", "2026-12-31", "2026-09-30", "2026-10-01"]
    assert ds[2].actionable is False


def test_quote_match_tolerates_whitespace_and_typographic_quotes():
    from proactive.analysis import quote_in_text

    assert quote_in_text("Bitte  überweisen Sie den\nBetrag", DOC_TEXT)
    assert quote_in_text('Er sagte „Hallo“ dann', 'Er sagte "Hallo" dann')
    assert not quote_in_text("Bitte zahlen Sie sofort", DOC_TEXT)
    assert not quote_in_text("bis", DOC_TEXT)  # too short to prove anything


def test_analysis_runs_once_and_again_only_when_the_text_changes(env, fake_llm):
    from proactive import analysis

    _sidecar(env, 100)
    a, used = asyncio.run(analysis.ensure(100, _model(), "alice", "t-alice"))
    assert used and len(a.deadlines) == 4
    _a, used = asyncio.run(analysis.ensure(100, _model(), "alice", "t-alice"))
    assert not used and len(fake_llm) == 1
    _sidecar(env, 100, DOC_TEXT + "\nNachtrag.")
    _a, used = asyncio.run(analysis.ensure(100, _model(), "alice", "t-alice"))
    assert used and len(fake_llm) == 2


def test_no_sidecar_is_no_text(env, fake_llm):
    from proactive import analysis

    with pytest.raises(analysis.NoText):
        asyncio.run(analysis.ensure(999, _model(), "alice", "t-alice"))
    assert fake_llm == []


# ── Findings + access ────────────────────────────────────────────────────────


def test_new_document_gives_deadline_findings_only_to_readers(env, fake_llm, monkeypatch):
    from config import proactive as cfgmod
    from proactive import findings

    monkeypatch.setenv("PROACTIVE_MODEL_DEFAULT", "qwen3:32b")
    cfgmod.proactive_settings.cache_clear()
    _sidecar(env, 100)
    _worker_run("doc_new", payload={"doc_id": 100, "first_seen": "2026-10-26T07:00:00Z"})
    alice = findings.list_findings("alice")
    assert sorted(f.deadline for f in alice) == ["2026-11-15", "2026-12-31"]  # actionable, future
    assert all(f.kind == "deadline" and f.doc_ids == [100] for f in alice)
    assert findings.list_findings("bob") == []  # no read access to #100
    assert len(fake_llm) == 1
    # analysing again (e.g. after losing proactive.db) creates no duplicates
    from proactive import analysis
    from proactive.db import connect

    with connect() as conn:
        conn.execute("DELETE FROM doc_analysis")
    assert analysis.get(100) is None
    _worker_run("doc_new", payload={"doc_id": 100, "first_seen": "2026-10-26T07:00:00Z"})
    assert len(findings.list_findings("alice")) == 2


def test_new_document_waits_for_its_sidecar(env, fake_llm, monkeypatch):
    from config import proactive as cfgmod
    from proactive import jobs

    monkeypatch.setenv("PROACTIVE_MODEL_DEFAULT", "qwen3:32b")
    cfgmod.proactive_settings.cache_clear()
    _worker_run("doc_new", payload={"doc_id": 101, "first_seen": "2026-10-26T07:00:00Z"})
    (j,) = [j for j in [jobs.get(1)] if j]
    assert j.status == "queued" and j.attempts == 0 and "not ingested" in j.last_error


def test_newdoc_poller_starts_at_the_newest_id(env, monkeypatch):
    from proactive import jobs
    from proactive.tasks import documents

    async def max_id():
        return 500

    seen = {}

    async def newest(after, limit=100):
        seen["after"] = after
        return [501, 502]

    monkeypatch.setattr(documents, "max_id_admin", max_id)
    monkeypatch.setattr(documents, "newest_ids_admin", newest)
    asyncio.run(documents.newdoc_poll(jobs.Job(0, "newdoc_poll", "", {}, False, "running", 0, "")))
    assert documents._cursor() == 500 and jobs.pending_count(kind="doc_new") == 0
    asyncio.run(documents.newdoc_poll(jobs.Job(0, "newdoc_poll", "", {}, False, "running", 0, "")))
    assert seen["after"] == 500 and documents._cursor() == 502
    assert jobs.pending_count(kind="doc_new") == 2


# ── Reminders ────────────────────────────────────────────────────────────────


def _make_findings(env, fake_llm):
    from proactive import analysis
    from proactive.tasks import documents

    _sidecar(env, 100)
    a, _ = asyncio.run(analysis.ensure(100, _model(), "alice", "t-alice"))
    asyncio.run(documents.per_user("alice", "t-alice", a, {"title": "Stadtwerke"}, _model()))


def _day(env, d: date):
    from proactive import clock

    now = clock.local_now()
    clock.advance(now.replace(year=d.year, month=d.month, day=d.day) - now)


def test_reminders_fire_once_per_offset_and_never_for_past_dates(env, fake_llm):
    from proactive import findings

    _make_findings(env, fake_llm)
    pay = lambda: next(f for f in findings.list_findings("alice") if f.deadline == "2026-11-15")  # noqa: E731

    _worker_run("daily_run")                     # 2026-10-26: 20 days left → offset 30
    assert len(_texts(env)) == 1 and "20 days left" in _texts(env)[0]
    assert pay().reminders_sent == [30] and pay().status == "surfaced"
    _worker_run("daily_run")                     # same day again: nothing
    assert len(_texts(env)) == 1

    _day(env, date(2026, 11, 8))                 # 7 days left
    _worker_run("daily_run")
    assert pay().reminders_sent == [30, 7]
    # the cancellation window (31.12.) reached its 30-day offset? 53 days left — no
    assert len(_texts(env)) == 2

    _day(env, date(2026, 11, 16))                # past: no reminder, ever
    _worker_run("daily_run")
    assert pay().reminders_sent == [30, 7] and len(_texts(env)) == 2


def test_worker_down_past_several_offsets_sends_one(env, fake_llm):
    from proactive import findings

    _make_findings(env, fake_llm)
    _day(env, date(2026, 11, 14))                # 1 day left, 30 and 7 never sent
    _worker_run("daily_run")
    pay = next(f for f in findings.list_findings("alice") if f.deadline == "2026-11-15")
    assert pay.reminders_sent == [30, 7, 1]
    assert len(_texts(env)) == 1 and "Due tomorrow" in _texts(env)[0]
    assert pay.urgency == "critical"


def test_dedupe_survives_losing_the_db(env, fake_llm, proactive_on):
    """reminders_sent lives in the finding file, not in proactive.db."""
    from pathlib import Path


    _make_findings(env, fake_llm)
    _worker_run("daily_run")
    assert len(_texts(env)) == 1
    for suffix in ("", "-wal", "-shm"):
        Path(str(proactive_on) + suffix).unlink(missing_ok=True)
    from proactive import prefs

    p = prefs.get("alice")
    p.optin, p.channels, p.language = True, "push", "en"
    prefs.save(p)
    from proactive.notify import webpush        # devices re-register after a DB loss
    webpush.subscribe("alice", {"endpoint": "https://fcm.googleapis.com/fcm/send/11",
                                "keys": {"p256dh": "k", "auth": "a"}})
    _worker_run("daily_run")
    assert len(_texts(env)) == 1


def test_done_deadline_gets_no_more_reminders(env, fake_llm):
    from proactive import findings, reactions

    _make_findings(env, fake_llm)
    _worker_run("daily_run")
    pay = next(f for f in findings.list_findings("alice") if f.deadline == "2026-11-15")
    asyncio.run(reactions.react("alice", pay.finding_id, "done", channel="app"))
    _day(env, date(2026, 11, 8))
    _worker_run("daily_run")
    assert len(_texts(env)) == 1


def test_reminders_go_out_while_the_host_sleeps(env, fake_llm):
    from proactive import jobs
    from proactive.worker import Worker

    class Asleep:
        woke_host = False

        async def local_ready(self, may_wake=True):
            raise AssertionError("the reminder path must not need the LLM")

    _make_findings(env, fake_llm)
    jobs.enqueue("daily_run")
    asyncio.run(Worker(gate=Asleep()).tick_once())
    assert len(_texts(env)) == 1


def test_binding_windows_are_critical():
    from proactive.analysis import Deadline
    from proactive.deadlines import urgency

    today = date(2026, 10, 26)
    d = lambda day, kind: Deadline(day, kind, True, "", "")  # noqa: E731
    assert urgency(d("2026-10-27", "payment"), today) == "critical"
    assert urgency(d("2026-11-01", "cancellation_window"), today) == "critical"
    assert urgency(d("2026-12-31", "cancellation_window"), today) == "high"
    assert urgency(d("2026-11-15", "payment"), today) == "medium"
    assert urgency(d("2027-03-01", "renewal"), today) == "low"


# ── Dashboard source ─────────────────────────────────────────────────────────


def test_dashboard_uses_doc_analysis_for_analysed_documents(env, fake_llm):
    from proactive.deadlines import dashboard_actions

    _make_findings(env, fake_llm)
    sidecar_actions = [
        {"paperless_id": 100, "deadline": "2019-03-01", "description": "old sidecar action"},
        {"paperless_id": 200, "deadline": "2026-12-01", "description": "not analysed yet"},
        {"paperless_id": None, "deadline": "2026-11-11", "description": "manual", "manual": True},
    ]
    out = dashboard_actions(sidecar_actions, "alice")
    descs = {a["description"] for a in out}
    assert "old sidecar action" not in descs  # replaced, never both
    assert {"not analysed yet", "manual", "Pay 120 EUR", "Cancel"} <= descs
    assert "old" not in descs  # past, actionable, but nobody was reminded: not "overdue"
    assert "Period end" not in descs  # informational


def test_dashboard_unchanged_when_disabled(proactive_off):
    from proactive.deadlines import dashboard_actions

    acts = [{"paperless_id": 1, "deadline": "2019-01-01", "description": "x"}]
    assert dashboard_actions(acts, "alice") == acts
    assert not proactive_off.exists()


def test_finding_files_carry_the_quote_and_link(env, fake_llm):
    _make_findings(env, fake_llm)
    notices = list((env["root"] / "alice" / "PaperlessBrain Findings" / "Notices").glob("*.md"))
    text = next(p.read_text() for p in notices if "2026-11-15" in p.read_text())
    assert "> Bitte überweisen Sie den Betrag von 120,00 EUR bis zum 15.11.2026" in text
    assert re.search(r"documents/100/details", text)
    assert "deadline_kind: payment" in text


def test_a_relevant_deadline_comes_back_to_the_inbox_with_its_reminder(env, fake_llm):
    """"Relevant" takes it out of the inbox; the reminder puts it back."""
    import asyncio

    from proactive import findings, reactions

    _make_findings(env, fake_llm)
    pay = lambda: next(f for f in findings.list_findings("alice") if f.deadline == "2026-11-15")  # noqa: E731
    asyncio.run(reactions.react("alice", pay().finding_id, "relevant", channel="app"))
    assert pay().status == "relevant"
    _worker_run("daily_run")                     # 20 days left → offset 30
    assert len(_texts(env)) == 1 and "20 days left" in _texts(env)[0]
    assert pay().status == "surfaced" and pay().reminders_sent == [30]


# ── One deadline per sender ──────────────────────────────────────────────────


def _analyse(env, doc_id, meta):
    from proactive import analysis
    from proactive.tasks import documents

    _sidecar(env, doc_id)
    a, _ = asyncio.run(analysis.ensure(doc_id, _model(), "alice", "t-alice"))
    asyncio.run(documents.per_user("alice", "t-alice", a, meta, _model()))


def test_same_deadline_of_one_sender_is_one_finding(env, fake_llm):
    """Every yearly pension letter repeats the same date: one finding, all letters."""
    from proactive import findings

    _analyse(env, 100, {"title": "Renteninfo 2025", "correspondent": 7})
    _analyse(env, 101, {"title": "Renteninfo 2026", "correspondent": 7})
    alice = findings.list_findings("alice")
    assert sorted(f.deadline for f in alice) == ["2026-11-15", "2026-12-31"]
    assert all(f.doc_ids == [100, 101] for f in alice)
    pay = next(f for f in alice if f.deadline == "2026-11-15")
    assert "Seen in 2 documents" in pay.body
    # analysing one of them again changes nothing — the other document stays
    _analyse(env, 100, {"title": "Renteninfo 2025", "correspondent": 7})
    assert all(f.doc_ids == [100, 101] for f in findings.list_findings("alice"))


def test_another_sender_keeps_its_own_deadline(env, fake_llm):
    from proactive import findings

    _analyse(env, 100, {"title": "A", "correspondent": 7})
    _analyse(env, 101, {"title": "B", "correspondent": 8})
    assert len(findings.list_findings("alice")) == 4


def test_a_merged_deadline_sends_nothing_new_and_keeps_the_status(env, fake_llm):
    from proactive import findings, reactions

    _analyse(env, 100, {"title": "A", "correspondent": 7})
    pay = next(f for f in findings.list_findings("alice") if f.deadline == "2026-11-15")
    asyncio.run(reactions.react("alice", pay.finding_id, "done", channel="app"))
    _analyse(env, 101, {"title": "B", "correspondent": 7})
    pay = findings.get("alice", pay.finding_id)
    assert pay.status == "done" and pay.doc_ids == [100, 101]


def test_legacy_deadline_findings_are_merged_once(env, fake_llm, monkeypatch):
    from proactive import findings, series
    from proactive.paperless_ro import document as _unused  # noqa: F401
    import proactive.paperless_ro as pro

    _analyse(env, 100, {"title": "A"})          # written before: no sender, per-document key
    _analyse(env, 101, {"title": "B"})
    assert len(findings.list_findings("alice")) == 4

    async def document(tok, doc_id):
        return {"id": doc_id, "correspondent": 7}

    monkeypatch.setattr(pro, "document", document)
    assert asyncio.run(series.consolidate_deadlines("alice", "t-alice")) == 2
    alice = findings.list_findings("alice")
    assert len(alice) == 2 and all(f.doc_ids == [100, 101] for f in alice)
    assert all(f.finding_key.startswith("deadline:c7:") for f in alice)
    assert asyncio.run(series.consolidate_deadlines("alice", "t-alice")) == 0   # once
    # a later analysis with the sender joins the merged finding, no twin
    _analyse(env, 100, {"title": "A", "correspondent": 7})
    assert len(findings.list_findings("alice")) == 2


def test_a_legacy_finding_is_adopted_not_duplicated(env, fake_llm):
    from proactive import findings

    _analyse(env, 100, {"title": "A"})
    _analyse(env, 100, {"title": "A", "correspondent": 7})   # version bump, sender known now
    alice = findings.list_findings("alice")
    assert len(alice) == 2 and all(f.finding_key.startswith("deadline:c7:") for f in alice)


def test_deadline_title_says_what_to_do_not_the_file_name():
    from proactive.analysis import Deadline
    from proactive.deadlines import title_for

    d = Deadline("2026-11-18", "objection", True, "einspruch gegen den Steuerbescheid einlegen. Frist 1 Monat", "q")
    assert title_for(d, "de", "2026_10_22_Steuerbescheid_Einkommensteuer_Finanzamt") == \
        "Einspruch gegen den Steuerbescheid einlegen"
    long = Deadline("2026-11-18", "payment", True, "Bezahlen " + "sehr " * 40 + "Ende", "q")
    assert len(title_for(long, "de")) <= 92 and title_for(long, "de").endswith(" …")
    bare = Deadline("2026-11-18", "payment", True, "", "q")
    assert title_for(bare, "en", "Invoice 12") == "Payment due: Invoice 12"
