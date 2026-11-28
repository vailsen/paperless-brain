"""The one "Save…" (`app_ui/save_chooser.py`): which destinations are offered."""

from app_ui import save_chooser as sc


def test_an_answer_can_go_everywhere():
    assert sc.destinations("answer", dossiers_on=True) == [sc.MEMORY, sc.DOSSIER, sc.PAPERLESS]
    assert sc.destinations("selection", dossiers_on=True) == [sc.MEMORY, sc.DOSSIER, sc.PAPERLESS]


def test_a_whole_conversation_is_no_memory():
    assert sc.destinations("conversation", dossiers_on=True) == [sc.DOSSIER, sc.PAPERLESS]


def test_no_dossier_without_findings_and_no_memory_without_a_user():
    assert sc.destinations("answer", dossiers_on=False) == [sc.MEMORY, sc.PAPERLESS]
    assert sc.destinations("answer", dossiers_on=False, memory_on=False) == [sc.PAPERLESS]
