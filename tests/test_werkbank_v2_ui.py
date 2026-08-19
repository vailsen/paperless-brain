"""Werkbank v2 — board rendering, fact markers, and restoring defaults.

The UI parts worth testing are the ones that carry a guarantee: a marker that
resolves to the fact it names, and a way back to a shipped archetype after the
user has edited it into something broken. Both are logic, not looks.
"""

import pathlib

import pytest

from config.settings import settings
from werkbank.v2 import reflection
from werkbank.v2.models import (
    Brief,
    Evidence,
    Fact,
    RunState,
    Source,
    SourceTrust,
    Subtask,
    SubtaskResult,
    SubtaskStatus,
)
from werkbank.v2.ui import board, style


def _fact(fid="st1.f1") -> Fact:
    return Fact(id=fid, claim="Die Frist beträgt sechs Wochen.", evidence=Evidence.QUOTE,
                sources=[Source(id="s1", type="paperless", trust=SourceTrust.AUTHORITATIVE,
                                ref="doc:12#p1", quote="Frist: sechs Wochen")])


def _state() -> RunState:
    return RunState(
        run_id="r1", user_id="alice", model="stub",
        brief=Brief(original_request="x", goal="y", acceptance_criteria=["c"]),
        subtasks=[Subtask(subtask_id="st1", question="Was steht im Vertrag?",
                          agent="doc_researcher", acceptance_criteria=["c"],
                          covers_criteria=[0])],
        results={"st1": SubtaskResult(subtask_id="st1", agent="doc_researcher",
                                      question="Was steht im Vertrag?",
                                      status=SubtaskStatus.OK, facts=[_fact()])},
    )


# ── Fact markers ──────────────────────────────────────────────────────────────


def test_the_report_is_split_at_every_fact_marker():
    parts = board._split_markers("Die Frist ist klar [st1.f1] und lang [st2.f3]. Ende.")
    assert [m for _text, m in parts if m] == ["st1.f1", "st2.f3"]
    assert parts[0][0] == "Die Frist ist klar "
    assert parts[-1][0] == ". Ende."


def test_text_without_markers_survives_whole():
    parts = board._split_markers("Ein Absatz ohne Beleg.")
    assert parts == [("Ein Absatz ohne Beleg.", "")]


def test_a_marker_shaped_string_that_is_not_a_fact_id_is_left_alone():
    """`[2026]` and `[siehe oben]` are not citations."""
    parts = board._split_markers("Siehe [2026] und [siehe oben].")
    assert [m for _t, m in parts if m] == []


def test_every_marker_in_a_report_resolves_to_a_fact():
    state = _state()
    known = {f.id for f in state.all_facts()}
    report = "Die Frist beträgt sechs Wochen [st1.f1]."
    assert all(m in known for _t, m in board._split_markers(report) if m)


# ── Style: colour is state, not identity ──────────────────────────────────────


def test_only_two_run_states_earn_a_colour():
    from werkbank.v2.ui.page import STATUS_WORDS, TONE_ACTIVE, TONE_ATTENTION, run_tone

    tones = {run_tone(status) for status in STATUS_WORDS}
    assert tones == {"", TONE_ACTIVE, TONE_ATTENTION}


def test_a_finished_run_is_not_coloured():
    """A board where everything is coloured shows nothing. Done and planned are
    outcomes the user does not have to react to."""
    from werkbank.v2.ui.page import TONE_ACTIVE, TONE_ATTENTION, run_tone

    assert run_tone("done") == ""
    assert run_tone("planned") == ""
    assert run_tone("running") == TONE_ACTIVE
    assert run_tone("failed") == TONE_ATTENTION


def test_the_board_css_uses_tokens_and_not_hex():
    import re

    assert not re.search(r"#[0-9a-fA-F]{6}", style.BOARD_CSS)
    assert "var(--c-accent)" in style.BOARD_CSS and "var(--c-warn)" in style.BOARD_CSS


def test_wide_content_in_the_report_scrolls_inside_its_own_box():
    """Nothing may scroll the page sideways on a phone."""
    assert "overflow-x: auto" in style.BOARD_CSS
    assert "max-width: 100%" in style.BOARD_CSS


