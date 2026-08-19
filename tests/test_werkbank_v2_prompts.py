"""Werkbank v2 — role prompts and the v1 cutover.

The prompts are editable because they change what a role is *asked*. They do not
change what is *verified*: D1–D9 run in `checks.py` after the model has spoken
and are unreachable from the settings page. That boundary is the reason exposing
them is safe, so it is pinned here.

The second half pins the cutover itself: `/werkbank`, the chat hand-off and the
settings editor must not reach into the v1 execution path any more. Those
modules still sit on disk for one release (see their deprecation headers), which
is exactly why an import could creep back unnoticed.
"""

import re
from pathlib import Path

import pytest

from werkbank import settings_store
from werkbank.v2 import prompts

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_store, "_DB_PATH", tmp_path / "papersage.db")
    import sqlite3

    with sqlite3.connect(tmp_path / "papersage.db") as conn:
        conn.execute(
            "CREATE TABLE werkbank_settings (key TEXT PRIMARY KEY, value TEXT,"
            " updated_at TEXT)"
        )
    return tmp_path


# ── Overrides ─────────────────────────────────────────────────────────────────


def test_every_role_ships_a_prompt(db):
    for role in prompts.ROLES:
        assert prompts.default_text(role).strip()
        assert prompts.system_prompt(role) == prompts.default_text(role)


def test_an_override_is_what_the_model_gets(db):
    prompts.set_override("planner", "Plane knapp.")

    assert prompts.system_prompt("planner") == "Plane knapp."
    assert prompts.is_overridden("planner")


def test_clearing_an_override_returns_to_the_shipped_prompt(db):
    prompts.set_override("planner", "Plane knapp.")
    prompts.set_override("planner", "")

    assert prompts.system_prompt("planner") == prompts.default_text("planner")
    assert not prompts.is_overridden("planner")


def test_an_override_equal_to_the_default_is_not_stored(db):
    """Otherwise a user who opens the editor and saves freezes today's prompt
    forever, and a later fix to the shipped text never reaches them."""
    prompts.set_override("writer", prompts.default_text("writer"))

    assert not prompts.is_overridden("writer")


def test_token_limits_round_trip_and_have_per_role_defaults(db):
    assert prompts.token_limit("writer") > prompts.token_limit("plan_critic")

    prompts.set_token_limit("writer", 20_000)
    assert prompts.token_limit("writer") == 20_000


# ── The boundary an override cannot cross ─────────────────────────────────────


def test_no_prompt_override_can_reach_the_deterministic_checks(db):
    """`checks.py` never reads a prompt or a setting — it only ever sees facts
    and the raw tool text. If that changes, the checks become negotiable."""
    source = (ROOT / "werkbank" / "v2" / "checks.py").read_text(encoding="utf-8")

    assert "settings_store" not in source
    assert "prompts" not in source
    assert "call_structured" not in source


# ── The v1 cutover ────────────────────────────────────────────────────────────

V1_EXECUTION = re.compile(
    r"werkbank\.(orchestrator|scheduler|compaction|prechecks|roles)\b"
    r"|werkbank\.ui\.(module_page|task_dialog)\b"
)

LIVE_PATHS = [
    "main.py",
    "app_ui/pages/chat.py",
    "app_ui/pages/settings.py",
    "app_ui/pages/dashboard.py",
]


@pytest.mark.parametrize("rel", LIVE_PATHS)
def test_no_live_path_imports_the_v1_execution_path(rel):
    source = (ROOT / rel).read_text(encoding="utf-8")
    assert not V1_EXECUTION.search(source), f"{rel} still reaches into werkbank v1"


def test_v2_owns_the_werkbank_route():
    main = (ROOT / "main.py").read_text(encoding="utf-8")

    assert "werkbank.v2.ui.page" in main
    assert "werkbank.ui.module_page" not in main


def test_the_v1_modules_say_they_are_deprecated():
    """They stay on disk for one release so the cutover is reversible. Anyone
    opening one has to see that before editing it."""
    for rel in [
        "werkbank/orchestrator.py",
        "werkbank/scheduler.py",
        "werkbank/ui/module_page.py",
        "werkbank/ui/task_dialog.py",
    ]:
        head = (ROOT / rel).read_text(encoding="utf-8")[:400]
        assert "DEPRECATED" in head, rel
