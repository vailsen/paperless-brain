"""Proactive Phase 4: findings ledger, state machine, reactions, findings page rules."""

import asyncio
from pathlib import Path

import pytest

from proactive.states import reactions_for, transition


@pytest.fixture
def vaults(proactive_on, tmp_path, monkeypatch):
    from config.settings import settings
    from proactive import findings

    root = tmp_path / "vaults"
    (root / "alice").mkdir(parents=True)
    (root / "bob").mkdir(parents=True)
    monkeypatch.setattr(settings, "vault_root", root)
    findings._index.clear()
    return root


@pytest.fixture
def telegram_on(vaults, monkeypatch):
    from config import proactive as cfgmod
    from proactive import linking, prefs
    from proactive.notify import telegram

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "1:x")
    cfgmod.proactive_settings.cache_clear()
    sent = []

    async def fake_call(method, payload=None, *, timeout=30):
        sent.append((method, payload or {}))
        return {}

    monkeypatch.setattr(telegram, "call", fake_call)
    for user, chat in (("alice", "11"), ("bob", "22")):
        p = prefs.get(user)
        p.optin, p.channels, p.language = True, "telegram", "en"
        prefs.save(p)
        linking.link(user, "telegram", chat)
    from proactive import users

    monkeypatch.setattr(users, "eligible", lambda: [("alice", "t-a"), ("bob", "t-b")])
    return sent


def _run_worker(times=2):
    from proactive.worker import Worker
    import proactive.tasks  # noqa: F401

    from proactive import jobs

    jobs.enqueue("ledger_scan")
    w = Worker()
    for _ in range(times):
        asyncio.run(w.tick_once())


MANUAL = """---
finding_id: abcdefg
kind: insight
title: Kfz-Versicherung Wechselfrist
status: new
topic: kfz-versicherung
urgency: high
doc_ids: [1234]
---
## Summary

Your car insurance can be cancelled until 30 November.
"""


def _manual(vaults, user="alice", text=MANUAL, name="manual.md") -> Path:
    from proactive.paths import findings_root

    d = findings_root(user) / "Notices"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text)
    return p


# ── State machine ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,event,expected",
    [
        ("new", "push", "surfaced"),
        ("new", "hold", "held"),
        ("held", "digest", "surfaced"),
        ("surfaced", "interested", "interested"),
        ("interested", "researched", "researched"),
        ("researched", "done", "done"),
        ("surfaced", "later", "snoozed"),
        ("snoozed", "wake", "surfaced"),
        ("surfaced", "never", "dismissed"),
        ("dismissed", "reopen", "new"),
        ("surfaced", "wrong", "dismissed"),
        # not applicable → None
        ("interested", "interested", None),
        ("interested", "later", None),
        ("dismissed", "interested", None),
        ("done", "push", None),
        # a finding is listed in the app before the ledger scan decided on it
        ("new", "interested", "interested"),
        ("new", "dismiss", "dismissed"),
        ("done", "dismiss", None),
    ],
)
def test_transitions(status, event, expected):
    assert transition(status, event) == expected


def test_unknown_event_is_an_error():
    with pytest.raises(ValueError):
        transition("new", "explode")


def test_buttons_follow_kind_and_status():
    assert reactions_for("surfaced", "deadline") == ["done", "wrong"]
    assert reactions_for("surfaced", "insight") == ["interested", "later", "dismiss", "never", "done"]
    assert reactions_for("new", "insight") == ["interested", "later", "dismiss", "never", "done"]
    assert reactions_for("dismissed", "insight") == []


# ── Paths ────────────────────────────────────────────────────────────────────


def test_findings_root_must_be_inside_the_vault(vaults, monkeypatch):
    from config import proactive as cfgmod
    from proactive.paths import findings_rel, is_findings_path

    assert findings_rel() == "PaperlessBrain Findings"
    assert is_findings_path("PaperlessBrain Findings/Notices/x.md")
    assert not is_findings_path("PaperlessBrain Memos/x.md")
    monkeypatch.setenv("PROACTIVE_FINDINGS_ROOT", str(vaults / "{user}" / "Brain" / "Findings"))
    cfgmod.proactive_settings.cache_clear()
    assert findings_rel() == "Brain/Findings"
    monkeypatch.setenv("PROACTIVE_FINDINGS_ROOT", "/srv/elsewhere/{user}")
    cfgmod.proactive_settings.cache_clear()
    with pytest.raises(ValueError):
        findings_rel()


