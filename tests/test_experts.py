"""Experts (docs/experts.md): scope enforcement, the Universal expert, skills,
briefing checks."""

import asyncio
import re
from datetime import datetime, timezone

import pytest

from models.paperless_document import PaperlessDocument


def _run(coro):
    return asyncio.run(coro)


def _doc(i, corr=None, dtype=None, tags=()):
    when = datetime(2026, 10, 1, tzinfo=timezone.utc)
    return PaperlessDocument(
        id=i, title=f"Doc {i}", original_file_name="x.pdf", notes=[], mime_type="application/pdf",
        tags=[f"t{t}" for t in tags], added=when, created=when, modified=when, page_count=2,
        pdf_url="", correspondent=f"c{corr}" if corr else None, document_type=f"d{dtype}" if dtype else None,
        correspondent_id=corr, document_type_id=dtype, tag_ids=list(tags),
    )


# ── Scope semantics ──────────────────────────────────────────────────────────


def test_empty_scope_restricts_nothing():
    from services.expert_scope import Scope

    s = Scope.from_meta({})
    assert s.unrestricted and s.allows(_doc(1))


def test_dimensions_and_within_or():
    from services.expert_scope import Scope

    s = Scope.from_meta({"correspondents": [{"id": 1, "name": "HUK"}, 2], "document_types": [{"id": 5}]})
    assert s.allows(_doc(1, corr=2, dtype=5))
    assert not s.allows(_doc(2, corr=3, dtype=5))     # wrong correspondent
    assert not s.allows(_doc(3, corr=1, dtype=6))     # wrong type
    assert not s.allows(_doc(4, corr=None, dtype=5))  # no correspondent, but they are restricted


def test_tags_are_any_of():
    from services.expert_scope import Scope

    s = Scope.from_meta({"tags": [{"id": 7}, {"id": 8}]})
    assert s.allows(_doc(1, tags=(3, 8))) and not s.allows(_doc(2, tags=(3,))) and not s.allows(_doc(3))


def test_never_these_takes_documents_out():
    from services.expert_scope import Scope

    s = Scope.from_meta({"exclude_correspondents": [{"id": 1}], "exclude_tags": [9]})
    assert not s.unrestricted
    assert s.allows(_doc(1, corr=2, tags=(3,)))
    assert not s.allows(_doc(2, corr=1))           # excluded correspondent
    assert not s.allows(_doc(3, corr=2, tags=(3, 9)))  # one excluded tag is enough
    assert s.allows(_doc(4, corr=None))            # no correspondent: not excluded
    both = Scope.from_meta({"document_types": [5], "exclude_document_types": [5]})
    assert not both.allows(_doc(5, dtype=5))       # never wins over only


def test_scope_hash_unchanged_without_exclusions():
    from services import briefings

    old = briefings.scope_hash({"correspondents": [1], "tags": [], "document_types": []})
    assert briefings.scope_hash({"correspondents": [{"id": 1}]}) == old
    assert briefings.scope_hash({"correspondents": [1], "exclude_tags": [3]}) != old


# ── The scoped client, and every document tool behind it ────────────────────

DOCS = {1: _doc(1, corr=1), 2: _doc(2, corr=2)}
SIDECAR = {
    "full_summary": "Police mit Selbstbeteiligung 500 EUR.", "full_summary_summarized": "Police.",
    "pages": [{"page_number": 1, "page_text": "Selbstbeteiligung 500 EUR", "images": []},
              {"page_number": 2, "page_text": "Seite zwei", "images": []}],
    "tables": [{"page": 1, "title": "T", "rows": [["a", "b"]]}], "actions": [], "cross_references": [],
}


