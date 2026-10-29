"""Proactive Phase 7: guard, triage, advice validator, scoring, weekly limit,
digest, deep dive, internal API signing."""

import asyncio
import json
import logging
import re
import time

import pytest

from proactive import guard

DOC = (
    "Allianz Lebensversicherung — Riester-Rentenvertrag\n"
    "Die Abschluss- und Vertriebskosten betragen 2,5 % der Beitragssumme.\n"
    "Die Verwaltungskosten betragen jährlich 1,2 % des Guthabens.\n"
)


# ── Guard ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "query,what",
    [
        ("DE89 3704 0044 0532 0130 00 Kosten", "IBAN"),
        ("Policennummer LV-88213 kündigen", "contract or policy number"),
        ("policy number 88213-A Riester", "contract or policy number"),
        ("Riester 1.234,56 € zu teuer", "amount"),
        ("Tarif 89 EUR monatlich", "amount"),
        ("Kündigung zum 31.12.2026", "date"),
        ("alice.example@mail.de Riester", "email address"),
        ("Steuer-ID 12345678901", "long number"),
        ("Pichlmaier Riester Erfahrungen", "the user's name"),
    ],
)
def test_guard_blocks_personal_identifiers(query, what, caplog):
    with caplog.at_level(logging.WARNING, logger="proactive.guard"):
        msg = guard.check(query, ["valentin", "pichlmaier"])
    assert msg and what in msg and "rephrase generically" in msg
    assert all(query not in r.message for r in caplog.records)  # the query itself is never logged


@pytest.mark.parametrize(
    "query",
    ["Riester Kosten Kritik 2026", "Kfz-Versicherung Wechsel November 2026", "ETF Sparplan Kosten 0,2 Prozent",
     "Riester policy 2026 Kritik", "Bausparvertrag Zinsen 2026"],
)
def test_guard_allows_generic_queries(query):
    assert guard.check(query, ["valentin", "pichlmaier"]) is None


def test_guarded_search_never_sends_a_rejected_query(monkeypatch):
    from proactive import web

    sent = []

    async def searx(q, **kw):
        sent.append(q)
        return []

    monkeypatch.setattr(web, "_searx", searx)
    run = web.WebRun(names=["alice"])
    with pytest.raises(web.QueryRejected):
        asyncio.run(run.search("Alice Riester Vertrag Nr. 4711"))
    asyncio.run(run.search("Riester Kosten Kritik 2026"))
    assert sent == ["Riester Kosten Kritik 2026"]


def test_user_names_from_paperless():
    assert guard.user_names({"first_name": "Anna-Lena", "last_name": "Meyer", "username": "alm"}) == [
        "anna", "lena", "meyer", "alm"]


# ── Advice validator ─────────────────────────────────────────────────────────


def test_advice_needs_a_verbatim_quote_and_a_fetched_url():
    from proactive.scoring import validate_advice

    ev = [{"doc_id": 1, "quote": "Die Verwaltungskosten betragen jährlich 1,2 % des Guthabens."},
          {"doc_id": 1, "quote": "Die Kosten sind niedrig."}]
    rec, e, s = validate_advice("Wechseln.", ev, ["https://a.org", "https://remembered.org"],
                                doc_texts={1: DOC}, fetched={"https://a.org": "2026-10-26"})
    assert rec == "Wechseln." and len(e) == 1
    assert s == [{"url": "https://a.org", "retrieved": "2026-10-26"}]
    rec, _e, s = validate_advice("Wechseln.", ev, ["https://remembered.org"], doc_texts={1: DOC}, fetched={})
    assert rec == "" and s == []


# ── Environment for job tests ────────────────────────────────────────────────


