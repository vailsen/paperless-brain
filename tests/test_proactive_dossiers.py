"""Proactive Phase 8: dossiers, incremental updates, revisits, version bump,
the guard inside Werkbank runs."""

import asyncio
import json
import logging
from datetime import timedelta

import pytest


@pytest.fixture
def env(proactive_on, tmp_path, monkeypatch):
    from config.settings import settings
    from proactive import findings, prefs, users

    root = tmp_path / "vaults"
    (root / "alice").mkdir(parents=True)
    monkeypatch.setattr(settings, "vault_root", root)
    monkeypatch.setattr(settings, "app_path", tmp_path)
    monkeypatch.setattr(settings, "extraction_sidecar_path", "sidecars")
    (tmp_path / "sidecars").mkdir()
    for i, text in {
        10: "Bausparvertrag abgeschlossen am 01.03.2015, Bausparsumme 50.000 EUR.",
        11: "Jahreskontoauszug 2020: Guthaben 12.400 EUR, Zins 0,5 %.",
        12: "Zuteilung möglich ab 01.01.2027. Bitte teilen Sie uns mit, ob Sie das Darlehen in Anspruch nehmen.",
    }.items():
        (tmp_path / "sidecars" / f"{i}.json").write_text(json.dumps({"pages": [{"page_text": text}]}))
    findings._index.clear()
    p = prefs.get("alice")
    p.optin, p.language = True, "en"
    prefs.save(p)
    monkeypatch.setattr(users, "eligible", lambda: [("alice", "t")])
    monkeypatch.setattr(users, "token", lambda u: "t")
    from proactive import models
    from proactive.models import ResolvedModel

    monkeypatch.setattr(models, "resolve_model", lambda u, t, p=None: ResolvedModel("qwen", "local", "default", "qwen3"))
    import proactive.tasks.topics as topics

    monkeypatch.setattr(topics, "resolve_model", lambda u, t, p=None: ResolvedModel("qwen", "local", "default", "qwen3"))
    return root


BUILD = {
    "mode": "build",
    "doc_ids": [10, 11],
    "read_doc_ids": [10, 11],
    "current_state": "Guthaben 12.400 EUR.",
    "timeline": [
        {"date": "2020-12-31", "event": "Jahreskontoauszug", "doc_id": 11},
        {"date": "2015-03-01", "event": "Vertrag abgeschlossen", "doc_id": 10},
        {"date": "2019-01-01", "event": "invented", "doc_id": 999},      # not a dossier document
        {"date": "irgendwann", "event": "no date", "doc_id": 10},        # unparsable
    ],
    "open_questions": ["Darlehen nehmen?"],
}


@pytest.fixture
def remote(monkeypatch):
    from proactive import internal_client

    calls = []

    async def dossier(user, topic, seed, existing, model):
        calls.append({"topic": topic, "seed": seed, "existing": existing})
        if existing:
            return {
                "mode": "update", "doc_ids": sorted(set(existing["doc_ids"]) | {seed}), "read_doc_ids": [seed],
                "current_state": "Zuteilung ab 2027.",
                "timeline": existing["timeline"] + [{"date": "2026-10-20", "event": "Zuteilungsschreiben", "doc_id": seed}],
                "open_questions": ["Darlehen nehmen?"],
            }
        return json.loads(json.dumps(BUILD))

    monkeypatch.setattr(internal_client, "dossier", dossier)
    return calls


def _run(kind, user="", payload=None, gate_ok=True):
    import proactive.tasks  # noqa: F401
    from proactive import jobs
    from proactive.worker import Worker

    class G:
        woke_host = False

        async def local_ready(self, may_wake=True):
            return gate_ok

    jobs.enqueue(kind, user, payload or {})
    asyncio.run(Worker(gate=G()).tick_once())


def test_build_validates_the_timeline_and_round_trips(env, remote):
    from proactive import dossiers

    _run("dossier_update", "alice", {"topic": "bausparvertrag", "doc_id": 10})
    d = dossiers.load("alice", "bausparvertrag")
    assert d.doc_ids == [10, 11]
    assert [(t["date"], t["doc_id"]) for t in d.timeline] == [("2015-03-01", 10), ("2020-12-31", 11)]
    assert d.open_questions == ["Darlehen nehmen?"] and d.revisit_after
    text = (env / "alice" / "PaperlessBrain Findings" / "Dossiers" / "bausparvertrag.md").read_text()
    assert "documents/11/details" in text and "invented" not in text