# ── Restoring a shipped archetype ─────────────────────────────────────────────


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "app_path", tmp_path, raising=False)
    from werkbank import repository

    monkeypatch.setattr(repository, "_DB_PATH", tmp_path / "data" / "papersage.db")
    monkeypatch.setattr(repository, "_LEGACY_DB_PATH", tmp_path / "data" / "werkbank.db")
    repository.init_db()
    return tmp_path


def test_an_edited_default_can_be_put_back(db):
    from werkbank import archetypes, repository

    archetypes.seed_defaults_if_needed("alice")
    original = repository.get_archetype_by_name("doc_researcher", "alice")
    assert original is not None

    repository.update_archetype(original.id, "alice", soul_text="kaputt",
                                enabled_tools=["calculate"])
    edited = repository.get_archetype_by_name("doc_researcher", "alice")
    assert archetypes.differs_from_default(edited)

    assert archetypes.restore_default("doc_researcher", "alice") is True
    restored = repository.get_archetype_by_name("doc_researcher", "alice")
    assert restored.soul_text == original.soul_text
    assert sorted(restored.enabled_tools) == sorted(original.enabled_tools)
    assert not archetypes.differs_from_default(restored)


def test_restore_works_after_the_mistake_people_actually_make(db):
    """Deleting it. A reset that only updates an existing row is no way back."""
    from werkbank import archetypes, repository

    archetypes.seed_defaults_if_needed("alice")
    victim = repository.get_archetype_by_name("web_researcher", "alice")
    repository.delete_archetype(victim.id, "alice")
    assert repository.get_archetype_by_name("web_researcher", "alice") is None

    assert archetypes.restore_default("web_researcher", "alice") is True
    assert repository.get_archetype_by_name("web_researcher", "alice") is not None


def test_a_user_created_archetype_has_no_default_and_is_never_touched(db):
    from werkbank import archetypes, repository

    archetypes.seed_defaults_if_needed("alice")
    mine = repository.create_archetype(
        user_id="alice", name="mein_agent", description="meiner",
        soul_text="mein prompt", enabled_tools=["calculate"])

    assert archetypes.restore_default("mein_agent", "alice") is False
    assert not archetypes.differs_from_default(mine)

    archetypes.restore_all_defaults("alice")
    still = repository.get_archetype_by_name("mein_agent", "alice")
    assert still.soul_text == "mein prompt"


def test_restore_all_reports_how_many_it_touched(db):
    from werkbank import archetypes

    archetypes.seed_defaults_if_needed("alice")
    assert archetypes.restore_all_defaults("alice") == len(archetypes.default_names())


def test_an_untouched_default_is_not_offered_a_reset(db):
    """A button that would change nothing teaches people to distrust the ones that do."""
    from werkbank import archetypes, repository

    archetypes.seed_defaults_if_needed("alice")
    fresh = repository.get_archetype_by_name("doc_researcher", "alice")
    assert not archetypes.differs_from_default(fresh)


# ── The reflection block survives rendering ───────────────────────────────────


def test_the_reflection_markers_do_not_leak_into_the_rendered_report():
    state = _state()
    markdown = f"Text [st1.f1].\n\n{reflection.build(state)}\n"
    body = markdown.replace(reflection.BEGIN, "> ").replace(reflection.END, "")
    assert reflection.BEGIN not in body and reflection.END not in body
    assert "Selbstreflexion" in body


# ── One set of defaults, not two ──────────────────────────────────────────────


def test_the_shipped_archetypes_are_the_ones_in_the_yaml(db):
    """Two default sets is the defect this pins. v1 seeded four archetypes into
    the same table v2 merges into its registry, so the planner was offered
    `retriever` and `writer` beside the tuned v2 agents — and those have no
    prompt file, so the runner falls back to a generic instruction with none of
    the evidence rules."""
    from werkbank import archetypes, repository
    from werkbank.v2 import registry

    archetypes.seed_defaults_if_needed("alice")
    names = {a.name for a in repository.get_archetypes("alice")}

    assert names == set(registry.load_defaults().agents)
    assert not names & {"retriever", "researcher", "secretary", "writer"}


def test_an_untouched_v1_archetype_is_removed_on_upgrade(db):
    from werkbank import archetypes, repository

    repository.create_archetype(
        user_id="alice", name="retriever", description="v1",
        soul_text="You are a precise document researcher. Find documents.",
        enabled_tools=["search"],
    )
    archetypes.seed_defaults_if_needed("alice")

    assert repository.get_archetype_by_name("retriever", "alice") is None