@pytest.fixture
def env(proactive_on, tmp_path, monkeypatch):
    from config import proactive as cfgmod
    from config.settings import settings
    from proactive import findings, linking, prefs, users
    from tests.conftest import fake_push

    monkeypatch.setenv("SMTP_HOST", "smtp")
    monkeypatch.setenv("SMTP_FROM", "brain@example.com")
    cfgmod.proactive_settings.cache_clear()
    root = tmp_path / "vaults"
    (root / "alice").mkdir(parents=True)
    monkeypatch.setattr(settings, "vault_root", root)
    monkeypatch.setattr(settings, "app_path", tmp_path)
    monkeypatch.setattr(settings, "extraction_sidecar_path", "sidecars")
    (tmp_path / "sidecars").mkdir()
    (tmp_path / "sidecars" / "1.json").write_text(json.dumps({"pages": [{"page_text": DOC}]}))
    findings._index.clear()
    sent = []
    fake_push(monkeypatch, {"alice": "11"}, sent)

    import smtplib

    class FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, **k):
            pass

        def send_message(self, msg):
            sent.append(("mail", {"text": str(msg)}))

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    p = prefs.get("alice")
    p.optin, p.channels, p.language = True, "push,email", "en"
    prefs.save(p)
    linking.link("alice", "email", "alice@example.org")
    monkeypatch.setattr(users, "eligible", lambda: [("alice", "t-alice")])
    monkeypatch.setattr(users, "token", lambda u: "t-alice")
    return {"sent": sent, "root": root, "tmp": tmp_path}


def _tg(env):
    return [p["text"] for m, p in env["sent"] if m == "sendMessage"]


def _run(kind, user="", payload=None):
    import proactive.tasks  # noqa: F401 — registers the handlers
    from proactive import jobs
    from proactive.worker import Worker

    class Open:
        woke_host = False

        async def local_ready(self, may_wake=True):
            return True

    jobs.enqueue(kind, user, payload or {})
    asyncio.run(Worker(gate=Open()).tick_once())


def _insight(env, n, *, topic="riester", urgency="high", status="new"):
    from proactive.paths import findings_root

    d = findings_root("alice") / "Notices"
    d.mkdir(parents=True, exist_ok=True)
    fid = f"insigh{'abcdefghijklmnop'[n]}"
    (d / f"{fid}.md").write_text(
        f"---\nfinding_id: {fid}\nkind: insight\ntitle: Insight {n}\nstatus: {status}\n"
        f"topic: {topic}\nurgency: {urgency}\ndoc_ids: [1]\n---\n## Summary\n\nx\n"
    )
    return fid


def _status(fid):
    from proactive import findings

    return findings.get("alice", fid).status


# ── Scoring, digest ──────────────────────────────────────────────────────────


def test_no_weekly_limit_every_push_worthy_insight_is_sent_once_per_channel(env):
    from proactive import pushes

    ids = [_insight(env, i) for i in range(6)]
    _run("ledger_scan")
    assert [_status(i) for i in ids] == ["surfaced"] * 6
    assert len(_tg(env)) == 6 and len([m for m, _ in env["sent"] if m == "mail"]) == 6
    assert pushes.last_push("alice") is not None


def test_every_due_deadline_reminder_arrives(env):
    from datetime import date

    from proactive import clock
    from proactive.paths import findings_root

    today = clock.local_now().date()
    due = date.fromordinal(today.toordinal() + 1).isoformat()
    week = date.fromordinal(today.toordinal() + 7).isoformat()
    d = findings_root("alice") / "Notices"
    d.mkdir(parents=True, exist_ok=True)
    (d / "dl1.md").write_text(f"---\nfinding_id: deadlin\nkind: deadline\ntitle: Pay\nstatus: new\nurgency: medium\n"
                              f"deadline: {due}\ndeadline_kind: payment\ndoc_ids: [1]\n---\nx\n")
    (d / "dl2.md").write_text(f"---\nfinding_id: deadlio\nkind: deadline\ntitle: Pay later\nstatus: new\nurgency: low\n"
                              f"deadline: {week}\ndeadline_kind: payment\ndoc_ids: [1]\n---\nx\n")
    _run("daily_run")
    texts = _tg(env)
    assert len(texts) == 2
    assert any("Due tomorrow" in t for t in texts) and any("7 days left" in t for t in texts)


def test_never_mutes_and_a_later_finding_on_the_topic_is_held(env):
    from proactive import reactions

    first = _insight(env, 0, status="surfaced")
    asyncio.run(reactions.react("alice", first, "never", channel="app"))
    later = _insight(env, 1, urgency="high")
    _run("ledger_scan")
    assert _status(later) == "held"
    crit = _insight(env, 2, urgency="critical")
    _run("ledger_scan")
    assert _status(crit) == "surfaced"  # muted, but critical