def test_vault_sync_never_embeds_findings(vaults):
    from vault.sync import _is_findings

    assert _is_findings(Path("PaperlessBrain Findings/_interests.md"))
    assert _is_findings(Path("PaperlessBrain Findings/Notices/2026-10-26 X.md"))
    assert not _is_findings(Path("To-Dos/Bremsen.md"))


def test_vault_sync_skips_findings_before_writing_anything(vaults):
    """_embed_file must not add pbrain_id / dont_ingest to a finding or embed it."""
    from vault import sync

    p = _manual(vaults)
    before = p.read_text()
    deleted = []

    class FakeChroma:
        async def delete(self, where):
            deleted.append(where)

        async def upsert(self, **kw):  # pragma: no cover — must not be reached
            raise AssertionError("embedded a finding")

    rel = p.relative_to(vaults / "alice")
    ok = asyncio.run(sync._embed_file("alice", vaults / "alice", rel, FakeChroma(), FakeChroma()))
    assert ok and p.read_text() == before
    assert deleted == [{"path": {"$eq": str(rel)}}]


# ── Files ────────────────────────────────────────────────────────────────────


def test_create_parse_and_access_is_per_user(vaults):
    from proactive import findings

    f = findings.Finding(
        finding_id="", kind="insight", title="Riester: Kosten prüfen", topic="riester",
        doc_ids=[5, 3], evidence_hash="h1", finding_key="riester:5",
        body=findings.render_body("en", summary="High fees.", evidence=[{"doc_id": 5, "quote": "Kosten  2,5 %"}],
                                  sources=[{"url": "https://example.org/x", "retrieved": "2026-10-26"}],
                                  recommendation="Compare offers."),
        advice=True,
    )
    made = asyncio.run(findings.create("alice", f))
    assert findings.valid_finding_id(made.finding_id) and made.pbrain_id
    assert made.rel.startswith("PaperlessBrain Findings/Notices/")
    assert made.rel.endswith(" Riester Kosten prüfen.md")
    text = (vaults / "alice" / made.rel).read_text()
    assert "> Kosten 2,5 %" in text and "Not financial or investment advice" in text
    assert findings.get("alice", made.finding_id).title == "Riester: Kosten prüfen"
    assert findings.get("bob", made.finding_id) is None  # not in bob's ledger
    assert findings.locate(made.finding_id).user == "alice"


def test_dismissed_finding_is_not_resurfaced_on_same_evidence(vaults):
    from proactive import findings

    def mk(h):
        return findings.Finding(finding_id="", kind="insight", title="Tarif", topic="strom",
                                doc_ids=[7], evidence_hash=h, finding_key="strom:7", body="x\n")

    f, outcome = asyncio.run(findings.upsert("alice", mk("h1")))
    assert outcome == "created"
    asyncio.run(findings.update("alice", f.finding_id, lambda x: {"status": "dismissed"}))
    again, outcome = asyncio.run(findings.upsert("alice", mk("h1")))
    assert outcome == "unchanged" and again.status == "dismissed"
    new, outcome = asyncio.run(findings.upsert("alice", mk("h2")))  # a new document
    assert outcome == "reopened" and new.status == "new" and new.finding_id == f.finding_id


# ── End to end: manual finding → message → button → files ───────────────────


def test_manual_finding_notification_button_updates_files(telegram_on, vaults):
    from proactive import findings, interests, jobs
    from proactive.notify import telegram

    p = _manual(vaults)
    _run_worker()
    sends = [pl for m, pl in telegram_on if m == "sendMessage"]
    assert len(sends) == 1 and sends[0]["chat_id"] == "11"
    assert "Kfz-Versicherung Wechselfrist" in sends[0]["text"]
    assert "status: surfaced" in p.read_text()

    cq = {"update_id": 5, "callback_query": {"id": "q", "data": "f:abcdefg:interested", "message": {"chat": {"id": 11}}}}
    asyncio.run(telegram.handle_update(cq))
    f = findings.get("alice", "abcdefg")
    assert f.status == "interested"
    i = interests.load("alice")
    assert (i.rows["kfz-versicherung"].surfaced, i.rows["kfz-versicherung"].interested) == (1, 1)
    assert jobs.pending_count(kind="deep_dive") == 1
    # the user's own text survived: only status keys changed
    assert "Your car insurance can be cancelled until 30 November." in p.read_text()