@pytest.fixture
def archive(monkeypatch):
    from services import chat_service
    from services.paperless import PaperlessClient

    async def list_documents(self, *a, ids=None, correspondent=None, document_type=None, tag_ids_any=None, **k):
        out = list(DOCS.values())
        if ids is not None:
            out = [d for d in out if d.id in set(ids)]
        if correspondent:
            out = [d for d in out if d.correspondent_id in set(correspondent)]
        return out

    async def download(self, doc_id):
        return b"%PDF"

    monkeypatch.setattr(PaperlessClient, "list_documents", list_documents)
    monkeypatch.setattr(PaperlessClient, "download_document", download)
    monkeypatch.setattr(chat_service.sidecar_service, "load_sidecar", lambda i: SIDECAR)
    from services import clients

    monkeypatch.setattr(clients.sidecar_service, "load_sidecar", lambda i: SIDECAR)
    return chat_service


def test_scoped_client_hides_like_unknown(archive):
    from services.expert_scope import Scope, ScopedPaperlessClient

    pl = ScopedPaperlessClient("http://x", "t", Scope.from_meta({"correspondents": [1]}))
    assert [d.id for d in _run(pl.list_documents())] == [1]
    assert _run(pl.get_document(1)).id == 1
    with pytest.raises(IndexError):
        _run(pl.get_document(2))  # the same error an unknown id raises
    with pytest.raises(IndexError):
        _run(pl.download_document(2))


def _tool(cs, name, inputs, scope):
    from services.expert_scope import Scope

    async def go():
        cs._current_token.set("t")
        cs._current_owner.set("alice")
        cs._current_scope.set(Scope.from_meta(scope) if scope else None)
        text, docs, _events = await cs.execute_tool(name, inputs)
        return text, [d.document.id for d in docs]

    return _run(go())


@pytest.mark.parametrize("name,inputs", [
    ("get_document_details", {"document_id": "{id}"}),
    ("get_document_page_text", {"document_id": "{id}", "page_number": 1}),
    ("get_document_table", {"document_id": "{id}"}),
    ("show_document_image", {"document_id": "{id}", "page_number": 1}),
    ("view_document_page", {"document_id": "{id}", "page_number": 1}),
])
def test_out_of_scope_answers_exactly_like_no_such_document(archive, name, inputs):
    def call(doc_id, scope):
        ins = {k: (int(v.format(id=doc_id)) if isinstance(v, str) else v) for k, v in inputs.items()}
        return _tool(archive, name, ins, scope)

    hidden, hidden_docs = call(2, {"correspondents": [1]})
    unknown, unknown_docs = call(999, {"correspondents": [1]})
    assert re.sub(r"\d+", "N", hidden) == re.sub(r"\d+", "N", unknown)
    assert hidden_docs == [] and "500" not in hidden
    # …and the same document in scope is read (vision needs a real PDF, skipped):
    if name != "view_document_page":
        seen, _ = call(1, {"correspondents": [1]})
        assert seen != hidden


def test_universal_scope_sees_everything(archive):
    text, _ = _tool(archive, "get_document_details", {"document_id": 2}, None)
    assert text.startswith("Details for document #2")


# ── Experts ──────────────────────────────────────────────────────────────────


@pytest.fixture
def vault(tmp_path, monkeypatch):
    from config.settings import settings

    root = tmp_path / "vaults"
    (root / "alice").mkdir(parents=True)
    (root / "bob").mkdir(parents=True)
    monkeypatch.setattr(settings, "vault_root", root)
    monkeypatch.setattr(settings, "app_path", tmp_path)
    return root


def test_universal_inherits_custom_instructions_once(vault, monkeypatch):
    from services import credential_store, experts

    store = {"chat_settings": {"custom_instructions": "Antworte knapp."}}
    monkeypatch.setattr(credential_store, "load_credentials", lambda u, t: store)
    monkeypatch.setattr(credential_store, "save_credentials", lambda u, t, c: store.update(c))
    u = _run(experts.ensure_universal("alice", "tok"))
    assert u.builtin and u.personality == "Antworte knapp."
    assert store["chat_settings"]["custom_instructions_migrated"] is True
    assert store["chat_settings"]["custom_instructions"] == "Antworte knapp."  # fallback copy stays
    # Deleted file: recreated, but the old text is not imported a second time.
    (vault / "alice" / u.rel).unlink()
    again = _run(experts.ensure_universal("alice", "tok"))
    assert again.personality == ""