def test_low_priority_is_held(env):
    fid = _insight(env, 0, urgency="low")  # 0.25 * 1.0 < 0.5
    _run("ledger_scan")
    assert _status(fid) == "held"


def test_quiet_period_digest_counts_once(env):
    from proactive import pushes

    a = _insight(env, 0, urgency="low")
    b = _insight(env, 1, urgency="low")
    c = _insight(env, 2, urgency="low")
    _run("ledger_scan")
    assert {_status(x) for x in (a, b, c)} == {"held"}
    _run("daily_run")
    digest = [t for t in _tg(env) if "What you may have missed" in t]
    assert len(digest) == 1
    assert [_status(x) for x in (a, b, c)].count("surfaced") == 2  # PROACTIVE_DIGEST_HELD_ITEMS
    assert pushes.last_push("alice") is not None
    _run("daily_run")  # not quiet any more
    assert len([t for t in _tg(env) if "What you may have missed" in t]) == 1


# ── Triage ───────────────────────────────────────────────────────────────────


def _model():
    from proactive.models import ResolvedModel

    return ResolvedModel("qwen", "local", "default", "qwen3:32b")


def _analysis():
    from proactive.analysis import Analysis

    return Analysis(doc_id=1, version=1, summary="Riester-Vertrag mit hohen Kosten.", topic_hint="Riester")


@pytest.fixture
def triage_llm(monkeypatch):
    from proactive import internal_client, llm, paperless_ro

    answers = {}

    async def structured(model, user, token, *, tool_name, **kw):
        return answers[tool_name]

    async def no_similar(*a, **k):
        raise internal_client.InternalUnavailable("down")

    async def whoami(tok):
        return {"first_name": "Alice", "last_name": "Example", "username": "alice"}

    monkeypatch.setattr(llm, "structured", structured)
    monkeypatch.setattr(internal_client, "similar", no_similar)
    monkeypatch.setattr(paperless_ro, "whoami", whoami)
    answers["triage"] = {
        "worth_finding": True, "topic": "Riester", "urgency": "medium", "title": "Riester-Kosten prüfen",
        "summary": "Die Kosten sind hoch.", "needs_web": True, "web_query": "Riester Kosten Kritik 2026",
        "dossier_update": False,
        "evidence_quotes": ["Die Verwaltungskosten betragen jährlich 1,2 % des Guthabens.", "Erfunden."],
    }
    answers["advice"] = {"recommendation": "Kosten vergleichen.", "sources": ["https://verbraucher.example/riester"]}
    return answers


def _finding_text(env):
    files = list((env["root"] / "alice" / "PaperlessBrain Findings" / "Notices").glob("*.md"))
    assert len(files) == 1
    return files[0].read_text()


def test_triage_without_a_fetched_page_drops_the_recommendation(env, triage_llm, monkeypatch):
    from proactive import triage, web

    async def searx(q, **kw):
        return [{"url": "https://verbraucher.example/riester", "title": "", "snippet": ""}]

    async def fetch(url):
        return None  # page not readable → nothing fetched

    monkeypatch.setattr(web, "_searx", searx)
    monkeypatch.setattr(web, "_fetch", fetch)
    asyncio.run(triage.triage("alice", "t", _analysis(), {"title": "Riester"}, _model()))
    text = _finding_text(env)
    assert "advice: false" in text and "## Recommendation" not in text
    assert "Verwaltungskosten" in text and "Erfunden" not in text


def test_triage_with_fetched_page_keeps_advice_and_disclaimer(env, triage_llm, monkeypatch):
    from proactive import triage, web

    queries = []

    async def searx(q, **kw):
        queries.append(q)
        return [{"url": "https://verbraucher.example/riester", "title": "", "snippet": ""}]

    async def fetch(url):
        return "Riester-Verträge sind oft teuer."

    monkeypatch.setattr(web, "_searx", searx)
    monkeypatch.setattr(web, "_fetch", fetch)
    asyncio.run(triage.triage("alice", "t", _analysis(), {"title": "Riester"}, _model()))
    text = _finding_text(env)
    assert "advice: true" in text and "## Recommendation" in text
    assert "Not financial or investment advice" in text
    assert "https://verbraucher.example/riester" in text
    assert queries == ["Riester Kosten Kritik 2026"]