def test_a_v1_archetype_the_user_rewrote_survives_as_their_own(db):
    """Their work, not ours to tidy away — it lives on as a user archetype."""
    from werkbank import archetypes, repository

    repository.create_archetype(
        user_id="alice", name="retriever", description="meins",
        soul_text="Du bist mein eigener Rechercheur mit eigenen Regeln.",
        enabled_tools=["search"],
    )
    archetypes.seed_defaults_if_needed("alice")

    survivor = repository.get_archetype_by_name("retriever", "alice")
    assert survivor is not None
    assert survivor.soul_text.startswith("Du bist mein eigener")


def test_editing_a_shipped_agent_cannot_switch_off_its_requirement(db):
    """`comms_researcher` needs mail. If an edited row replaced the shipped spec
    wholesale, its `requires` would be lost and the planner would assign it to a
    user with no mail account — which is how a run ends up answering from
    parametric knowledge instead of recording a gap."""
    from werkbank.v2 import registry

    edited = registry.AgentSpec(
        id="comms_researcher", label="comms_researcher", description="meins",
        tools=["search_emails"], user_defined=True, prompt_text="Mach was.",
    )

    without_mail = registry.available_agents({"paperless", "vault"}, user_agents=[edited])
    with_mail = registry.available_agents({"paperless", "vault", "mail"},
                                          user_agents=[edited])

    assert "comms_researcher" not in without_mail.agents
    assert with_mail.agents["comms_researcher"].prompt_text == "Mach was."
    assert with_mail.agents["comms_researcher"].tools == ["search_emails"]


def test_an_edited_prompt_is_what_the_agent_runs_on(db):
    from werkbank.v2 import registry, runner

    spec = registry.load_defaults().agents["doc_researcher"]
    assert "Fact" in runner.agent_prompt(spec) or spec.prompt_path().is_file()

    spec.prompt_text = "Nur das hier."
    assert runner.agent_prompt(spec) == "Nur das hier."


# ── The run list ──────────────────────────────────────────────────────────────


def test_a_run_timestamp_is_shown_in_the_users_timezone():
    """Stored UTC, read locally. Slicing the ISO string was two hours off in
    Germany all summer — the store was right, the display was lying about when
    a run happened."""
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from werkbank.v2.ui import page

    # `local_tz` is imported inside the function, so the source module is what
    # has to be patched.
    with patch("config.settings.local_tz", return_value=ZoneInfo("Europe/Berlin")):
        assert page.local_time("2026-08-18T05:47:11+00:00") == "2026-08-18 07:47"
        # A naive timestamp is UTC too — that is what the store writes.
        assert page.local_time("2026-08-18T05:47:11") == "2026-08-18 07:47"


def test_an_unreadable_timestamp_does_not_break_the_list():
    from werkbank.v2.ui import page

    assert page.local_time("") == ""
    assert page.local_time("not a date").startswith("not a date")


def test_polling_pauses_while_a_run_dialog_is_open():
    """A dialog built inside the refreshable slot is destroyed by the next poll,
    which looked like the dialog closing itself after a couple of seconds. It is
    built in a page-level host now, and the poll holds off besides."""
    import inspect

    from werkbank.v2.ui import page

    source = inspect.getsource(page.werkbank_page)
    assert "dialog_host = ui.element" in source
    assert "if not _open_dialogs:" in source

    open_source = inspect.getsource(page._open_run)
    assert "with host, ui.dialog()" in open_source
    # …and the dialog refreshes itself instead, because a running subtask is
    # exactly what someone with this open is watching.
    assert "body.refresh()" in open_source


def test_the_goal_fields_are_re_measured_when_the_section_opens():
    """Autogrow measures a field when it is built, and inside a collapsed
    expansion that is zero — so Goal, format and out-of-scope each stayed one
    line tall no matter how long the text. Quasar only re-measures on input, so
    opening the section has to nudge it; `rows` does not help because autogrow
    overrides it."""
    import inspect

    from werkbank.v2.ui import brief_dialog

    source = inspect.getsource(brief_dialog.BriefDialog.build)
    assert 'details.on("show"' in source
    assert "new Event('input'" in source
    assert "rows=" not in source          # autogrow wins over rows; do not pretend


# ── The board says what it means ──────────────────────────────────────────────