def test_new_letter_updates_incrementally(env, remote, caplog):
    from proactive import dossiers

    _run("dossier_update", "alice", {"topic": "bausparvertrag", "doc_id": 10})
    with caplog.at_level(logging.INFO, logger="proactive.topics"):
        _run("dossier_update", "alice", {"topic": "bausparvertrag", "doc_id": 12})
    assert remote[1]["existing"]["doc_ids"] == [10, 11]  # the old dossier went in
    assert any("update — read 1 document(s)" in r.message for r in caplog.records)
    d = dossiers.load("alice", "bausparvertrag")
    assert d.doc_ids == [10, 11, 12] and d.timeline[-1]["doc_id"] == 12
    assert d.current_state == "Zuteilung ab 2027."


def test_revisit_interval_follows_interest(env):
    from datetime import date

    from proactive import dossiers, interests

    today = date(2026, 10, 26)
    assert dossiers.revisit_after("alice", "x", today) == "2027-01-24"  # unknown → 90 days
    for _ in range(3):
        asyncio.run(interests.record("alice", "hot", "interested"))
    assert dossiers.revisit_after("alice", "hot", today) == "2026-11-25"  # weight 2.0 → 30 days
    for _ in range(4):
        asyncio.run(interests.record("alice", "cold", "dismissed"))
    assert dossiers.revisit_after("alice", "cold", today) == "2027-04-24"  # weight 0.33 → 180 days
    asyncio.run(interests.record("alice", "gone", "never", mute=True))
    assert dossiers.revisit_after("alice", "gone", today) is None


def test_version_bump_rebuilds_once(env, remote, monkeypatch):
    from config import proactive as cfgmod
    from proactive import jobs
    from proactive.tasks import topics

    _run("dossier_update", "alice", {"topic": "bausparvertrag", "doc_id": 10})
    monkeypatch.setenv("PROACTIVE_ANALYSIS_VERSION", "2")
    cfgmod.proactive_settings.cache_clear()
    asyncio.run(topics.schedule_topics("alice"))
    asyncio.run(topics.schedule_topics("alice"))
    assert jobs.pending_count(kind="dossier_build") == 1


def test_rebuild_waits_for_a_window(env, remote, monkeypatch):
    from config import proactive as cfgmod
    from proactive import clock, dossiers, jobs

    _run("dossier_update", "alice", {"topic": "bausparvertrag", "doc_id": 10})
    monkeypatch.setenv("PROACTIVE_ANALYSIS_VERSION", "2")
    now = clock.local_now()
    far = now + timedelta(hours=6)
    monkeypatch.setenv("PROACTIVE_SWEEP_WINDOWS", f"{far:%H:%M}-{far + timedelta(hours=1):%H:%M}/10")
    cfgmod.proactive_settings.cache_clear()
    _run("dossier_build", "alice", {"topic": "bausparvertrag"})
    (j,) = [jobs.get(i) for i in range(1, 10) if jobs.get(i) and jobs.get(i).kind == "dossier_build"]
    # Outside a window a backlog job is not even picked up (and so never wakes
    # the host): untouched, not deferred.
    assert j.status == "queued" and j.attempts == 0 and j.last_error is None
    monkeypatch.setenv("PROACTIVE_SWEEP_WINDOWS", f"{now - timedelta(hours=1):%H:%M}-{now + timedelta(hours=3):%H:%M}/10")
    cfgmod.proactive_settings.cache_clear()
    _run("heartbeat")
    assert remote[-1]["existing"] is None  # a full rebuild, not an update
    assert dossiers.load("alice", "bausparvertrag").analysis_version == 2


