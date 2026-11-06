"""Findings workspace (docs/findings-workspace.md): dossier tree and
insights, provenance from the tool log, the merge checks, ledger search."""

import asyncio

import pytest


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def vault(proactive_on, tmp_path, monkeypatch):
    from config.settings import settings

    root = tmp_path / "vaults"
    (root / "alice").mkdir(parents=True)
    monkeypatch.setattr(settings, "vault_root", root)
    monkeypatch.setattr(settings, "app_path", tmp_path)
    return root


def _insight(title="ADAC günstiger", text="Kostet 412 € im Jahr."):
    from proactive import dossiers

    return dossiers.Insight(insight_id="", created="2026-11-02T14:03", title=title, text=text,
                            evidence="documents+web", conv_id="c1", msg_index=3, model="m",
                            docs=[{"id": 101, "page": 2}], web=[{"url": "https://example.org/x"}])


def test_insights_survive_a_rebuild_and_round_trip(vault):
    from proactive import dossiers

    d = _run(dossiers.write("alice", dossiers.Dossier(topic="kfz", doc_ids=[101], current_state="alt",
                                                      open_questions=["Frist?", "Werkstatt?"])))
    _run(dossiers.add_insight("alice", "kfz", _insight(), answered=["Werkstatt?"], new_questions=["Zweitwagen?"]))
    before = dossiers.load("alice", "kfz")
    assert before.insights[0].docs == [{"id": 101, "page": 2}] and before.insights[0].text == "Kostet 412 € im Jahr."
    assert before.open_questions == ["Frist?", "Zweitwagen?"] and before.resolved[0]["question"] == "Werkstatt?"
    # A model rebuild that knows nothing of the insight keeps it, and does not
    # bring back the answered question.
    rebuilt = dossiers.from_result("alice", "kfz", {"doc_ids": [101], "current_state": "neu",
                                                    "open_questions": ["Frist?", "Werkstatt?"]},
                                   lang="de", version=2, existing=before)
    _run(dossiers.write("alice", rebuilt))
    after = dossiers.load("alice", "kfz")
    assert after.current_state == "neu" and after.open_questions == ["Frist?"]
    assert [i.meta() for i in after.insights] == [i.meta() for i in before.insights]
    assert "saved_insights" in after.existing_payload()


def test_old_positional_file_still_reads(vault):
    from proactive import dossiers
    from proactive.paths import DOSSIERS, findings_root

    p = findings_root("alice") / DOSSIERS
    p.mkdir(parents=True)
    (p / "alt.md").write_text(
        "---\npbrain_id: x1\ntopic: alt\ndoc_ids: [5]\nlang: de\n---\n"
        "## Aktueller Stand\n\nläuft\n\n## Zeitachse\n\n- 2020-01-01 — Start — [#5](u)\n\n"
        "## Offene Fragen\n\n- Wann?\n", encoding="utf-8")
    d = dossiers.load("alice", "alt")
    assert d.current_state == "läuft" and d.timeline[0]["doc_id"] == 5 and d.open_questions == ["Wann?"]
    _run(dossiers.write("alice", d))  # rewritten with anchors
    assert "<!-- pb:state -->" in (p / "alt.md").read_text()
    assert dossiers.load("alice", "alt").open_questions == ["Wann?"]


def test_tree_moves_keep_identity(vault):
    from proactive import dossiers

    d = _run(dossiers.create_manual("alice", "Kfz-Versicherung", "Auto/Versicherungen"))
    assert d.origin == "user" and d.folder == "Auto/Versicherungen" and d.topic == "kfz-versicherung"
    moved = _run(dossiers.move("alice", d.topic, ""))
    assert moved.pbrain_id == d.pbrain_id and moved.folder == ""
    assert "Auto/Versicherungen" in dossiers.folders("alice")
    with pytest.raises(dossiers.DossierError):
        _run(dossiers.create_manual("alice", "kfz versicherung"))  # same topic key
    with pytest.raises(dossiers.DossierError):
        _run(dossiers.move("alice", d.topic, "../../etc"))