def test_partial_is_not_marked_as_something_to_react_to():
    """`partial` means "answered, with the holes named" — the outcome the whole
    design aims at. Marking it warn painted every honest subtask yellow, and a
    board where everything is coloured says nothing."""
    from werkbank.v2.models import SubtaskStatus
    from werkbank.v2.ui import style as style_mod

    tones = style_mod._TONES if hasattr(style_mod, "_TONES") else None
    import inspect

    source = inspect.getsource(style_mod.status_pill)
    assert "SubtaskStatus.PARTIAL: \"is-attention\"" not in source
    assert "SubtaskStatus.UNRESOLVABLE: \"is-attention\"" in source
    assert tones is None or SubtaskStatus.PARTIAL not in tones


def test_a_criterion_badge_says_which_criterion_it_is():
    """A row of bare "met / not met" says nothing about *what* was met."""
    import inspect

    from werkbank.v2.ui import style as style_mod

    source = inspect.getsource(style_mod.verdict_pill)
    assert "index" in source and "number" in source


def test_a_derived_fact_shows_the_facts_it_was_built_on():
    """Its `sources` are placeholders; rendering them as documents produced a
    column of the word "fact" under a row of identical "ohne Quelle" chips."""
    import inspect

    from werkbank.v2.ui import board as board_mod

    source = inspect.getsource(board_mod.fact_dialog)
    assert "Built on these facts" in source
    assert "state.fact_by_id" in source
    assert "real_sources" in source


def test_the_source_link_is_the_url_the_tool_fetched():
    """The catalogue shown to the model truncated every argument at 40
    characters, the model copied that into `sources[].ref`, and every link in
    the report pointed at a chopped URL."""
    from werkbank.v2.tools import _short

    long_url = "https://www.bfs.de/DE/themen/emf/hff/schutz/grenzwerte/grenzwerte.html"
    rendered = _short({"url": long_url})

    assert long_url in rendered
    # Non-identifying arguments are still shortened.
    assert len(_short({"query": "x" * 200})) < 80


def test_a_blocked_search_engine_is_not_reported_as_an_empty_web():
    """SearXNG answers HTTP 200 with an empty result list when its upstream
    engines are CAPTCHA'd — which a burst of agent searches provokes. In one run
    every engine was suspended and the agent concluded the web had nothing."""
    from werkbank.v2.tools import _looks_empty

    assert _looks_empty(
        "Search unavailable — every engine refused this request "
        "(google: Suspended: CAPTCHA; duckduckgo: CAPTCHA)."
    )
    assert _looks_empty("No results found.")
    assert not _looks_empty("1. [Ein Treffer](https://e.example)")


def test_the_web_search_tool_names_the_blocked_engines():
    import pathlib as _pathlib

    root = _pathlib.Path(__file__).resolve().parent.parent
    source = (root / "services" / "chat_service.py").read_text(encoding="utf-8")

    assert "unresponsive_engines" in source
    assert "the search itself did not run" in source


def test_the_web_search_tool_offers_the_academic_engines():
    """Case reports, measurements and epidemiology live in PubMed, Crossref and
    OpenAlex; a general web search mostly returns pages *about* them. The
    SearXNG instance had those engines enabled all along — nothing could ask
    for them."""
    from services.chat_service import TOOL_DEFINITIONS

    tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "web_search")
    category = tool["input_schema"]["properties"]["category"]

    assert "science" in category["enum"]
    assert "PubMed" in category["description"]

    prompt = (
        pathlib.Path(__file__).resolve().parent.parent
        / "werkbank" / "v2" / "prompts" / "agents" / "web_researcher.md"
    ).read_text(encoding="utf-8")
    assert 'category: "science"' in prompt


def test_the_report_states_the_run_time_in_the_users_timezone():
    """Stored UTC, read by a human. A report that misstates when it ran is a
    report whose other timestamps have to be doubted too."""
    from unittest.mock import patch
    from zoneinfo import ZoneInfo

    from werkbank.v2.writer import _local

    with patch("config.settings.local_tz", return_value=ZoneInfo("Europe/Berlin")):
        assert _local("2026-08-18T09:33:39.494572+00:00") == "2026-08-18 11:33:39"
        assert _local("2026-08-18T09:33:39.494572") == "2026-08-18 11:33:39"
    assert _local("kaputt") == "kaputt"