def test_triage_query_with_the_users_name_is_not_sent(env, triage_llm, monkeypatch):
    from proactive import triage, web

    triage_llm["triage"]["web_query"] = "Alice Example Riester Kosten"
    sent = []

    async def searx(q, **kw):
        sent.append(q)
        return []

    monkeypatch.setattr(web, "_searx", searx)
    asyncio.run(triage.triage("alice", "t", _analysis(), {"title": "Riester"}, _model()))
    assert sent == [] and "advice: false" in _finding_text(env)


def test_triage_nothing_worth_it_writes_nothing(env, triage_llm):
    from proactive import triage

    triage_llm["triage"] = dict(triage_llm["triage"], worth_finding=False)
    asyncio.run(triage.triage("alice", "t", _analysis(), {}, _model()))
    assert not (env["root"] / "alice" / "PaperlessBrain Findings" / "Notices").exists()


# ── Deep dive ────────────────────────────────────────────────────────────────


def test_deep_dive_starts_the_run_and_links_it_in_the_finding(env, monkeypatch):
    from proactive import findings, internal_client, jobs, models

    fid = _insight(env, 0, status="interested")
    monkeypatch.setattr(models, "resolve_model", lambda u, t, p=None: _model())

    async def start(user, finding_id, model):
        return "run1"

    monkeypatch.setattr(internal_client, "deep_dive", start)
    _run("deep_dive", "alice", {"finding_id": fid})
    f = findings.get("alice", fid)
    assert f.status == "interested" and f.research_run == "run1"
    assert jobs.pending_count(kind="deep_dive_poll") == 1


def _poll(env, monkeypatch, answer):
    from proactive import findings, internal_client, models

    fid = _insight(env, 0, status="interested")
    asyncio.run(findings.update("alice", fid, lambda x: {"research_run": "run1"}))
    monkeypatch.setattr(models, "resolve_model", lambda u, t, p=None: _model())

    async def result(user, finding_id, run_id, model):
        return answer

    monkeypatch.setattr(internal_client, "deep_dive_result", result)
    _run("deep_dive_poll", "alice", {"finding_id": fid, "run_id": "run1"})
    return findings.get("alice", fid)


def test_deep_dive_poll_waits_for_a_running_run(env, monkeypatch):
    from proactive import jobs

    f = _poll(env, monkeypatch, {"state": "running"})
    assert f.status == "interested"
    (j,) = [jobs.get(i) for i in range(1, 5) if jobs.get(i) and jobs.get(i).kind == "deep_dive_poll"]
    assert j.status == "queued" and j.attempts == 0


def test_deep_dive_result_is_validated_written_and_does_not_count(env, monkeypatch):
    from proactive import pushes

    f = _poll(env, monkeypatch, {
        "state": "done",
        "summary": "Ein Wechsel lohnt sich.",
        "recommendation": "Angebote vergleichen.",
        "evidence": [{"doc_id": 1, "quote": "Die Abschluss- und Vertriebskosten betragen 2,5 % der Beitragssumme."}],
        "sources": ["https://a.example/x", "https://not-fetched.example"],
        "fetched": {"https://a.example/x": "2026-10-26"},
    })
    assert f.status == "researched" and f.advice
    assert "https://a.example/x" in f.body and "not-fetched" not in f.body
    head = f.body.split("\n---\n")[0]
    assert head.startswith("## Research result (") and "/werkbank?run=run1" in head
    assert "### Summary" in head and "Ein Wechsel lohnt sich." in head
    assert len(_tg(env)) == 1 and pushes.last_push("alice") is None  # asked for: not logged
    # Its own title and tag: with the finding's, the phone replaced the
    # finding's notification silently.
    ((_, msg),) = [m for m in env["sent"] if m[0] == "sendMessage"]
    assert msg["text"].startswith("Research done: ") and msg["tag"] == f"{f.finding_id}-research"


def test_deep_dive_run_deleted_leaves_the_finding_unresearched(env, monkeypatch):
    f = _poll(env, monkeypatch, {"state": "gone"})
    assert f.status == "interested" and f.research_run is None


