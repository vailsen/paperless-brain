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
    from proactive.notify import telegram

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    monkeypatch.setenv("SMTP_HOST", "smtp")
    monkeypatch.setenv("SMTP_FROM", "brain@example.com")
    monkeypatch.setenv("IMAP_HOST", "imap")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("PROACTIVE_MAX_PUSHES_PER_WEEK", "3")
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

    async def fake_call(method, payload=None, *, timeout=30):
        sent.append((method, payload or {}))
        return {}

    monkeypatch.setattr(telegram, "call", fake_call)

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
    p.optin, p.channels, p.language = True, "both", "en"
    prefs.save(p)
    linking.link("alice", "telegram", "11")
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

        async def local_ready(self):
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


# ── Weekly limit, scoring, digest ────────────────────────────────────────────


def test_limit_3_fourth_push_is_held_and_both_channels_count_once(env):
    from proactive import pushes

    ids = [_insight(env, i) for i in range(4)]
    _run("ledger_scan")
    assert [_status(i) for i in ids].count("surfaced") == 3
    assert [_status(i) for i in ids].count("held") == 1
    assert pushes.count_week("alice") == 3
    assert len(_tg(env)) == 3 and len([m for m, _ in env["sent"] if m == "mail"]) == 3


def test_limit_0_no_insights_but_critical_deadline_arrives(env, monkeypatch):
    from datetime import date

    from proactive import clock, prefs
    from proactive.paths import findings_root

    p = prefs.get("alice")
    p.max_pushes_per_week = 0
    prefs.save(p)
    fid = _insight(env, 0, urgency="critical")
    _run("ledger_scan")
    assert _status(fid) == "held"  # 0 = no insight pushes, critical or not
    today = clock.local_now().date()
    due = date.fromordinal(today.toordinal() + 1).isoformat()
    later = date.fromordinal(today.toordinal() + 20).isoformat()
    d = findings_root("alice") / "Notices"
    (d / "dl1.md").write_text(f"---\nfinding_id: deadlin\nkind: deadline\ntitle: Pay\nstatus: new\nurgency: medium\n"
                              f"deadline: {due}\ndeadline_kind: payment\ndoc_ids: [1]\n---\nx\n")
    (d / "dl2.md").write_text(f"---\nfinding_id: deadlio\nkind: deadline\ntitle: Pay later\nstatus: new\nurgency: low\n"
                              f"deadline: {later}\ndeadline_kind: payment\ndoc_ids: [1]\n---\nx\n")
    _run("daily_run")
    texts = _tg(env)
    assert len(texts) == 1 and "Pay" in texts[0] and "Due tomorrow" in texts[0]
    assert _status("deadlio") == "new"  # non-critical reminder waits, not dropped


def test_never_mutes_and_a_later_finding_on_the_topic_is_held(env):
    from proactive import reactions

    first = _insight(env, 0, status="surfaced")
    asyncio.run(reactions.react("alice", first, "never", channel="telegram"))
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


def test_value_above_the_ceiling_cannot_be_saved(env):
    from proactive import prefs, pushes

    p = prefs.get("alice")
    p.max_pushes_per_week = 99
    prefs.save(p)
    assert prefs.get("alice").max_pushes_per_week == 14
    assert pushes.limit_for("alice") == 14


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
    assert pushes.count_week("alice") == 1
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


def test_deep_dive_result_is_validated_written_and_does_not_count(env, monkeypatch):
    from proactive import findings, internal_client, models, pushes

    fid = _insight(env, 0, status="interested")
    monkeypatch.setattr(models, "resolve_model", lambda u, t, p=None: _model())

    async def remote(user, finding_id, model):
        return {
            "summary": "Ein Wechsel lohnt sich.",
            "recommendation": "Angebote vergleichen.",
            "evidence": [{"doc_id": 1, "quote": "Die Abschluss- und Vertriebskosten betragen 2,5 % der Beitragssumme."}],
            "sources": ["https://a.example/x", "https://not-fetched.example"],
            "fetched": {"https://a.example/x": "2026-10-26"},
        }

    monkeypatch.setattr(internal_client, "deep_dive", remote)
    _run("deep_dive", "alice", {"finding_id": fid})
    f = findings.get("alice", fid)
    assert f.status == "researched" and f.advice
    assert "https://a.example/x" in f.body and "not-fetched" not in f.body
    assert len(_tg(env)) == 1 and pushes.count_week("alice") == 0


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