def test_a_bot_wall_is_not_treated_as_the_article():
    """Observed: a JAMA case report came back as 603 characters of "enable
    JavaScript" and an eplasty case study as 427 — both exactly the source the
    question needed. An agent that is not told what happened quotes the banner."""
    from services.chat_service import _unreadable_page

    assert _unreadable_page("Please enable JavaScript to continue.")
    assert _unreadable_page("Checking your browser before accessing…")
    assert _unreadable_page("Subscribe to continue reading this article")
    assert _unreadable_page("short stub")
    assert not _unreadable_page("Microwave burns are burn injuries caused by " + "x" * 1000)


def test_an_unreadable_page_counts_as_nothing_retrieved():
    """So the gap says `source_unavailable` rather than "there is nothing"."""
    from werkbank.v2.tools import _looks_empty

    assert _looks_empty("Could not read https://x.example — the page returned a block page")
    assert _looks_empty("No text content extractable from: https://x.example")


def test_the_two_states_that_want_the_user_are_the_marked_ones():
    """A brief waiting for confirmation and a failed briefing both need a
    decision; briefing and running are just the machine working."""
    from werkbank.v2.ui.page import TONE_ACTIVE, TONE_ATTENTION, run_tone

    assert run_tone("draft") == TONE_ATTENTION
    assert run_tone("briefing_failed") == TONE_ATTENTION
    assert run_tone("briefing") == TONE_ACTIVE
    assert run_tone("running") == TONE_ACTIVE
    assert run_tone("done") == ""


def test_a_run_being_briefed_offers_no_board_to_open():
    """There is nothing in it yet — an empty board reads as a broken run."""
    import inspect

    from werkbank.v2.ui import page as page_mod

    source = inspect.getsource(page_mod.werkbank_page)
    assert 'if status not in ("briefing", "draft"):' in source
    assert 'if status == "draft":' in source


def test_werkbank_fetches_through_the_browser():
    """Journals, registries and anything behind a cookie banner only open in a
    real browser. In chat the extra seconds are felt by someone waiting; a
    research run already waits minutes on the model."""
    import inspect

    from werkbank.v2.tools import ToolBelt

    source = inspect.getsource(ToolBelt._call)
    assert '_web_fetch_mode.set("crawl4ai")' in source
    assert "_web_fetch_mode.reset(mode)" in source


def test_deleting_a_run_asks_first():
    """A finished run is twenty minutes of model time and a report that cannot
    be reproduced — and the button sits next to the one that opens it."""
    import inspect

    from werkbank.v2.ui import page as page_mod

    source = inspect.getsource(page_mod._delete_run)
    assert "Delete this run?" in source
    assert "gone for good" in source
    # The deletion happens in the confirm handler, never on the first click.
    assert source.index("ui.dialog()") < source.index("dialog.open()")
    assert "store.delete_run" in source.split("def _do")[1].split("container =")[0]


def test_a_running_run_says_it_will_be_stopped():
    import inspect

    from werkbank.v2.ui import page as page_mod

    assert "will be stopped" in inspect.getsource(page_mod._delete_run)


def test_every_dialog_is_built_outside_the_slot_the_poll_refreshes():
    """Third time this bug appeared: the board dialog, then fact dialogs, now
    the brief review. A dialog created inside the refreshable list is deleted a
    few seconds after it opens, mid-sentence."""
    import inspect

    from werkbank.v2.ui import brief_dialog as brief_mod
    from werkbank.v2.ui import page as page_mod

    assert "host=host" in inspect.getsource(page_mod.review_draft)
    assert "with host:" in inspect.getsource(brief_mod.confirm_brief)
    delete_source = inspect.getsource(page_mod._delete_run)
    assert "host if host is not None" in delete_source


def test_a_search_tool_reports_its_own_hit_count():
    """The tools state it in words — "Found: 18 document(s)", "Vault: 5 hits" —
    and that statement is better than anything counted from the formatting.
    Without it a negative finding can never satisfy D3."""
    from werkbank.v2.tools import _hits_from

    assert _hits_from("search", "Found: 18 document(s)\n\n1. #116 — Versicherung") == 18
    assert _hits_from("vault_search", "Vault: 5 hits\n\n• [[Notiz]] › Titel") == 5
    assert _hits_from("search", "Found: 0 document(s)") == 0
    assert _hits_from("web_search", "No results found.") == 0
    assert _hits_from("search", "Fließtext ohne Trefferliste") is None