def test_universal_cannot_be_narrowed_renamed_or_deleted(vault):
    from services import experts

    u = _run(experts.ensure_universal("alice", None))
    u.name, u.scope_meta, u.tools_off = "Mine", {"correspondents": [1]}, ["web"]
    saved = _run(experts.save("alice", u, expected_sha=u.sha))
    assert saved.name == "Universal" and saved.scope.unrestricted and saved.disabled_groups == set()
    assert not _run(experts.delete("alice", experts.UNIVERSAL_ID))
    # An Obsidian edit that adds a scope is ignored too.
    p = vault / "alice" / saved.rel
    p.write_text(p.read_text().replace("builtin: true", "builtin: true\nscope:\n  correspondents: [1]"))
    assert experts.get("alice", experts.UNIVERSAL_ID).scope.unrestricted


def test_memory_and_vault_stay_on(vault):
    from services import experts

    e = _run(experts.create("alice", "Steuern"))
    e.tools_off = ["web", "memory", "vault"]
    e = _run(experts.save("alice", e, expected_sha=e.sha))
    assert e.disabled_groups == {"web"}


def test_names_are_unique(vault):
    from services import experts

    _run(experts.create("alice", "Steuern"))
    with pytest.raises(experts.ExpertError):
        _run(experts.create("alice", "steuern"))


def test_config_folders_are_never_embedded(vault):
    from pathlib import Path

    from config.settings import settings
    from vault.router import is_config_path

    assert is_config_path(Path(settings.experts_subfolder) / "x.md", settings.experts_subfolder, settings.skills_subfolder)
    assert is_config_path(Path(settings.skills_subfolder) / "y.md", settings.experts_subfolder, settings.skills_subfolder)
    assert not is_config_path(Path("Notes") / "z.md", settings.experts_subfolder, settings.skills_subfolder)


def test_prompt_puts_expert_blocks_before_the_personality():
    from config.chat_prompts import CUSTOM_INSTRUCTIONS_HEADER, build_system_prompt

    p = build_system_prompt({"documents"}, "alice", "en", custom_instructions="ZZPERSONA",
                            expert_blocks=["SCOPEBLOCK"])
    assert p.index("SCOPEBLOCK") < p.index(CUSTOM_INSTRUCTIONS_HEADER) < p.index("ZZPERSONA")


# ── Skills ───────────────────────────────────────────────────────────────────


def test_only_common_skills_are_shared_and_unsharing_takes_them_away(vault):
    from services import skills

    s = _run(skills.create("bob", "Steuerbescheid", "Use when a notice is unclear.", "Steps", common=True))
    _run(skills.create("bob", "Privat", "", "secret"))
    shared = skills.list_shared_by_others("alice")
    assert [x.name for x in shared] == ["Steuerbescheid"]
    ref = shared[0].ref_for("alice")
    assert ref == f"shared:bob/{s.skill_id}" and skills.resolve("alice", ref).body == "Steps"
    s.common = False
    _run(skills.save("bob", s, expected_sha=s.sha))
    assert skills.resolve("alice", ref) is None and skills.list_shared_by_others("alice") == []


def test_shared_ref_cannot_escape_the_vault_root(vault):
    from services import skills

    assert skills.resolve("alice", "shared:../etc/x") is None
    assert skills.resolve("alice", "shared:alice/x") is None  # own skills are not "shared"


def test_small_skill_sets_inline_large_ones_load_on_demand():
    from services import skills

    small = [skills.Skill("a", "A", "Use when a", "short")]
    block, need = skills.prompt_block(small)
    assert "short" in block and not need
    big = [skills.Skill(str(i), f"S{i}", "Use when", "x" * 1000) for i in range(4)]
    block, need = skills.prompt_block(big)
    assert need and "x" * 50 not in block and "use_skill" in block