def test_tree_nodes_follow_the_folders():
    from app_ui.pages.findings import dossier_tree
    from proactive.dossiers import Dossier

    a = Dossier(topic="a", label="A", rel="F/Dossiers/Auto/a.md", pbrain_id="1")
    b = Dossier(topic="b", label="B", rel="F/Dossiers/b.md", pbrain_id="2")
    nodes = dossier_tree([a, b], ["Auto", "Leer"], {"a": 3})
    assert [n["label"] for n in nodes] == ["Auto", "Leer", "B"]
    assert nodes[0]["children"][0] == {"id": "d:1", "label": "A", "topic": "a", "count": 3, "insights": 0}


# ── Provenance and the merge check ───────────────────────────────────────────


def test_provenance_comes_from_the_tool_log_only():
    from services.dossier_merge import provenance

    trace = [
        {"name": "get_document_details", "input": {"document_id": 241}, "output": "Details for document #241 [^241-p3]"},
        {"name": "web_fetch_page", "input": {"url": "https://real.example/page"}, "output": "Seite …"},
        {"name": "web_search", "input": {"query": "q"}, "output": "1. https://hit.example/a — Titel"},
    ]
    answer = ("Laut Vertrag [^241-p3] und [^999] gilt X. Quelle: https://hit.example/a und "
              "https://invented.example/z")
    p = provenance(answer, trace, model="m")
    assert p["docs"] == [{"id": 241, "page": 3}]            # 999 was never returned by a tool
    assert [w["url"] for w in p["web"]] == ["https://hit.example/a", "https://real.example/page"]
    assert p["evidence"] == "documents+web"
    assert provenance("Nur Nachdenken.", [])["evidence"] == "reasoning"


def test_merge_check_flags_numbers_and_clamps():
    from services.dossier_merge import check_merge

    res = check_merge({"title": "# T", "insight": "## X\nKostet 412 € statt 380 € [^5-p2].",
                       "answers_questions": [1, 7, "x"], "new_questions": ["a", "b", "c", "d"]},
                      excerpt="Kostet 412 € im Jahr.", haystack="", n_questions=2)
    assert res["unsupported"] == ["380"] and res["answers"] == [1]
    assert res["title"] == "T" and not res["text"].startswith("#") and "[#5 p2]" in res["text"]
    assert res["new_questions"] == []  # never proposed: they were made up


# ── Ledger search ────────────────────────────────────────────────────────────


def test_search_finds_held_findings_and_insights(vault):
    from app_ui.pages.findings import search_ledger
    from proactive import dossiers, findings

    f = findings.Finding(finding_id="abcdefg", kind="insight", title="Strompreis gestiegen", status="held",
                         body="Abschlag erhöht.")
    d = dossiers.Dossier(topic="kfz", label="Kfz", insights=[_insight("Übersicht ADAC")])
    res = search_ledger("strompreis", [f], [d])
    assert res["findings"] == [f]
    res = search_ledger("adac", [f], [d])
    assert res["insights"][0][1].title == "Übersicht ADAC"
    assert search_ledger("uebersicht", [f], [d])["insights"] == [] and search_ledger("ubersicht", [f], [d])["insights"]


def test_search_finds_findings_by_the_names_of_their_documents(vault):
    from app_ui.pages.findings import search_ledger
    from proactive import dossiers, findings

    f = findings.Finding(finding_id="abcdefg", kind="insight", title="Abschlag erhöht", doc_ids=[241])
    d = dossiers.Dossier(topic="strom", label="Strom", added_docs=[300])
    titles = {241: "Stadtwerke Jahresabrechnung 2026", 300: "Stadtwerke Vertrag"}
    res = search_ledger("jahresabrechnung", [f], [d], titles)
    assert res["findings"] == [f] and res["documents"] == [(241, "Stadtwerke Jahresabrechnung 2026")]
    res = search_ledger("stadtwerke vertrag", [f], [d], titles)
    assert res["dossiers"] == [d] and res["documents"][0][0] == 300