def test_deep_dive_defers_while_the_app_is_down(env, monkeypatch):
    from proactive import internal_client, jobs, models

    fid = _insight(env, 0, status="interested")
    monkeypatch.setattr(models, "resolve_model", lambda u, t, p=None: _model())

    async def down(*a, **k):
        raise internal_client.InternalUnavailable("ConnectError")

    monkeypatch.setattr(internal_client, "deep_dive", down)
    _run("deep_dive", "alice", {"finding_id": fid})
    (j,) = [jobs.get(i) for i in range(1, 3) if jobs.get(i) and jobs.get(i).kind == "deep_dive"]
    assert j.status == "queued" and j.attempts == 0


# ── Internal API signing ─────────────────────────────────────────────────────


def test_internal_signature():
    from proactive.internal_auth import SIG_HEADER, TS_HEADER, sign, verify

    body = b'{"user":"alice"}'
    h = sign(body)
    assert verify(body, h[TS_HEADER], h[SIG_HEADER])
    assert not verify(b'{"user":"mallory"}', h[TS_HEADER], h[SIG_HEADER])
    old = sign(body, ts=int(time.time()) - 600)
    assert not verify(body, old[TS_HEADER], old[SIG_HEADER])
    assert not verify(body, None, None)
    assert re.fullmatch(r"[0-9a-f]{64}", h[SIG_HEADER])


def test_triage_judges_from_today_in_the_findings_language(env, triage_llm, monkeypatch):
    """A German letter from 2023 with a one-month termination right: the model
    must see today's date, and is told the language last (it drifted into the
    document's language when only the system prompt said it)."""
    from proactive import clock, llm, prefs, triage

    p = prefs.get("alice")
    p.findings_language = "de"
    prefs.save(p)
    seen = {}

    async def structured(model, user, token, *, tool_name, system="", messages=(), **kw):
        seen[tool_name] = (system, messages[-1]["content"])
        return {**triage_llm[tool_name], "needs_web": False} if tool_name == "triage" else triage_llm[tool_name]

    monkeypatch.setattr(llm, "structured", structured)
    asyncio.run(triage.triage("alice", "t", _analysis(), {"title": "Riester", "created": "2023-02-01"}, _model()))
    system, user_msg = seen["triage"]
    assert "window that has already closed" in system
    assert f"Today: {clock.local_now().date().isoformat()}" in user_msg
    assert "dated 2023-02-01" in user_msg
    assert user_msg.rstrip().endswith("Evidence quotes stay exactly as in the document.")
    assert "in German" in user_msg


def test_not_relevant_closes_one_finding_and_keeps_the_topic(env):
    from proactive import interests, reactions

    fid = _insight(env, 0, topic="bausparkonto")
    msg = asyncio.run(reactions.react("alice", fid, "dismiss", channel="app"))
    assert _status(fid) == "dismissed" and msg == "Dismissed."
    assert "bausparkonto" not in interests.load("alice").muted


def test_deep_dive_result_links_its_vault_note_in_the_finding_and_the_dossier(env, monkeypatch):
    """The report lives in PaperlessBrain Research/; the finding and the topic's
    dossier link it Obsidian-style, so deleting the Werkbank run loses nothing."""
    from proactive import dossiers

    asyncio.run(dossiers.write("alice", dossiers.Dossier(topic="riester", doc_ids=[1], current_state="läuft")))
    note = "PaperlessBrain Research/26-10-29 Riester prüfen.md"
    f = _poll(env, monkeypatch, {
        "state": "done", "summary": "Ein Wechsel lohnt sich.", "recommendation": "", "evidence": [],
        "sources": [], "fetched": {}, "note": note,
    })
    link = "[[PaperlessBrain Research/26-10-29 Riester prüfen]]"
    assert f.status == "researched" and f.research_note == link
    head = f.body.split("\n---\n")[0]
    assert "[[PaperlessBrain Research/26-10-29 Riester prüfen|Full report]]" in head and "/werkbank" not in head
    d = dossiers.load("alice", "riester")
    assert d.research == [link] and "## Research" in (env["root"] / "alice" / d.rel).read_text()
    # a rebuild from the model keeps the link — the model never sees it
    again = dossiers.from_result("alice", "riester", {"doc_ids": [1]}, lang="en", version=1, existing=d)
    assert again.research == [link]