def test_revisit_can_produce_a_finding(env, remote, monkeypatch):
    from config import proactive as cfgmod
    from proactive import clock, dossiers, findings, llm, paperless_ro, web

    _run("dossier_update", "alice", {"topic": "bausparvertrag", "doc_id": 10})
    d = dossiers.load("alice", "bausparvertrag")
    d.revisit_after = "2000-01-01"
    asyncio.run(dossiers.write("alice", d))
    now = clock.local_now()
    monkeypatch.setenv("PROACTIVE_SWEEP_WINDOWS", f"{now - timedelta(hours=1):%H:%M}-{now + timedelta(hours=1):%H:%M}/10")
    cfgmod.proactive_settings.cache_clear()

    async def whoami(t):
        return {"username": "alice"}

    async def searx(q, **kw):
        return [{"url": "https://bausparen.example/2026", "title": "", "snippet": ""}]

    async def fetch(url):
        return "Bausparzinsen 2026 gestiegen."

    async def structured(model, user, token, **kw):
        return {"worth_finding": True, "title": "Zuteilung prüfen", "summary": "Zinsen gestiegen.",
                "urgency": "medium", "recommendation": "Darlehen prüfen.",
                "evidence_quotes": [{"doc_id": 11, "quote": "Jahreskontoauszug 2020: Guthaben 12.400 EUR, Zins 0,5 %."}],
                "sources": ["https://bausparen.example/2026"]}

    monkeypatch.setattr(paperless_ro, "whoami", whoami)
    monkeypatch.setattr(web, "_searx", searx)
    monkeypatch.setattr(web, "_fetch", fetch)
    monkeypatch.setattr(llm, "structured", structured)
    _run("dossier_revisit", "alice", {"topic": "bausparvertrag"})
    (f,) = findings.list_findings("alice")
    assert f.advice and f.topic == "bausparvertrag" and "https://bausparen.example/2026" in f.body
    assert dossiers.load("alice", "bausparvertrag").revisit_after > "2026"


# ── Werkbank hooks ───────────────────────────────────────────────────────────


def test_web_query_guard_applies_inside_werkbank_belts():
    from werkbank.v2 import registry
    from werkbank.v2.tools import ToolBelt, web_query_guard

    from proactive import guard

    belt = ToolBelt(registry=registry.load_defaults(), allowed_tools=["web_search"], persist=False)
    called = []

    async def fake_call(name, args):
        called.append(args)
        return "results"

    belt._call = fake_call
    token = web_query_guard.set(lambda q: guard.check(q, ["alice"]))
    try:
        out = asyncio.run(belt.execute("web_search", {"query": "Alice Vertrag Nr. 4711"}))
        assert "query rejected" in out and called == []
        assert asyncio.run(belt.execute("web_search", {"query": "Bausparen Zinsen 2026"})) == "results"
    finally:
        web_query_guard.reset(token)
    # interactive runs: no guard
    assert asyncio.run(belt.execute("web_search", {"query": "Alice Vertrag Nr. 4711"})) == "results"


def test_deep_dive_runs_werkbank_with_the_guard_set(monkeypatch):
    from proactive import deep_dive_app, paperless_ro
    from proactive.findings import Finding
    from proactive.models import ResolvedModel
    from werkbank import llm_lane
    from werkbank.v2 import pipeline, store
    from werkbank.v2.tools import web_query_guard

    seen = {}

    async def whoami(t):
        return {"first_name": "Alice", "username": "alice"}

    async def run_from_request(request, user, model, *, notify=True):
        seen["notify"] = notify
        screen = web_query_guard.get()
        seen["guarded"] = screen is not None and screen("Alice Riester") is not None
        seen["request"] = request
        return "run1", None

    async def condensed(*a, **k):
        return {"summary": "s", "recommendation": "r", "evidence": [], "sources": ["https://x"]}

    monkeypatch.setattr(paperless_ro, "whoami", whoami)
    monkeypatch.setattr(pipeline, "run_from_request", run_from_request)
    monkeypatch.setattr(pipeline, "register_token", lambda u, t: None)
    monkeypatch.setattr(store, "load_report", lambda r, u: "report")
    monkeypatch.setattr(store, "fetched_pages", lambda r, u: {"https://x": "2026-10-26"})
    monkeypatch.setattr(llm_lane, "complete_structured", condensed)
    f = Finding(finding_id="abcdefg", kind="insight", title="Riester", topic="riester", doc_ids=[1], body="b")
    out = asyncio.run(deep_dive_app.run("alice", "tok", f, ResolvedModel("q", "local", "default", "q")))
    assert seen["guarded"] and "generic terms" in seen["request"]
    assert seen["notify"] is False  # its result message is the notification
    assert web_query_guard.get() is None  # reset after the run
    assert out["fetched"] == {"https://x": "2026-10-26"} and out["run_id"] == "run1"