def test_use_skill_returns_only_assigned_skills():
    from services import chat_service as cs
    from services import skills

    async def go(name):
        cs._current_skills.set([skills.Skill("a", "Police lesen", "", "BODY")])
        return cs._tool_use_skill({"name": name})

    assert "BODY" in _run(go("police lesen"))
    assert "No skill" in _run(go("Something else"))


def test_lint_flags_invented_tools_and_keeps_real_ones():
    from services.skill_wizard import lint

    found = lint("X", "Use when y", "1. call search_policies\n2. then get_document_details")
    codes = {(f["code"], f["text"].split(" ")[0]) for f in found}
    assert ("unknown_tool", "search_policies") in codes
    assert not any(t == "get_document_details" for _c, t in codes)


def test_use_skill_never_reaches_the_werkbank():
    from werkbank.v2.registry import load_defaults

    assert "use_skill" in load_defaults().forbidden_tools


# ── Briefings ────────────────────────────────────────────────────────────────


def test_briefing_checks_drop_what_the_documents_do_not_say():
    from services import briefings

    studied = {1: "(#1) 2025-12-01 · HUK · Police: Beitrag 412 EUR ab 2026.",
               2: "(#2) 2024-03-01 · Allianz · Rechnung: Hausrat 88 EUR."}
    result = {
        "about": "Two insurers (#1) (#2).",
        "parties": [{"text": "HUK, premium 412 EUR", "doc_ids": [1]},
                    {"text": "HUK, premium 999 EUR", "doc_ids": [1]},       # number not in #1
                    {"text": "Somebody", "doc_ids": [77]},                  # not studied
                    {"text": "Allianz household", "doc_ids": []}],          # no source
        "matters": [], "open_items": [], "glossary": [{"term": "SF", "meaning": "no-claims class"}],
        "events": [{"date": "2025-12-01", "text": "Premium raised", "doc_ids": [1]},
                   {"date": "sometime", "text": "x", "doc_ids": [1]}],
    }
    kept, dropped = briefings.validate(result, studied)
    assert [p["text"] for p in kept["parties"]] == ["HUK, premium 412 EUR"]
    assert {d["reason"] for d in dropped} == {"unsupported_number", "unknown_id", "no_source", "bad_date"}
    assert kept["events"][0]["date"] == "2025-12-01" and kept["glossary"]


def test_briefing_refs_are_never_citations():
    from services import briefings
    from services.citations import CITE_RE

    md = briefings.orientation_markdown({"parties": [{"text": "HUK", "doc_ids": [241]}]})
    assert "(#241)" in md and not CITE_RE.search(md)
    b = briefings.Briefing(expert_id="e", body=md)
    assert briefings.BRIEFING_HEADER in briefings.prompt_block(b) and "MAP, not evidence" in briefings.BRIEFING_HEADER


def test_archive_map_is_code_counts():
    from services import briefings

    docs = [_doc(i, corr=1, dtype=2) for i in range(5)]
    m = briefings.archive_map(docs)
    assert m["total"] == 5 and m["correspondents"] == [["c1", 5]]


def test_staleness_sees_new_documents_and_scope_changes():
    from services import briefings

    b = briefings.Briefing(expert_id="e", studied_ids=[1, 2], last_doc_id=2,
                           scope_hash=briefings.scope_hash({"correspondents": [1]}))
    docs = [_doc(1), _doc(2), _doc(3)]
    assert briefings.staleness(b, docs, {"correspondents": [1]}) == {"state": "outdated", "new": 1}
    assert briefings.staleness(b, docs, {"correspondents": [2]})["state"] == "scope_changed"
    assert briefings.staleness(None, docs, {})["state"] == "none"