def test_merge_falls_back_when_the_model_answers_past_the_tool(monkeypatch):
    """A local model that writes prose instead of calling `file_insight` was a
    bare "ValueError" in the dialog: retry once, then a plain-text round."""
    from proactive import dossiers
    from services import dossier_merge
    from werkbank import llm_lane

    calls = {"structured": 0}

    async def structured(*a, **k):
        calls["structured"] += 1
        raise ValueError("OpenAI-compat model did not call tool 'file_insight' — finish_reason=stop")

    async def text(*a, **k):
        return "<think>hm</think>\nADAC ohne Werkstattbindung\n\nDer Tarif kostet 412 € im Jahr."

    async def awake(*a, **k):
        return None

    monkeypatch.setattr(llm_lane, "complete_structured", structured)
    monkeypatch.setattr(llm_lane, "complete", text)
    monkeypatch.setattr(dossier_merge, "_wake", awake)
    d = dossiers.Dossier(topic="kfz", open_questions=["Frist?"])
    out = _run(dossier_merge.merge("Er kostet 412 € im Jahr.", d, model="m", user="alice", token="t", haystack=""))
    assert calls["structured"] == 2 and out["mode"] == "text"
    assert out["title"] == "ADAC ohne Werkstattbindung" and "412" in out["text"] and out["unsupported"] == []


def test_a_whole_conversation_is_condensed_with_all_its_sources():
    from services import dossier_merge

    msgs = [{"role": "user", "content": "Lohnt der Wechsel?"},
            {"role": "assistant", "content": "Laut Police [^241-p2] …"},
            {"role": "user", "content": "Und der ADAC?"},
            {"role": "assistant", "content": "412 € [web]"}]
    t = dossier_merge.transcript(msgs)
    assert t.startswith("User: Lohnt der Wechsel?") and "[#241 p2]" in t and t.endswith("Assistant: 412 € [web]")
    p = dossier_merge.merge_provenance([{"docs": [{"id": 241, "page": 2}], "haystack": "a"},
                                        {"web": [{"url": "https://adac.de/x"}], "haystack": "412"}])
    assert p["evidence"] == "documents+web" and p["docs"] == [{"id": 241, "page": 2}] and "412" in p["haystack"]
    assert dossier_merge.merge_provenance([{}, {}]) == {}


def test_open_questions_sent_as_a_json_string_stay_questions(vault):
    """A local model sent `open_questions` as the string '["Wer zahlt?", …]':
    every character became a question of its own."""
    from proactive import dossiers

    res = {"timeline": '[{"date": "2026-06-11", "event": "Ankauf", "doc_id": 537}]',
           "open_questions": '["Widersprüchliche HU-Termine prüfen", "BGV kündigen?"]'}
    tl, qs = dossiers.validated(res, {537})
    assert qs == ["Widersprüchliche HU-Termine prüfen", "BGV kündigen?"] and tl[0]["doc_id"] == 537
    assert dossiers.validated({"open_questions": "- Eins\n- Zwei"}, set())[1] == ["Eins", "Zwei"]
    # Files already broken that way read as no questions (the next rescan writes them again).
    assert dossiers.heal_questions(list('["Widersprüchliche')) == []
    assert dossiers.heal_questions(["Frist?", "Werkstatt?"]) == ["Frist?", "Werkstatt?"]


def test_question_actions_survive_a_rebuild(vault):
    from proactive import dossiers

    _run(dossiers.write("alice", dossiers.Dossier(topic="kfz", doc_ids=[1], open_questions=["A?", "B?", "C?"])))
    _run(dossiers.resolve_question("alice", "kfz", "A?"))
    _run(dossiers.drop_question("alice", "kfz", "B?"))
    _run(dossiers.add_question("alice", "kfz", "Eigene?"))
    d = dossiers.load("alice", "kfz")
    assert d.open_questions == ["C?", "Eigene?"] and d.resolved[0]["question"] == "A?" and d.dropped_questions == ["B?"]
    # A rebuild that proposes A? and B? again does not bring them back.
    rebuilt = dossiers.from_result("alice", "kfz", {"doc_ids": [1], "open_questions": ["A?", "B?", "D?"]},
                                   lang="de", version=1, existing=d)
    assert rebuilt.open_questions == ["D?"] and rebuilt.dropped_questions == ["B?"]
    _run(dossiers.reopen_question("alice", "kfz", "A?"))
    assert "A?" in dossiers.load("alice", "kfz").open_questions