def test_second_channel_reaction_is_ignored(telegram_on, vaults):
    from proactive import findings, reactions

    _manual(vaults, text=MANUAL.replace("status: new", "status: surfaced"))
    asyncio.run(reactions.react("alice", "abcdefg", "interested", channel="telegram"))
    msg = asyncio.run(reactions.react("alice", "abcdefg", "later", channel="email"))
    assert msg == "Already handled."
    assert findings.get("alice", "abcdefg").status == "interested"


def test_never_mutes_the_topic(telegram_on, vaults):
    from proactive import interests, reactions

    _manual(vaults, text=MANUAL.replace("status: new", "status: surfaced"))
    asyncio.run(reactions.react("alice", "abcdefg", "never", channel="app"))
    i = interests.load("alice")
    assert "kfz-versicherung" in i.muted
    assert (i.rows["kfz-versicherung"].never, i.rows["kfz-versicherung"].dismissed) == (1, 1)


def test_status_edited_in_obsidian_is_respected(telegram_on, vaults):
    from proactive import findings

    p = _manual(vaults)
    p.write_text(p.read_text().replace("status: new", "status: dismissed"))
    _run_worker()
    assert not [pl for m, pl in telegram_on if m == "sendMessage"]
    assert findings.get("alice", "abcdefg").status == "dismissed"


def test_snoozed_finding_wakes_up(telegram_on, vaults):
    _manual(vaults, text=MANUAL.replace("status: new", "status: snoozed\nsnoozed_until: 2000-01-01"))
    _run_worker()
    assert len([pl for m, pl in telegram_on if m == "sendMessage"]) == 1


# ── Concurrent edits ─────────────────────────────────────────────────────────


def _edit_after_first_read(monkeypatch, edit):
    """Simulate Obsidian saving between the worker's read and its write."""
    from vault import notes

    real = notes.read_note
    state = {"done": False}

    async def read_then_user_edits(user, rel):
        snap = await real(user, rel)
        if not state["done"]:
            state["done"] = True
            path = notes.resolve(user, rel)
            path.write_text(edit(path.read_text()))
        return snap

    monkeypatch.setattr(notes, "read_note", read_then_user_edits)


def test_user_edit_to_another_key_is_merged(telegram_on, vaults, monkeypatch):
    from proactive import reactions

    p = _manual(vaults, text=MANUAL.replace("status: new", "status: surfaced"))
    _edit_after_first_read(monkeypatch, lambda t: t.replace("urgency: high", "urgency: low"))
    asyncio.run(reactions.react("alice", "abcdefg", "interested", channel="telegram"))
    text = p.read_text()
    assert "status: interested" in text and "urgency: low" in text


def test_user_edit_to_status_wins_and_reaction_is_re_decided(telegram_on, vaults, monkeypatch):
    from proactive import reactions

    p = _manual(vaults, text=MANUAL.replace("status: new", "status: surfaced"))
    _edit_after_first_read(monkeypatch, lambda t: t.replace("status: surfaced", "status: dismissed"))
    msg = asyncio.run(reactions.react("alice", "abcdefg", "interested", channel="telegram"))
    assert "status: dismissed" in p.read_text()
    assert msg == "Already handled."


def test_user_body_edit_is_kept_when_worker_changes_frontmatter(telegram_on, vaults, monkeypatch):
    from proactive import reactions

    p = _manual(vaults, text=MANUAL.replace("status: new", "status: surfaced"))
    _edit_after_first_read(monkeypatch, lambda t: t + "\nMy own note: call the broker.\n")
    asyncio.run(reactions.react("alice", "abcdefg", "later", channel="app"))
    text = p.read_text()
    assert "My own note: call the broker." in text and "status: snoozed" in text


# ── Interests file ───────────────────────────────────────────────────────────


def test_interests_weight_and_manual_merge():
    from proactive.interests import parse

    i = parse(
        "---\nmuted: [Handyvertrag]\n---\n"
        "| topic | surfaced | interested | dismissed | never | last_reaction |\n|---|---|---|---|---|---|\n"
        "| kfz-versicherung | 3 | 2 | 0 | 0 | 2026-10-20 |\n"
        "| KFZ Versicherung | 1 | 1 | 1 | 0 | 2026-10-21 |\n"
    )
    assert i.muted == {"handyvertrag"}
    r = i.rows["kfz-versicherung"]
    assert (r.surfaced, r.interested, r.dismissed, r.last_reaction) == (4, 3, 1, "2026-10-21")
    assert i.weight("kfz-versicherung") == pytest.approx(2 * 4 / 6)
    assert i.weight("unknown") == 1.0
